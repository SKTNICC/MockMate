"""材料证据与匹配结果的统一校验。

引用必须同时满足：属于本场允许的快照、item_id 存在于该快照、quote 属于该条目的
evidence 原文，而不只是碰巧出现在整份材料里。
"""

from __future__ import annotations

from app.core.errors import AppError
from app.schemas import Anchor, MatchRow


def profile_items(profile: dict | None) -> dict[str, dict]:
    if hasattr(profile, "model_dump"):
        profile = profile.model_dump(mode="json")
    profile = profile or {}
    sections = (
        ("education", "work_experience", "projects", "skills")
        if profile.get("kind") == "resume"
        else ("responsibilities", "requirements")
    )
    items: dict[str, dict] = {}
    for section in sections:
        for item in profile.get(section) or []:
            item_id = item.get("item_id")
            if item_id:
                items[item_id] = item
    return items


def _item_quote(item: dict) -> str:
    evidence = item.get("evidence") or {}
    quote = evidence.get("quote")
    return quote if isinstance(quote, str) else ""


def validate_anchor(
    anchor: Anchor | dict,
    *,
    snapshots: dict[str, object],
    items_by_snapshot: dict[str, dict[str, dict]],
    error_code: str = "EVIDENCE_INVALID",
) -> None:
    snapshot_id = str(anchor["snapshot_id"] if isinstance(anchor, dict) else anchor.snapshot_id)
    item_id = anchor["item_id"] if isinstance(anchor, dict) else anchor.item_id
    quote = anchor["quote"] if isinstance(anchor, dict) else anchor.quote
    snapshot = snapshots.get(snapshot_id)
    if snapshot is None:
        raise AppError(error_code, "题目或证据引用了不属于本场的材料", [snapshot_id])
    item = items_by_snapshot.get(snapshot_id, {}).get(item_id)
    if item is None:
        raise AppError(error_code, "引用的条目不在材料结构中", [item_id])
    item_quote = _item_quote(item)
    if not item_quote or quote not in item_quote:
        raise AppError(
            error_code,
            "引用原文不属于所引用的材料条目",
            [item_id, quote[:40]],
        )
    if quote not in (getattr(snapshot, "text", "") or ""):
        raise AppError(error_code, "引用原文在当前材料中不存在", [item_id])


def validate_match_rows(
    rows: list[dict], resume, jd, *, error_code: str = "AI_INVALID_OUTPUT"
) -> list[dict]:
    """匹配结论与简历证据的统一校验；返回规范化结果。"""
    requirement_ids = {
        item["item_id"] for item in (jd.profile.get("requirements") or [])
    }
    items = profile_items(resume.profile)
    snapshots = {str(resume.id): resume}
    items_by_snapshot = {str(resume.id): items}
    seen: set[str] = set()
    result: list[dict] = []
    for row in rows:
        model = MatchRow.model_validate(row)
        if model.requirement_id not in requirement_ids:
            raise AppError(error_code, "匹配结果引用了不存在的 JD 要求")
        if model.requirement_id in seen:
            raise AppError(error_code, "匹配结果要求重复")
        seen.add(model.requirement_id)
        if model.status == "supported" and not model.resume_evidence:
            raise AppError(
                error_code,
                "supported 匹配必须有至少一条有效简历证据",
                [model.requirement_id],
            )
        if model.status == "not_evidenced" and model.resume_evidence:
            raise AppError(
                error_code,
                "not_evidenced 匹配不能附带支持证据",
                [model.requirement_id],
            )
        for anchor in model.resume_evidence:
            validate_anchor(
                anchor,
                snapshots=snapshots,
                items_by_snapshot=items_by_snapshot,
                error_code=error_code,
            )
        result.append(model.model_dump(mode="json"))
    missing = requirement_ids - seen
    if missing:
        raise AppError(
            error_code, "匹配结果未覆盖全部 JD 要求", details=sorted(missing)[:5]
        )
    return result
