"""岗位目录与内置 JD 模板读取。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import JdTemplate, Role, SkillCode
from app.schemas import JdTemplate as JdTemplateSchema
from app.schemas import Role as RoleSchema
from app.schemas import SkillCode as SkillCodeSchema


def role_to_schema(role: Role) -> RoleSchema:
    return RoleSchema(
        id=role.id,
        name=role.name,
        levels=list(role.levels or []),
        enabled=role.enabled,
        sample_only=role.sample_only,
    )


def get_role(db: Session, role_id: str) -> Role | None:
    return db.get(Role, role_id)


def _enabled_skill_codes_stmt():
    return select(SkillCode).where(SkillCode.enabled.is_(True)).order_by(SkillCode.code)


def list_skill_codes(db: Session) -> list[SkillCodeSchema]:
    """返回当前启用的技能目录（稳定排序）。"""
    rows = db.scalars(_enabled_skill_codes_stmt()).all()
    return [SkillCodeSchema(code=row.code, label=(row.label or "").strip() or row.code) for row in rows]


def allowed_skill_codes(db: Session) -> set[str]:
    """已启用的技能编码词表；模型自创编码不得进入材料或计划。"""
    return {item.code for item in list_skill_codes(db)}


def require_role_level(db: Session, role_id: str, level: str, *, require_enabled: bool) -> Role:
    role = get_role(db, role_id)
    if role is None:
        raise AppError("ROLE_LEVEL_MISMATCH", "岗位不存在", details=[role_id])
    if level not in (role.levels or []):
        raise AppError(
            "ROLE_LEVEL_MISMATCH",
            "所选级别不属于该岗位",
            details=[f"{role_id}/{level}"],
        )
    if require_enabled and not role.enabled:
        raise AppError(
            "ROLE_NOT_AVAILABLE",
            "该岗位尚未启用，无法用于本次面试准备",
            details=[role_id],
        )
    return role


def list_roles(db: Session) -> list[RoleSchema]:
    rows = db.scalars(select(Role).order_by(Role.id)).all()
    return [role_to_schema(row) for row in rows]


def template_to_schema(template: JdTemplate) -> JdTemplateSchema:
    return JdTemplateSchema(
        id=template.id,
        version=template.version,
        role_id=template.role_id,
        level=template.level,
        title=template.title,
        label=template.label,
        text=template.text,
        profile=template.profile,
    )


def list_templates(db: Session) -> list[JdTemplateSchema]:
    rows = db.scalars(
        select(JdTemplate).where(JdTemplate.enabled.is_(True)).order_by(JdTemplate.id, JdTemplate.version)
    ).all()
    latest: dict[str, JdTemplate] = {}
    for row in rows:
        current = latest.get(row.id)
        if current is None or row.version > current.version:
            latest[row.id] = row
    return [template_to_schema(row) for _, row in sorted(latest.items())]


def get_template(db: Session, template_id: str, version: int | None = None) -> JdTemplate:
    stmt = select(JdTemplate).where(JdTemplate.id == template_id)
    if version is not None:
        stmt = stmt.where(JdTemplate.version == version)
    else:
        stmt = stmt.order_by(JdTemplate.version.desc())
    row = db.scalars(stmt).first()
    if row is None or not row.enabled:
        raise AppError("NOT_FOUND", "内置 JD 模板不存在", status_code=404)
    return row


def ensure_jd_profile_compatible(db: Session, profile: dict) -> None:
    """JD 的岗位／级别必须与目录一致；模板来源还要与模板一致。"""
    role_id = profile.get("role_id")
    level = profile.get("level")
    role = get_role(db, role_id)
    if role is None:
        raise AppError("ROLE_LEVEL_MISMATCH", "JD 中的岗位不存在", details=[str(role_id)])
    if level not in (role.levels or []):
        raise AppError(
            "ROLE_LEVEL_MISMATCH",
            "JD 中的级别不属于该岗位",
            details=[f"{role_id}/{level}"],
        )
