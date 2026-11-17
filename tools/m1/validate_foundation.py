import json
import re
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from build_contracts import content_digest, outputs


ROOT = Path(__file__).resolve().parents[2]


def read(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def normalize_stem_v1(stem):
    return "".join(c for c in unicodedata.normalize("NFKC", stem).casefold() if c.isalnum())


def bigrams(stem):
    normalized = normalize_stem_v1(stem)
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)} if len(normalized) > 1 else {normalized}


def similar(left, right):
    a, b = bigrams(left), bigrams(right)
    return len(a & b) / len(a | b) if a | b else 1.0


def validator(schema, name):
    selected = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{name}"}
    return Draft202012Validator(selected, format_checker=FormatChecker())


def profile_items(profile):
    fields = ("education", "work_experience", "projects", "skills") if profile["kind"] == "resume" else ("responsibilities", "requirements")
    return [item for field in fields for item in profile[field]]


def check_snapshot(snapshot):
    items = profile_items(snapshot["profile"])
    assert items, "材料不能四项全空"
    assert snapshot["kind"] == snapshot["profile"]["kind"], "kind不一致"
    assert len({item["item_id"] for item in items}) == len(items), "重复item_id"
    for item in items:
        assert item["evidence"]["quote"] in snapshot["text"], f"引用不在原文：{item['item_id']}"


def approved_reviewers(plan, resource_type, resource_id, version, content_hash):
    decisions = [r for r in plan["content_reviews"] if r["resource_type"] == resource_type
                 and r["resource_id"] == resource_id and r["version"] == version
                 and r["content_sha256"] == content_hash]
    if any(r["decision"] == "reject" for r in decisions):
        return set()
    return {r["reviewer_id"] for r in decisions if r["decision"] == "approve"}


