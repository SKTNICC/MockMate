"""材料草稿、文件存储、解析任务与已确认快照。"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import Text, cast, delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import IdempotencyRecord, Job, MaterialDraft, MaterialSnapshot, Preparation
from app.schemas import MaterialDraft as MaterialDraftSchema
from app.schemas import Snapshot as SnapshotSchema
from app.services import catalog
from app.services.jobs import create_or_get_job, supersede_job
from app.services.text import (
    check_material_text,
    normalized_stem,
    sha256_text,
    validate_file_metadata,
    validate_profile,
)


def material_to_schema(draft: MaterialDraft) -> MaterialDraftSchema:
    return MaterialDraftSchema(
        id=draft.id,
        revision=draft.revision,
        kind=draft.kind,
        title=draft.title,
        text=draft.text,
        profile=draft.profile,
        state=draft.state,
        template_id=draft.template_id,
        template_version=draft.template_version,
        latest_job_id=draft.latest_job_id,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def snapshot_to_schema(row: MaterialSnapshot) -> SnapshotSchema:
    return SnapshotSchema(
        id=row.id,
        material_id=row.material_id,
        source_revision=row.source_revision,
        kind=row.kind,
        title=row.title,
        text=row.text,
        profile=row.profile,
        template_id=row.template_id,
        template_version=row.template_version,
        confirmed_at=row.confirmed_at,
    )


def get_material(
    db: Session, owner_id: uuid.UUID, material_id: uuid.UUID, *, for_update: bool = False
) -> MaterialDraft:
    if for_update:
        draft = db.scalars(
            select(MaterialDraft)
            .where(MaterialDraft.id == material_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
    else:
        draft = db.get(MaterialDraft, material_id)
    if draft is None or draft.owner_id != owner_id:
        raise AppError("NOT_FOUND", "材料不存在", status_code=404)
    return draft


def get_snapshot(db: Session, owner_id: uuid.UUID, snapshot_id: uuid.UUID) -> MaterialSnapshot:
    row = db.get(MaterialSnapshot, snapshot_id)
    if row is None or row.owner_id != owner_id:
        raise AppError("NOT_FOUND", "材料版本不存在", status_code=404)
    return row


def list_materials(db: Session, owner_id: uuid.UUID) -> list[MaterialDraftSchema]:
    rows = db.scalars(
        select(MaterialDraft)
        .where(MaterialDraft.owner_id == owner_id)
        .order_by(MaterialDraft.updated_at.desc())
        .limit(100)
    ).all()
    return [material_to_schema(row) for row in rows]


def _validate_template(db: Session, payload) -> None:
    if payload.template_id is None and payload.template_version is None:
        return
    if not payload.template_id or payload.template_version is None:
        raise AppError("TEMPLATE_MISMATCH", "使用内置模板时必须同时提供模板 ID 与版本")
    if payload.kind != "jd":
        raise AppError("TEMPLATE_MISMATCH", "只有 JD 可以使用内置模板")
    template = catalog.get_template(db, payload.template_id, payload.template_version)
    if template.version != payload.template_version:
        raise AppError("TEMPLATE_MISMATCH", "模板版本不存在")
    if template.text != payload.text:
        raise AppError(
            "TEMPLATE_MISMATCH",
            "使用内置模板时正文必须与模板完全一致；如需修改请复制内容后另存",
        )


def create_text_material(db: Session, owner_id: uuid.UUID, payload, settings: Settings) -> MaterialDraft:
    text = check_material_text(payload.text, max_characters=settings.max_material_characters)
    _validate_template(db, payload)
    draft = MaterialDraft(
        owner_id=owner_id,
        kind=payload.kind,
        title=payload.title.strip(),
        revision=1,
        text=text,
        profile=None,
        state="text_ready",
        template_id=payload.template_id,
        template_version=payload.template_version,
    )
    db.add(draft)
    db.flush()
    return draft


def _store_file(settings: Settings, data: bytes) -> tuple[str, str]:
    root = settings.private_file_root
    materials_dir = root / "materials"
    materials_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    relative = f"materials/{uuid.uuid4().hex}"
    target = root / relative
    tmp = root / "materials" / f".tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return relative, digest


def create_file_material(
    db: Session,
    owner_id: uuid.UUID,
    *,
    kind: str,
    title: str,
    filename: str,
    content_type: str | None,
    data: bytes,
    settings: Settings,
) -> MaterialDraft:
    meta = validate_file_metadata(filename, content_type, len(data), settings.max_upload_bytes)
    relative, digest = _store_file(settings, data)
    draft = MaterialDraft(
        owner_id=owner_id,
        kind=kind,
        title=title.strip(),
        revision=1,
        text=None,
        profile=None,
        state="uploaded",
        file_ref=relative,
        file_sha256=digest,
        file_name=filename,
    )
    db.add(draft)
    db.flush()
    return draft


def read_file(settings: Settings, draft: MaterialDraft) -> bytes:
    if not draft.file_ref:
        raise AppError("FILE_TEXT_UNAVAILABLE", "材料没有原文件")
    root = settings.private_file_root.resolve()
    path = (root / draft.file_ref).resolve()
    if not str(path).startswith(str(root)):
        raise AppError("FILE_TEXT_UNAVAILABLE", "文件路径非法")
    if not path.exists():
        raise AppError("FILE_TEXT_UNAVAILABLE", "原文件不存在，请重新上传")
    return path.read_bytes()


def edit_material(db: Session, draft: MaterialDraft, payload, settings: Settings) -> MaterialDraft:
    # 不能只比较调用方先前 Session 里的对象：锁本人材料并重读数据库当前 revision。
    locked = get_material(db, draft.owner_id, draft.id, for_update=True)
    if locked.revision != payload.revision:
        raise AppError(
            "REVISION_CONFLICT",
            "材料已被修改，请刷新后重试",
            details=[f"current_revision={locked.revision}"],
        )
    text = check_material_text(payload.text, max_characters=settings.max_material_characters)
    locked.text = text
    locked.title = payload.title.strip()
    locked.profile = None
    locked.state = "text_ready"
    locked.revision = locked.revision + 1
    locked.updated_at = datetime.now(timezone.utc)
    # 锁顺序：先 MaterialDraft，再其 Job。
    _supersede_material_jobs(db, locked, reason_revision=locked.revision - 1)
    db.flush()
    return locked


def _supersede_material_jobs(db: Session, draft: MaterialDraft, *, reason_revision: int) -> None:
    jobs = db.scalars(
        select(Job).where(
            Job.target_id == draft.id,
            Job.kind.in_(("parse", "extract")),
            Job.input_revision == reason_revision,
            Job.state.in_(("queued", "running")),
        )
    ).all()
    for job in jobs:
        supersede_job(job)


def create_extract_job(db: Session, draft: MaterialDraft, payload_revision: int) -> Job:
    if draft.revision != payload_revision:
        raise AppError("REVISION_CONFLICT", "材料版本已变化，请刷新后重试")
    if draft.state != "uploaded" or not draft.file_ref:
        raise AppError("FILE_TEXT_UNAVAILABLE", "该材料没有待提取的文件")
    input_hash = sha256_text(f"{draft.file_ref}:{draft.file_sha256}")
    job, _ = create_or_get_job(
        db,
        owner_id=draft.owner_id,
        kind="extract",
        target_id=draft.id,
        input_revision=draft.revision,
        input_hash=input_hash,
        input_payload={
            "material_id": str(draft.id),
            "file_ref": draft.file_ref,
            "file_name": draft.file_name or "",
        },
    )
    return job


def create_parse_job(
    db: Session, draft: MaterialDraft, *, revision: int, role_id: str, level: str
) -> Job:
    if draft.revision != revision:
        raise AppError("REVISION_CONFLICT", "材料版本已变化，请刷新后重试")
    if draft.state == "uploaded" or not draft.text:
        raise AppError("FILE_TEXT_UNAVAILABLE", "材料文本尚未提取，无法解析")
    if draft.profile is not None:
        raise AppError(
            "REVISION_CONFLICT",
            "当前版本已确认，如需重新解析请先编辑材料产生新版本",
        )
    catalog.require_role_level(db, role_id, level, require_enabled=False)
    input_hash = sha256_text(
        "|".join([role_id, level, draft.kind, sha256_text(draft.text), str(draft.revision)])
    )
    job, _ = create_or_get_job(
        db,
        owner_id=draft.owner_id,
        kind="parse",
        target_id=draft.id,
        input_revision=draft.revision,
        input_hash=input_hash,
        input_payload={
            "material_id": str(draft.id),
            "kind": draft.kind,
            "role_id": role_id,
            "level": level,
        },
    )
    return job


def confirm_material(db: Session, draft: MaterialDraft, payload) -> MaterialSnapshot:
    # 同一 revision 的并发确认必须先串行化，避免两个事务都看不到快照后撞唯一约束。
    draft = get_material(db, draft.owner_id, draft.id, for_update=True)
    if draft.revision != payload.revision:
        raise AppError(
            "REVISION_CONFLICT",
            "材料已被修改，请刷新后重试",
            details=[f"current_revision={draft.revision}"],
        )
    if not draft.text:
        raise AppError("FILE_TEXT_UNAVAILABLE", "材料还没有可核对的文本")
    profile = payload.profile.model_dump(mode="json")
    validate_profile(
        profile,
        draft.kind,
        draft.text,
        allow_user_confirmed=True,
        allowed_skill_codes=catalog.allowed_skill_codes(db),
    )
    if draft.kind == "jd":
        catalog.ensure_jd_profile_compatible(db, profile)
        if draft.template_id:
            template = catalog.get_template(db, draft.template_id, draft.template_version)
            if template.role_id != profile["role_id"] or template.level != profile["level"]:
                raise AppError(
                    "ROLE_LEVEL_MISMATCH",
                    "内置模板的岗位或级别被修改，请先复制文本为自备 JD 再修改",
                )

    existing = db.scalars(
        select(MaterialSnapshot).where(
            MaterialSnapshot.material_id == draft.id,
            MaterialSnapshot.source_revision == draft.revision,
        )
    ).one_or_none()
    if existing is not None:
        if existing.profile == profile:
            return existing
        raise AppError(
            "REVISION_CONFLICT",
            "当前版本已确认过不同内容，请先编辑材料产生新版本再确认",
        )

    snapshot = MaterialSnapshot(
        material_id=draft.id,
        owner_id=draft.owner_id,
        source_revision=draft.revision,
        kind=draft.kind,
        title=draft.title,
        text=draft.text,
        profile=profile,
        template_id=draft.template_id,
        template_version=draft.template_version,
    )
    db.add(snapshot)
    draft.profile = profile
    draft.state = "parsed"
    draft.updated_at = datetime.now(timezone.utc)
    _supersede_material_jobs(db, draft, reason_revision=draft.revision)
    db.flush()
    return snapshot


def list_snapshots(db: Session, draft: MaterialDraft) -> list[SnapshotSchema]:
    rows = db.scalars(
        select(MaterialSnapshot)
        .where(MaterialSnapshot.material_id == draft.id, MaterialSnapshot.owner_id == draft.owner_id)
        .order_by(MaterialSnapshot.confirmed_at.desc())
        .limit(100)
    ).all()
    return [snapshot_to_schema(row) for row in rows]


def delete_material(
    db: Session, draft: MaterialDraft, settings: Settings, current_delete_path: str | None = None
) -> None:
    # 先锁材料，再删除其 Job，与 worker 的“目标 → job”一致。
    draft = get_material(db, draft.owner_id, draft.id, for_update=True)
    snapshot_ids = [
        row
        for row in db.scalars(
            select(MaterialSnapshot.id).where(MaterialSnapshot.material_id == draft.id)
        ).all()
    ]
    if snapshot_ids:
        used = db.scalars(
            select(Preparation.id).where(
                (Preparation.resume_snapshot_id.in_(snapshot_ids))
                | (Preparation.jd_snapshot_id.in_(snapshot_ids))
            )
        ).first()
        if used is not None:
            raise AppError(
                "MATERIAL_IN_USE",
                "该材料已被准备引用，请先删除引用它的准备",
                details=[str(used)],
            )
    file_ref = draft.file_ref
    material_id = draft.id
    db.execute(delete(MaterialSnapshot).where(MaterialSnapshot.material_id == material_id))
    db.execute(delete(Job).where(Job.target_id == material_id, Job.owner_id == draft.owner_id))
    marker = str(material_id)
    purge = delete(IdempotencyRecord).where(
        IdempotencyRecord.user_id == draft.owner_id,
        (IdempotencyRecord.path.like(f"%{marker}%"))
        | (cast(IdempotencyRecord.response, Text).like(f"%{marker}%")),
    )
    if current_delete_path:
        # 保留本次 DELETE 自身的幂等记录，使相同 key 的确定回执可回放 204（不含私人内容）。
        purge = purge.where(
            ~(
                (IdempotencyRecord.method == "DELETE")
                & (IdempotencyRecord.path == current_delete_path)
            )
        )
    db.execute(purge)
    db.delete(draft)
    db.flush()
    if file_ref:
        root = settings.private_file_root.resolve()
        path = (root / file_ref).resolve()
        if str(path).startswith(str(root)) and path.exists():
            path.unlink(missing_ok=True)


def confirm_profile_is_valid(profile: dict | None) -> bool:
    """辅助：判断 profile 是否已有可复用结构。"""
    if not profile:
        return False
    return normalized_stem(str(profile.get("kind", ""))) != ""
