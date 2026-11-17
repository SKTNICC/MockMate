"""运维命令：目录种子、任务回收、自检。

用法：
    python -m app.cli seed                 # 正式目录 + 待审题库（默认）
    python -m app.cli seed --mode test     # 隔离测试：test_only 题库与合成审核记录
    python -m app.cli reap                 # 回收过期租约
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models import BankQuestion, ContentReview, FactCard, JdTemplate, Role, SkillCode
from app.services.bank import content_digest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
FIXTURES = REPO_ROOT / "fixtures" / "m1"


def _load(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():
        raise SystemExit(f"缺少样例文件: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def seed_catalog(db: Session, sample_enabled: bool) -> None:
    data = _load("catalog.json")
    for code in data.get("skill_codes", []):
        row = db.get(SkillCode, code)
        if row is None:
            row = SkillCode(code=code)
            db.add(row)
        row.label = code
        row.enabled = True
    for role in data.get("roles", []):
        row = db.get(Role, role["id"])
        if row is None:
            row = Role(id=role["id"])
            db.add(row)
        row.name = role["name"]
        row.levels = list(role["levels"])
        row.sample_only = bool(role.get("sample_only", True))
        row.enabled = bool(role.get("enabled", False) or (sample_enabled and row.sample_only))
    for template in data.get("jd_templates", []):
        row = db.get(JdTemplate, (template["id"], template["version"]))
        if row is None:
            row = JdTemplate(id=template["id"], version=template["version"])
            db.add(row)
        row.role_id = template["role_id"]
        row.level = template["level"]
        row.title = template["title"]
        row.label = template["label"]
        row.text = template["text"]
        row.profile = template["profile"]
        row.enabled = True
    db.commit()
    print(f"catalog: {len(data.get('roles', []))} 个岗位、{len(data.get('jd_templates', []))} 个模板（sample_enabled={sample_enabled}）")


def _upsert_bank(db: Session, question: dict, *, approved_test_only: bool) -> BankQuestion:
    content = copy.deepcopy(question)
    if approved_test_only:
        content["status"] = "approved"
        content["answer_status"] = "verified"
        content["license_status"] = "clear"
        content["evaluation"]["answer_status"] = "verified"
        content["evaluation"]["trust"] = "reviewed"
    digest = content_digest(content)
    content["content_sha256"] = digest
    row = db.get(BankQuestion, (question["question_id"], question["version"]))
    if row is None:
        row = BankQuestion(question_id=question["question_id"], version=question["version"])
        db.add(row)
    row.canonical_id = question["canonical_id"]
    row.status = content["status"]
    row.role_ids = list(question["role_ids"])
    row.levels = list(question["levels"])
    row.content = content
    row.source_digest = digest
    row.test_only = approved_test_only
    return row


def _upsert_fact_card(db: Session, card: dict, *, approved_test_only: bool) -> FactCard:
    content = copy.deepcopy(card)
    if approved_test_only:
        content["status"] = "approved"
        content["reviewer_ids"] = ["TEST-ONLY-A", "TEST-ONLY-B"]
        # 仅限隔离测试种子：合成内容显式把来源许可标为 clear，仍由 test_only 阻断生产选题。
        content["license_status"] = "clear"
    digest = content_digest(content)
    content["content_sha256"] = digest
    row = db.get(FactCard, (card["id"], card["version"]))
    if row is None:
        row = FactCard(id=card["id"], version=card["version"])
        db.add(row)
    row.content = content
    row.content_sha256 = digest
    row.status = content["status"]
    row.test_only = approved_test_only
    return row


def _add_review(db: Session, *, resource_type: str, resource_id: str, version: int, digest: str, reviewer: str) -> None:
    existing = db.scalars(
        select(ContentReview).where(
            ContentReview.resource_type == resource_type,
            ContentReview.resource_id == resource_id,
            ContentReview.version == version,
            ContentReview.reviewer_id == reviewer,
            ContentReview.content_sha256 == digest,
        )
    ).one_or_none()
    if existing is None:
        db.add(
            ContentReview(
                resource_type=resource_type,
                resource_id=resource_id,
                version=version,
                reviewer_id=reviewer,
                reviewer_kind="synthetic_test",
                decision="approve",
                reviewed_at=datetime.now(timezone.utc),
                note="TEST ONLY：合成审核记录，不是真实人工批准",
                content_sha256=digest,
            )
        )


def seed_content(db: Session, *, test_mode: bool) -> None:
    bank = _load("bank-candidates.json")
    for question in bank.get("questions", []):
        row = _upsert_bank(db, question, approved_test_only=test_mode)
        if test_mode:
            for reviewer in ("TEST-ONLY-A", "TEST-ONLY-B"):
                _add_review(
                    db,
                    resource_type="bank_question",
                    resource_id=question["question_id"],
                    version=question["version"],
                    digest=row.content["content_sha256"],
                    reviewer=reviewer,
                )
    facts = _load("fact-cards.json")
    for card in facts.get("cards", []):
        row = _upsert_fact_card(db, card, approved_test_only=test_mode)
        if test_mode:
            for reviewer in ("TEST-ONLY-A", "TEST-ONLY-B"):
                _add_review(
                    db,
                    resource_type="fact_card",
                    resource_id=card["id"],
                    version=card["version"],
                    digest=row.content_sha256,
                    reviewer=reviewer,
                )
    ledger = _load("content-reviews.json")
    for decision in ledger.get("decisions", []):
        # 真实审核记录只能由负责人写入独立 ledger；此处只导入 human 记录。
        if decision.get("reviewer_id", "").startswith("TEST-ONLY"):
            continue
        db.add(
            ContentReview(
                resource_type=decision["resource_type"],
                resource_id=decision["resource_id"],
                version=decision["version"],
                reviewer_id=decision["reviewer_id"],
                reviewer_kind="human",
                decision=decision["decision"],
                reviewed_at=datetime.fromisoformat(decision["reviewed_at"].replace("Z", "+00:00")),
                note=decision["note"],
                content_sha256=decision["content_sha256"],
            )
        )
    db.commit()
    approved = db.scalars(
        select(BankQuestion).where(BankQuestion.status == "approved")
    ).all()
    human_approved = [row for row in approved if not row.test_only]
    print(
        f"content: 题库 {len(bank.get('questions', []))} 条候选；approved={len(approved)}"
        f"（其中 test_only={len(approved) - len(human_approved)}，生产可用={len(human_approved)}）"
    )
    if test_mode:
        print("注意：本次写入 test_only 题库与合成审核记录，仅用于隔离测试，不是真实人工审核。")


def cmd_seed(args: argparse.Namespace) -> int:
    settings = get_settings()
    mode = args.mode
    if mode == "test" and not (settings.allow_test_fixtures and not settings.is_production):
        print("拒绝：test 模式需要 ALLOW_TEST_FIXTURES=true 且非生产环境", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        seed_catalog(db, settings.sample_catalog_enabled)
        seed_content(db, test_mode=(mode == "test"))
    finally:
        db.close()
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    from app.services.jobs import reap_expired_leases

    db = SessionLocal()
    try:
        count = reap_expired_leases(db)
    finally:
        db.close()
    print(f"reaped {count} expired leases")
    return 0


def _read_json(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"文件不存在: {file}")
    return json.loads(file.read_text(encoding="utf-8"))


def cmd_import_content(args: argparse.Namespace) -> int:
    from app.services.content_import import import_content

    settings = get_settings()
    if settings.is_production and settings.allow_test_fixtures:
        print("拒绝：生产环境不能启用测试 fixture", file=sys.stderr)
        return 2
    bank = _read_json(args.bank)
    facts = _read_json(args.fact_cards)
    reviews = _read_json(args.reviews)
    db = SessionLocal()
    try:
        result = import_content(
            db,
            bank_data=bank,
            fact_data=facts,
            reviews_data=reviews,
            settings=settings,
            allow_test_fixtures=settings.allow_test_fixtures and not settings.is_production,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if hasattr(exc, "code"):
            print(f"导入失败：{exc.code} {getattr(exc, 'message', str(exc))}", file=sys.stderr)
            details = getattr(exc, "details", None)
            if details:
                print("详情：" + "; ".join(details), file=sys.stderr)
            return 1
        raise
    finally:
        db.close()
    print(
        "content import: "
        f"bank created={result.bank_created} updated={result.bank_updated} unchanged={result.bank_unchanged} approved={result.approved_bank}; "
        f"fact created={result.fact_created} updated={result.fact_updated} unchanged={result.fact_unchanged} approved={result.approved_fact}; "
        f"reviews created={result.reviews_created} unchanged={result.reviews_unchanged}"
    )
    return 0


def cmd_review_packet(args: argparse.Namespace) -> int:
    from app.services.content_import import build_review_packet

    bank = _read_json(args.bank)
    facts = _read_json(args.fact_cards)
    reviews = _read_json(args.reviews)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    build_review_packet(
        bank_data=bank,
        fact_data=facts,
        reviews_data=reviews,
        output_path=output,
    )
    print(f"review packet written: {output}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_startup()
    from sqlalchemy import text

    db = SessionLocal()
    try:
        version = db.execute(text("select version()")).scalar_one()
    finally:
        db.close()
    print(f"config ok；ai_provider={settings.ai_provider}；db={version.split(',')[0]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mockmate-cli")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="导入岗位目录、内置 JD 与题库/依据卡候选")
    seed.add_argument("--mode", choices=["auto", "production", "test"], default="auto")
    seed.set_defaults(func=cmd_seed)

    reap = sub.add_parser("reap", help="回收过期租约")
    reap.set_defaults(func=cmd_reap)

    import_content = sub.add_parser("import-content", help="导入正式题库／依据卡／人工审核 ledger")
    import_content.add_argument("--bank", required=True)
    import_content.add_argument("--fact-cards", required=True)
    import_content.add_argument("--reviews", required=True)
    import_content.set_defaults(func=cmd_import_content)

    packet = sub.add_parser("review-packet", help="生成待人工核对包（不批准任何内容）")
    packet.add_argument("--bank", required=True)
    packet.add_argument("--fact-cards", required=True)
    packet.add_argument("--reviews", required=True)
    packet.add_argument("--output", required=True)
    packet.set_defaults(func=cmd_review_packet)

    check = sub.add_parser("check", help="配置与数据库自检")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if getattr(args, "mode", None) == "auto":
        args.mode = "test" if get_settings().allow_test_fixtures else "production"
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