def plan_error(plan, schema):
    # 仅检查合成结构与声明的不变量；不是生产计划服务或真实语义去重器。
    if list(validator(schema, "InternalPlan").iter_errors(plan)):
        return "SCHEMA_INVALID"
    questions = plan["questions"]
    count = plan["main_question_count"]
    actual = {source: sum(q["source"] == source for q in questions) for source in ("bank", "generated")}
    if len(questions) != count or actual != {"bank": count // 2, "generated": count // 2} or plan["source_counts"] != actual:
        return "SOURCE_QUOTA"
    if [q["position"] for q in questions] != list(range(1, count + 1)):
        return "POSITION_INVALID"
    snapshots = {s["id"]: s for s in plan["material_snapshots"]}
    if set(snapshots) != {plan["resume_snapshot_id"], plan["jd_snapshot_id"]}:
        return "EVIDENCE_INVALID"
    if snapshots[plan["resume_snapshot_id"]]["kind"] != "resume" or snapshots[plan["jd_snapshot_id"]]["kind"] != "jd":
        return "EVIDENCE_INVALID"
    if plan["semantic_review"]["duplicate_pairs"]:
        return "DUPLICATE_QUESTION"
    if plan["semantic_review"]["uncertain"]:
        return "DEDUP_UNCERTAIN"
    if plan["semantic_review"]["grounding_issues"]:
        return "GROUNDING_INVALID"
    bank_sources = {(b["question_id"], b["version"]): b for b in plan["bank_source_snapshots"]}
    facts = {card["id"]: card for card in plan["fact_cards"]}
    seen_ids, seen_canonical, seen_stems, seen_bank = set(), set(), set(), set()
    for index, question in enumerate(questions):
        normalized = normalize_stem_v1(question["stem"])
        if question["id"] in seen_ids or question["canonical_id"] in seen_canonical or normalized in seen_stems:
            return "DUPLICATE_QUESTION"
        if any(similar(question["stem"], old["stem"]) >= 0.70 for old in questions[:index]):
            return "DUPLICATE_QUESTION"
        seen_ids.add(question["id"])
        seen_canonical.add(question["canonical_id"])
        seen_stems.add(normalized)
        if question["source"] == "generated" and question["bank_ref"] is not None:
            return "GENERATED_REUSE"
        if question["source"] == "bank":
            bank_ref = question["bank_ref"]
            if bank_ref is None:
                return "BANK_INELIGIBLE"
            bank_id = (bank_ref["question_id"], bank_ref["version"])
            if bank_id in seen_bank:
                return "DUPLICATE_QUESTION"
            seen_bank.add(bank_id)
            if bank_ref["canonical_id"] != question["canonical_id"]:
                return "BANK_INELIGIBLE"
            bank_source = bank_sources.get(bank_id)
            if bank_source is None or content_digest(bank_source) != bank_ref["content_sha256"] or bank_source["content_sha256"] != bank_ref["content_sha256"]:
                return "BANK_INELIGIBLE"
            if bank_source["status"] != "approved" or bank_source["answer_status"] != "verified" or bank_source["license_status"] != "clear":
                return "BANK_INELIGIBLE"
            reviewers = approved_reviewers(plan, "bank_question", bank_id[0], bank_id[1], bank_ref["content_sha256"])
            if len(reviewers) < 2 or set(bank_ref["reviewer_ids"]) != reviewers:
                return "BANK_INELIGIBLE"
            for field in ("stem", "canonical_id", "focus", "topic_key", "skill_codes", "evaluation"):
                if question[field] != bank_source[field]:
                    return "BANK_INELIGIBLE"
        for anchor in question["anchors"]:
            snapshot = snapshots.get(anchor["snapshot_id"])
            if snapshot is None:
                return "EVIDENCE_INVALID"
            matching = [item for item in profile_items(snapshot["profile"]) if item["item_id"] == anchor["item_id"]]
            if not matching or anchor["quote"] not in snapshot["text"] or anchor["quote"] not in matching[0]["evidence"]["quote"]:
                return "EVIDENCE_INVALID"
        evaluation = question["evaluation"]
        if abs(sum(point["weight"] for point in evaluation["key_points"]) - 1) > 1e-6:
            return "EVALUATION_INVALID"
        if abs(sum(d["weight"] for d in evaluation["rubric"]["dimensions"]) - 1) > 1e-6:
            return "EVALUATION_INVALID"
        if question["source"] == "generated" and (evaluation["answer_status"] != "model_generated" or evaluation["trust"] != "provisional"):
            return "EVALUATION_INVALID"
        if evaluation["uncertainties"]:
            return "EVALUATION_INVALID"
        for basis in evaluation["basis"]:
            if basis["kind"] == "fact_card":
                card = facts.get(basis["ref_id"])
                if card is None or card["status"] != "approved" or content_digest(card) != card["content_sha256"]:
                    return "EVALUATION_INVALID"
                if len(approved_reviewers(plan, "fact_card", card["id"], card["version"], card["content_sha256"])) < 2:
                    return "EVALUATION_INVALID"
            elif basis["kind"] == "material":
                if basis["ref_id"] not in {f"{a['snapshot_id']}/{a['item_id']}" for a in question["anchors"]}:
                    return "EVALUATION_INVALID"
    if plan["state"] == "confirmed" and plan["confirmed_at"] is None:
        return "PLAN_STATE_INVALID"
    if plan["state"] == "ready" and plan["confirmed_at"] is not None:
        return "PLAN_STATE_INVALID"
    return None


def inspect_refs(value, schema_names):
    if isinstance(value, dict):
        if "$ref" in value:
            target = value["$ref"]
            assert target.startswith("#/components/schemas/"), f"非预期ref：{target}"
            assert target.rsplit("/", 1)[-1] in schema_names, f"未定义ref：{target}"
        for item in value.values():
            inspect_refs(item, schema_names)
    elif isinstance(value, list):
        for item in value:
            inspect_refs(item, schema_names)


def main():
    expected_outputs = outputs()
    for relative, expected in expected_outputs.items():
        assert read(relative) == expected, f"生成物不同步：{relative}"
    schema = read("contracts/m1/m1.schema.json")
    Draft202012Validator.check_schema(schema)
    for name in schema["$defs"]:
        Draft202012Validator.check_schema({"$defs": schema["$defs"], **schema["$defs"][name]})
    api = read("contracts/m1/openapi.json")
    inspect_refs(api, set(api["components"]["schemas"]))
    operation_ids = []
    operations = 0
    for path, methods in api["paths"].items():
        for method, operation in methods.items():
            operations += 1
            operation_ids.append(operation["operationId"])
            placeholders = set(re.findall(r"\{([^}]+)\}", path))
            assert placeholders == {p["name"] for p in operation["parameters"] if p["in"] == "path"}
            if method in ("post", "put", "delete") and not path.startswith("/auth/"):
                assert any(p["name"] == "Idempotency-Key" and p["required"] for p in operation["parameters"])
    assert len(operation_ids) == len(set(operation_ids))
    validate(api)
    assert api["paths"]["/plans/{plan_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("PublicPlan")
    materials = read("fixtures/m1/materials.json")
    for snapshot in materials["snapshots"]:
        validator(schema, "Snapshot").validate(snapshot)
        check_snapshot(snapshot)
    catalog = read("fixtures/m1/catalog.json")
    for role in catalog["roles"]:
        validator(schema, "Role").validate(role)
        assert role["sample_only"] and not role["enabled"], "未审核岗位不应默认开放"
    for template in catalog["jd_templates"]:
        validator(schema, "JdTemplate").validate(template)
    bank = read("fixtures/m1/bank-candidates.json")
    for question in bank["questions"]:
        assert content_digest(question) == question["content_sha256"]
        validator(schema, "BankCandidate").validate(question)
        validator(schema, "Evaluation").validate(question["evaluation"])
        assert question["status"] == "draft" and question["answer_status"] == "model_generated"
    reviews = read("fixtures/m1/content-reviews.json")
    assert reviews["decisions"] == [], "本轮不能凭空出现人工审核"
    facts = read("fixtures/m1/fact-cards.json")
    assert all(card["status"] == "draft" and not card["reviewer_ids"] for card in facts["cards"])
    for card in facts["cards"]:
        validator(schema, "FactCard").validate(card)
        assert content_digest(card) == card["content_sha256"]
    internal = read("fixtures/m1/internal-plan.example.json")
    public = read("fixtures/m1/public-plan.example.json")
    validator(schema, "PublicPlan").validate(public)
    projection = {key: internal[key] for key in schema["$defs"]["PublicPlan"]["properties"]}
    assert projection == public
    assert internal["test_only"] is True and internal["model_id"] == "SYNTHETIC-NO-AI-CALL"
    assert not {"questions", "stem", "evaluation", "key_points", "rubric", "reference_approach"} & public.keys()
    assert plan_error(internal, schema) is None
    cases = read("fixtures/m1/rule-cases.json")["cases"]
    for case in cases:
        actual = plan_error(case["plan"], schema)
        assert actual == case["expected_error"], f"{case['id']}: expected={case['expected_error']}, actual={actual}"

    m2 = read("fixtures/m1/m2-examples.json")
    assert m2["test_only"] is True
    for name in ("start_request",):
        validator(schema, "StartInterviewInput").validate(m2[name])
    for name in ("start_created", "start_reopened"):
        validator(schema, "InterviewStartReceipt").validate(m2[name])
    validator(schema, "InterviewAnswerInput").validate(m2["answer_request"])
    for name in ("answer_receipt_main", "answer_receipt_follow_up"):
        validator(schema, "InterviewReceipt").validate(m2[name])
    for name in ("detail_active", "detail_deciding", "detail_ended"):
        validator(schema, "InterviewDetail").validate(m2[name])
    validator(schema, "InterviewHistory").validate(m2["history"])
    for name in ("report_full", "report_partial", "report_basic", "report_no_answers"):
        validator(schema, "InterviewReport").validate(m2[name])
    validator(schema, "ErrorResponse").validate(m2["error_report_pending"])
    validator(schema, "Job").validate(m2["job_follow_failed"])
    validator(schema, "Job").validate(m2["job_report_failed"])
    assert m2["job_follow_failed"]["kind"] == "follow_up" and m2["job_follow_failed"]["retryable"]
    assert m2["job_report_failed"]["kind"] == "interview_report"
    for name in ("detail_active", "detail_deciding", "detail_ended", "report_full",
                 "report_partial", "report_basic", "report_no_answers"):
        serialized = json.dumps(m2[name], ensure_ascii=False)
        for forbidden in ("reference_approach", "key_points", "plan_snapshot",
                          "evaluation", "acceptable_alternatives"):
            assert forbidden not in serialized, f"{name} 泄露内部字段 {forbidden}"
    assert m2["report_no_answers"]["quality"] == "no_answers"
    assert m2["report_basic"]["quality"] == "unscored"
    assert m2["report_partial"]["questions"][0]["question_score"] is None
    assert any(path.startswith("/interviews") for path in api["paths"])
    assert api["paths"]["/interviews/{session_id}/answers"]["post"]["responses"].keys() >= {"200", "202"}
    print(f"PASS: {len(schema['$defs'])} schemas, {operations} API operations, 4 material snapshots, 6 draft candidates, {len(cases)} rule vectors, M2 examples")
    print("Scope: foundation checks only; no backend, PostgreSQL, Android, human approval or real AI verified.")


if __name__ == "__main__":
    main()
