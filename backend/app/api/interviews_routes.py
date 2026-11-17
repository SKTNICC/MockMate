"""M2 文字模拟面试接口。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models import User
from app.schemas import (
    ErrorResponse,
    InterviewAnswerInput,
    InterviewBasicReportInput,
    InterviewContinueInput,
    InterviewDetail,
    InterviewHistory,
    InterviewReceipt,
    InterviewReport,
    InterviewSkipInput,
    InterviewStartReceipt,
    RevisionInput,
    StartInterviewInput,
)
from app.services import idempotency as idem
from app.services import interviews as interviews_service
from app.services.bank import digest

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "请求内容不合法"},
    401: {"model": ErrorResponse, "description": "登录会话无效"},
    404: {"model": ErrorResponse, "description": "对象不存在或不属于本人"},
    409: {"model": ErrorResponse, "description": "状态／版本／任务冲突"},
    422: {"model": ErrorResponse, "description": "请求结构不符合契约"},
}

router = APIRouter(tags=["interviews"], responses=ERROR_RESPONSES)


def _begin(db: Session, user: User, request: Request, payload) -> idem.IdempotencyHandle:
    request_hash = (
        digest(payload.model_dump(mode="json")) if payload is not None else digest({})
    )
    return idem.begin(
        db,
        user_id=user.id,
        method=request.method,
        path=request.url.path,
        raw_key=request.headers.get("Idempotency-Key"),
        request_hash=request_hash,
    )


def _uuid(raw: str, message: str = "对象不存在") -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError("NOT_FOUND", message, status_code=404)


def _receipt_response(db: Session, handle, receipt: InterviewReceipt, status_code: int):
    body = receipt.model_dump(mode="json")
    return idem.complete(db, handle, status_code, body)


@router.post(
    "/plans/{plan_id}/interviews",
    response_model=InterviewStartReceipt,
    status_code=201,
    operation_id="startInterview",
    responses={200: {"model": InterviewStartReceipt, "description": "已存在原场，直接打开"}},
)
def start_interview(
    plan_id: str,
    payload: StartInterviewInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    session, created = interviews_service.start_interview(
        db,
        owner_id=user.id,
        plan_id=_uuid(plan_id, "计划不存在"),
        plan_revision=payload.plan_revision,
        external_processing_consent=payload.external_processing_consent,
        allow_test_fixtures=get_settings().allow_test_fixtures,
    )
    body = InterviewStartReceipt(
        session_id=session.id, revision=session.revision
    ).model_dump(mode="json")
    return idem.complete(db, handle, 201 if created else 200, body)


@router.get(
    "/interviews", response_model=InterviewHistory, operation_id="listInterviews"
)
def list_interviews(
    limit: int = Query(default=20, ge=1, le=50),
    cursor: str | None = Query(default=None),
    plan_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InterviewHistory:
    parsed_plan: uuid.UUID | None = None
    if plan_id is not None:
        try:
            parsed_plan = uuid.UUID(plan_id)
        except ValueError:
            raise AppError("VALIDATION_ERROR", "plan_id 无效", status_code=422)
    return interviews_service.list_history(
        db, owner_id=user.id, limit=limit, cursor=cursor, plan_id=parsed_plan
    )


@router.get(
    "/interviews/{session_id}",
    response_model=InterviewDetail,
    operation_id="getInterview",
)
def get_interview(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InterviewDetail:
    return interviews_service.get_detail(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        max_attempts=get_settings().max_job_attempts,
    )


@router.post(
    "/interviews/{session_id}/answers",
    response_model=InterviewReceipt,
    status_code=202,
    operation_id="submitInterviewAnswer",
    responses={200: {"model": InterviewReceipt, "description": "追问回答直接结清该主问题"}},
)
def submit_answer(
    session_id: str,
    payload: InterviewAnswerInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    receipt, turn_kind = interviews_service.submit_answer(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        revision=payload.revision,
        turn_id=payload.turn_id,
        text=payload.text,
        max_attempts=get_settings().max_job_attempts,
    )
    status_code = 202 if turn_kind == "main" else 200
    return _receipt_response(db, handle, receipt, status_code)


@router.post(
    "/interviews/{session_id}/skip",
    response_model=InterviewReceipt,
    status_code=200,
    operation_id="skipInterviewTurn",
)
def skip_turn(
    session_id: str,
    payload: InterviewSkipInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    receipt = interviews_service.skip_turn(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        revision=payload.revision,
        turn_id=payload.turn_id,
        max_attempts=get_settings().max_job_attempts,
    )
    return _receipt_response(db, handle, receipt, 200)


@router.post(
    "/interviews/{session_id}/pause",
    response_model=InterviewReceipt,
    status_code=200,
    operation_id="pauseInterview",
)
def pause_interview(
    session_id: str,
    payload: RevisionInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    receipt = interviews_service.pause_interview(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        revision=payload.revision,
    )
    return _receipt_response(db, handle, receipt, 200)


@router.post(
    "/interviews/{session_id}/resume",
    response_model=InterviewReceipt,
    status_code=200,
    operation_id="resumeInterview",
)
def resume_interview(
    session_id: str,
    payload: RevisionInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    receipt = interviews_service.resume_interview(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        revision=payload.revision,
    )
    return _receipt_response(db, handle, receipt, 200)


@router.post(
    "/interviews/{session_id}/continue-without-follow-up",
    response_model=InterviewReceipt,
    status_code=200,
    operation_id="continueWithoutFollowUp",
)
def continue_without_follow_up(
    session_id: str,
    payload: InterviewContinueInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    receipt = interviews_service.continue_without_follow_up(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        revision=payload.revision,
        job_id=payload.job_id,
        max_attempts=get_settings().max_job_attempts,
    )
    return _receipt_response(db, handle, receipt, 200)


@router.post(
    "/interviews/{session_id}/finish",
    response_model=InterviewReceipt,
    status_code=200,
    operation_id="finishInterview",
)
def finish_interview(
    session_id: str,
    payload: RevisionInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    receipt = interviews_service.finish_interview(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        revision=payload.revision,
        max_attempts=get_settings().max_job_attempts,
    )
    return _receipt_response(db, handle, receipt, 200)


@router.get(
    "/interviews/{session_id}/report",
    response_model=InterviewReport,
    operation_id="getInterviewReport",
)
def get_interview_report(
    session_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InterviewReport:
    return interviews_service.get_report(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
    )


@router.post(
    "/interviews/{session_id}/report/basic",
    response_model=InterviewReceipt,
    status_code=200,
    operation_id="createBasicInterviewReport",
)
def create_basic_report(
    session_id: str,
    payload: InterviewBasicReportInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    receipt = interviews_service.create_basic_report(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        revision=payload.revision,
        job_id=payload.job_id,
        max_attempts=get_settings().max_job_attempts,
    )
    return _receipt_response(db, handle, receipt, 200)
