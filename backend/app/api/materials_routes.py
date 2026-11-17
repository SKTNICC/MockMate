"""材料接口：文本/文件上传、编辑、提取、解析、确认快照、删除。"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models import User
from app.schemas import (
    ConfirmMaterialInput,
    EditMaterialInput,
    Job as JobSchema,
    MaterialDraft as MaterialDraftSchema,
    Materials,
    ParseMaterialInput,
    RevisionInput,
    Snapshot as SnapshotSchema,
    Snapshots,
    TextMaterialInput,
)
from app.services import idempotency as idem
from app.services import jobs as jobs_service
from app.services import materials as materials_service
from app.services.bank import digest

router = APIRouter(tags=["materials"])


def _payload_digest(payload) -> str:
    return digest(payload.model_dump(mode="json")) if payload is not None else digest({})


def _begin(db: Session, user: User, request: Request, request_hash: str) -> idem.IdempotencyHandle:
    return idem.begin(
        db,
        user_id=user.id,
        method=request.method,
        path=request.url.path,
        raw_key=request.headers.get("Idempotency-Key"),
        request_hash=request_hash,
    )


@router.get("/materials", response_model=Materials, operation_id="listMaterials")
def list_materials(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Materials:
    return Materials(items=materials_service.list_materials(db, user.id))


@router.post(
    "/materials/text",
    response_model=MaterialDraftSchema,
    status_code=201,
    operation_id="createTextMaterial",
)
def create_text_material(
    payload: TextMaterialInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, _payload_digest(payload))
    if handle.replay is not None:
        return handle.replay
    draft = materials_service.create_text_material(db, user.id, payload, get_settings())
    body = materials_service.material_to_schema(draft).model_dump(mode="json")
    return idem.complete(db, handle, 201, body)


@router.post(
    "/materials/file",
    response_model=MaterialDraftSchema,
    status_code=201,
    operation_id="createFileMaterial",
)
def create_file_material(
    request: Request,
    kind: str = Form(...),
    title: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    settings = get_settings()
    if kind not in ("resume", "jd"):
        raise AppError("INVALID_INPUT", "kind 必须是 resume 或 jd", details=["kind"])
    if not title.strip() or len(title.strip()) > 120:
        raise AppError("INVALID_INPUT", "title 必须为 1–120 字符", details=["title"])
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise AppError("FILE_TOO_LARGE", "文件超过 10 MiB 上限")
    data = file.file.read()
    if len(data) > settings.max_upload_bytes:
        raise AppError("FILE_TOO_LARGE", "文件超过 10 MiB 上限")
    file_hash = hashlib.sha256(data).hexdigest()
    plan_digest = idem.digest_file_input(kind, title, file_hash)
    handle = _begin(db, user, request, plan_digest)
    if handle.replay is not None:
        return handle.replay
    draft = materials_service.create_file_material(
        db,
        user.id,
        kind=kind,
        title=title,
        filename=file.filename or "",
        content_type=file.content_type,
        data=data,
        settings=settings,
    )
    body = materials_service.material_to_schema(draft).model_dump(mode="json")
    return idem.complete(db, handle, 201, body)


@router.get("/materials/{material_id}", response_model=MaterialDraftSchema, operation_id="getMaterial")
def get_material(
    material_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> MaterialDraftSchema:
    import uuid

    try:
        material_uuid = uuid.UUID(material_id)
    except ValueError:
        raise AppError("NOT_FOUND", "材料不存在", status_code=404)
    draft = materials_service.get_material(db, user.id, material_uuid)
    return materials_service.material_to_schema(draft)


@router.put("/materials/{material_id}", response_model=MaterialDraftSchema, operation_id="editMaterial")
def edit_material(
    material_id: str,
    payload: EditMaterialInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import uuid

    handle = _begin(db, user, request, _payload_digest(payload))
    if handle.replay is not None:
        return handle.replay
    draft = materials_service.get_material(db, user.id, uuid.UUID(material_id))
    draft = materials_service.edit_material(db, draft, payload, get_settings())
    body = materials_service.material_to_schema(draft).model_dump(mode="json")
    return idem.complete(db, handle, 200, body)


@router.delete("/materials/{material_id}", status_code=204, operation_id="deleteMaterial")
def delete_material(
    material_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import uuid

    handle = _begin(db, user, request, digest({}))
    if handle.replay is not None:
        return handle.replay
    draft = materials_service.get_material(db, user.id, uuid.UUID(material_id))
    materials_service.delete_material(db, draft, get_settings(), request.url.path)
    return idem.complete(db, handle, 204, None)


@router.post(
    "/materials/{material_id}/extract",
    response_model=JobSchema,
    status_code=202,
    operation_id="extractMaterial",
)
def extract_material(
    material_id: str,
    payload: RevisionInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import uuid

    handle = _begin(db, user, request, _payload_digest(payload))
    if handle.replay is not None:
        return handle.replay
    draft = materials_service.get_material(db, user.id, uuid.UUID(material_id))
    job = materials_service.create_extract_job(db, draft, payload.revision)
    body = jobs_service.job_to_schema(db, job, get_settings().max_job_attempts).model_dump(mode="json")
    return idem.complete(db, handle, 202, body)


@router.post(
    "/materials/{material_id}/parse",
    response_model=JobSchema,
    status_code=202,
    operation_id="parseMaterial",
)
def parse_material(
    material_id: str,
    payload: ParseMaterialInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import uuid

    handle = _begin(db, user, request, _payload_digest(payload))
    if handle.replay is not None:
        return handle.replay
    if not payload.external_processing_consent:
        raise AppError(
            "CONSENT_REQUIRED", "解析会把简历文本发送给配置的外部模型，需要先确认"
        )
    from app.ai.provider import get_provider

    get_provider(get_settings())
    draft = materials_service.get_material(db, user.id, uuid.UUID(material_id))
    job = materials_service.create_parse_job(
        db, draft, revision=payload.revision, role_id=payload.role_id, level=payload.level
    )
    body = jobs_service.job_to_schema(db, job, get_settings().max_job_attempts).model_dump(mode="json")
    return idem.complete(db, handle, 202, body)


@router.post(
    "/materials/{material_id}/confirm",
    response_model=SnapshotSchema,
    status_code=201,
    operation_id="confirmMaterial",
)
def confirm_material(
    material_id: str,
    payload: ConfirmMaterialInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import uuid

    handle = _begin(db, user, request, _payload_digest(payload))
    if handle.replay is not None:
        return handle.replay
    draft = materials_service.get_material(db, user.id, uuid.UUID(material_id))
    snapshot = materials_service.confirm_material(db, draft, payload)
    body = materials_service.snapshot_to_schema(snapshot).model_dump(mode="json")
    return idem.complete(db, handle, 201, body)


@router.get(
    "/materials/{material_id}/snapshots",
    response_model=Snapshots,
    operation_id="listMaterialSnapshots",
)
def list_snapshots(
    material_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Snapshots:
    import uuid

    draft = materials_service.get_material(db, user.id, uuid.UUID(material_id))
    return Snapshots(items=materials_service.list_snapshots(db, draft))


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotSchema, operation_id="getSnapshot")
def get_snapshot(
    snapshot_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> SnapshotSchema:
    import uuid

    row = materials_service.get_snapshot(db, user.id, uuid.UUID(snapshot_id))
    return materials_service.snapshot_to_schema(row)
