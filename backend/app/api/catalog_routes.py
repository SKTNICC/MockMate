"""岗位与内置 JD 模板（登录后可读）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.db import get_db
from app.models import User
from app.schemas import JdTemplate, JdTemplates, Roles, SkillCodes
from app.services import catalog

router = APIRouter(tags=["catalog"])


@router.get("/roles", response_model=Roles, operation_id="listRoles")
def list_roles(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Roles:
    return Roles(items=catalog.list_roles(db))


@router.get("/skill-codes", response_model=SkillCodes, operation_id="listSkillCodes")
def list_skill_codes(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> SkillCodes:
    return SkillCodes(items=catalog.list_skill_codes(db))


@router.get("/jd-templates", response_model=JdTemplates, operation_id="listJdTemplates")
def list_jd_templates(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> JdTemplates:
    return JdTemplates(items=catalog.list_templates(db))


@router.get("/jd-templates/{template_id}", response_model=JdTemplate, operation_id="getJdTemplate")
def get_jd_template(
    template_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> JdTemplate:
    return catalog.template_to_schema(catalog.get_template(db, template_id))
