import argparse
import copy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-11-16T00:00:00Z"


def uid(number):
    return f"00000000-0000-4000-8000-{number:012d}"


def obj(properties, required=None):
    return {
        "type": "object", "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def string(min_length=1, **kwargs):
    return {"type": "string", "minLength": min_length, **kwargs}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def array(item, minimum=0):
    return {"type": "array", "items": item, "minItems": minimum}


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def digest(value):
    # 审核绑定的规范序列化：UTF-8、键排序、无额外空格。
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def content_digest(value):
    content = copy.deepcopy(value)
    for key in ("status", "answer_status", "content_sha256", "reviewer_ids"):
        content.pop(key, None)
    if "evaluation" in content:
        content["evaluation"].pop("answer_status", None)
        content["evaluation"].pop("trust", None)
    return digest(content)


def build_schema():
    s = {}
    ident = {"type": "string", "format": "uuid"}
    date = {"type": "string", "format": "date-time"}
    revision = {"type": "integer", "minimum": 1}
    text = string(maxLength=20000)
    count = {"type": "integer", "enum": [2, 4, 6]}
    level = enum("intern", "junior")
    strings = array(string())
    s["Evidence"] = obj({"quote": string(), "origin": enum("extracted", "user_confirmed")})
    s["Anchor"] = obj({"snapshot_id": ident, "item_id": string(), "quote": string()})
    s["Education"] = obj({"item_id": string(), "degree": nullable(string()),
                          "major": nullable(string()), "evidence": ref("Evidence")})
    s["Experience"] = obj({"item_id": string(), "organization": string(),
                           "title": string(), "description": string(), "evidence": ref("Evidence")})
    s["Project"] = obj({"item_id": string(), "name": string(), "description": string(),
                        "skill_codes": strings, "evidence": ref("Evidence")})
    s["Skill"] = obj({"item_id": string(), "name": string(), "code": nullable(string()),
                      "evidence": ref("Evidence")})
    s["ResumeProfile"] = obj({"kind": {"const": "resume"},
                              "education": array(ref("Education")),
                              "work_experience": array(ref("Experience")),
                              "projects": array(ref("Project")), "skills": array(ref("Skill"))})
    s["Responsibility"] = obj({"item_id": string(), "text": string(), "evidence": ref("Evidence")})
    s["Requirement"] = obj({"item_id": string(), "text": string(), "skill_codes": strings,
                            "importance": enum("must", "nice"), "evidence": ref("Evidence")})
    s["JdProfile"] = obj({"kind": {"const": "jd"}, "role_id": string(), "level": level,
                          "title": string(), "responsibilities": array(ref("Responsibility"), 1),
                          "requirements": array(ref("Requirement"), 1)})
    s["Profile"] = {"oneOf": [ref("ResumeProfile"), ref("JdProfile")]}
    s["MaterialDraft"] = obj({"id": ident, "revision": revision, "kind": enum("resume", "jd"),
                              "title": string(maxLength=120), "text": nullable(text),
                              "profile": nullable(ref("Profile")),
                              "state": enum("uploaded", "text_ready", "parsed"),
                              "template_id": nullable(string()), "template_version": nullable(revision),
                              "latest_job_id": nullable(ident), "created_at": date, "updated_at": date})
    s["Snapshot"] = obj({"id": ident, "material_id": ident, "source_revision": revision,
                         "kind": enum("resume", "jd"), "title": string(maxLength=120), "text": text,
                         "profile": ref("Profile"), "template_id": nullable(string()),
                         "template_version": nullable(revision), "confirmed_at": date})
    s["TextMaterialInput"] = obj({"kind": enum("resume", "jd"), "title": string(maxLength=120),
                                  "text": text, "template_id": nullable(string()),
                                  "template_version": nullable(revision)})
    s["FileMaterialInput"] = obj({"kind": enum("resume", "jd"), "title": string(maxLength=120),
                                  "file": {"type": "string", "format": "binary"}})
    s["EditMaterialInput"] = obj({"revision": revision, "title": string(maxLength=120), "text": text})
    s["ConfirmMaterialInput"] = obj({"revision": revision, "profile": ref("Profile")})
    s["RevisionInput"] = obj({"revision": revision})
    s["AiJobInput"] = obj({"revision": revision, "external_processing_consent": {"type": "boolean"}})
    s["ParseMaterialInput"] = obj({"revision": revision, "external_processing_consent": {"type": "boolean"},
                                   "role_id": string(), "level": level})
    s["RetryInput"] = obj({"expected_attempt": {"type": "integer", "minimum": 1, "maximum": 2}})
    prep_config = {"role_id": string(), "level": level, "main_question_count": count,
                   "resume_snapshot_id": nullable(ident), "jd_snapshot_id": nullable(ident)}
    s["CreatePreparationInput"] = obj(prep_config)
    s["EditPreparationInput"] = obj({"revision": revision, **prep_config})
    s["Preparation"] = obj({"id": ident, "revision": revision, **prep_config,
                            "match_report_id": nullable(ident), "current_plan_id": nullable(ident),
                            "created_at": date, "updated_at": date})
    s["MatchRow"] = obj({"requirement_id": string(),
                         "status": enum("supported", "to_verify", "not_evidenced"),
                         "resume_evidence": array(ref("Anchor")), "explanation": string(),
                         "suggested_focus": string()})
    s["MatchRowsOutput"] = obj({"rows": array(ref("MatchRow"), 1)})
    s["MatchReport"] = obj({"id": ident, "preparation_id": ident, "input_revision": revision,
                            "resume_snapshot_id": ident, "jd_snapshot_id": ident,
                            "rows": array(ref("MatchRow"), 1), "created_at": date})
    s["KeyPoint"] = obj({"point": string(), "weight": {"type": "number", "exclusiveMinimum": 0,
                                                              "maximum": 1}})
    s["Basis"] = obj({"kind": enum("material", "fact_card", "reasoning"),
                      "ref_id": string(), "note": string()})
    s["RubricDimension"] = obj({"id": string(), "name": string(),
                                "weight": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                                "low_anchor": string(), "high_anchor": string()})
    s["Rubric"] = obj({"id": string(), "version": revision,
                       "dimensions": array(ref("RubricDimension"), 1)})
    s["Evaluation"] = obj({"goal": string(), "reference_approach": string(),
                           "key_points": array(ref("KeyPoint"), 2),
                           "acceptable_alternatives": strings, "common_mistakes": strings,
                           "rubric": ref("Rubric"), "basis": array(ref("Basis"), 1),
                           "answer_status": enum("verified", "model_generated"),
                           "trust": enum("reviewed", "provisional"), "uncertainties": strings})
    sha = string(pattern="^[a-f0-9]{64}$")
    s["BankCandidate"] = obj({"question_id": string(), "version": revision, "canonical_id": string(),
                              "status": enum("draft", "review", "approved", "deprecated"),
                              "answer_status": enum("model_generated", "verified"), "title": string(),
                              "role_ids": array(string(), 1), "levels": array(level, 1), "stem": string(),
                              "focus": string(), "topic_key": string(), "skill_codes": strings,
                              "evaluation": ref("Evaluation"), "provenance": {"const": "authored"},
                              "source_urls": array({"type": "string", "format": "uri"}, 1),
                              "license_status": enum("pending_owner_review", "clear"), "content_sha256": sha})
    s["FactCard"] = obj({"id": string(), "version": revision, "status": enum("draft", "approved"),
                         "reviewer_ids": strings, "url": {"type": "string", "format": "uri"},
                         "summary": string(),
                         "license_status": enum("pending_owner_review", "clear"),
                         "content_sha256": sha})
    s["ReviewDecision"] = obj({"resource_type": enum("bank_question", "fact_card"), "resource_id": string(),
                               "version": revision, "content_sha256": sha, "reviewer_id": string(),
                               "decision": enum("approve", "reject"), "reviewed_at": date, "note": string()})
    s["BankRef"] = obj({"question_id": string(), "version": revision, "canonical_id": string(),
                        "review_status": {"const": "approved"}, "answer_status": {"const": "verified"},
                        "reviewer_ids": {"type": "array", "items": string(), "minItems": 2,
                                         "uniqueItems": True},
                        "content_sha256": string(pattern="^[a-f0-9]{64}$"),
                        "license_status": {"const": "clear"}})
    question_props = {"id": ident, "position": {"type": "integer", "minimum": 1, "maximum": 6},
                      "source": enum("bank", "generated"), "bank_ref": nullable(ref("BankRef")),
                      "question_type": enum("experience", "knowledge"),
                      "canonical_id": string(), "stem": string(min_length=8, maxLength=500),
                      "focus": string(), "topic_key": string(), "skill_codes": strings,
                      "anchors": array(ref("Anchor"), 1), "evaluation": ref("Evaluation")}
    s["InternalQuestion"] = obj(question_props)
    s["GeneratedQuestionOutput"] = obj({k: v for k, v in question_props.items()
                                         if k not in ("id", "position", "bank_ref", "source")})
    s["GroundingIssue"] = obj({"question_id": string(), "claim": string(), "reason": string()})
    s["DuplicateReviewOutput"] = obj({"duplicate_pairs": array(obj({
        "left_id": string(), "right_id": string(), "reason": string()})),
        "grounding_issues": array(ref("GroundingIssue")),
        "uncertain": {"type": "boolean"}})
    public_props = {"id": ident, "preparation_id": ident, "input_revision": revision,
                    "revision": revision, "state": enum("ready", "confirmed"),
                    "role_id": string(), "level": level, "main_question_count": count,
                    "source_counts": obj({"bank": {"type": "integer", "minimum": 1, "maximum": 3},
                                          "generated": {"type": "integer", "minimum": 1, "maximum": 3}}),
                    "resume_snapshot_id": ident, "jd_snapshot_id": ident,
                    "focus_areas": strings, "limitations": strings,
                    "created_at": date, "confirmed_at": nullable(date)}
    s["PublicPlan"] = obj(public_props)
    s["InternalPlan"] = obj({**public_props, "test_only": {"type": "boolean"},
                             "material_snapshots": array(ref("Snapshot"), 2),
                             "bank_source_snapshots": array(ref("BankCandidate"), 1),
                             "fact_cards": array(ref("FactCard")),
                             "content_reviews": array(ref("ReviewDecision")),
                             "questions": array(ref("InternalQuestion"), 2),
                             "prompt_version": string(), "model_id": string(),
                             "semantic_review": obj({"duplicate_pairs": array(obj({
                                 "left_id": string(), "right_id": string(), "reason": string()})),
                                 "grounding_issues": array(ref("GroundingIssue")),
                                 "uncertain": {"type": "boolean"}, "reviewed_at": date})})
    s["ErrorInfo"] = obj({"code": string(), "message": string(), "retryable": {"type": "boolean"},
                          "details": strings, "request_id": ident})
    s["ErrorResponse"] = obj({"error": ref("ErrorInfo")})
    s["ResultRef"] = obj({"entity_type": enum("material", "match_report", "plan",
                                               "interview_session", "interview_report",
                                               "audio_asset", "speech_asset"), "id": ident})
    s["Job"] = obj({"id": ident, "kind": enum("extract", "parse", "match", "plan",
                                               "follow_up", "interview_report",
                                               "audio_asr", "audio_tts"),
                    "target_id": ident, "input_revision": revision,
                    "state": enum("queued", "running", "succeeded", "failed", "interrupted", "superseded"),
                    "attempt": {"type": "integer", "minimum": 1, "maximum": 2},
                    "retryable": {"type": "boolean"}, "result_ref": nullable(ref("ResultRef")),
                    "error": nullable(ref("ErrorInfo")), "created_at": date, "updated_at": date})

    # ------------------------------------------------------------------ M2 文字面试
    s["StartInterviewInput"] = obj({
        "plan_revision": revision, "external_processing_consent": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["text", "voice"], "default": "text"},
        "audio_processing_consent": {"type": "boolean", "default": False},
    }, required=["plan_revision", "external_processing_consent"])
    s["InterviewStartReceipt"] = obj({"session_id": ident, "revision": revision,
                                      "mode": enum("text", "voice")})
    s["InterviewAnswerInput"] = obj({
        "revision": revision, "turn_id": ident,
        "text": string(min_length=1, maxLength=4000),
        "input_source": {"type": "string", "enum": ["text", "asr", "asr_corrected"],
                         "default": "text"},
        "audio_asset_id": nullable(ident),
        "transcript_revision": nullable(revision),
    }, required=["revision", "turn_id", "text"])
    s["InterviewSkipInput"] = obj({"revision": revision, "turn_id": ident})
    s["InterviewContinueInput"] = obj({"revision": revision, "job_id": ident})
    s["InterviewBasicReportInput"] = obj({"revision": revision, "job_id": ident})
    s["InterviewReceipt"] = obj({
        "session_id": ident, "revision": revision,
        "answer_id": nullable(ident), "job_id": nullable(ident)})
    s["InterviewMaterialVersion"] = obj({
        "kind": enum("resume", "jd"), "snapshot_id": ident, "material_id": ident,
        "title": string(maxLength=120), "source_revision": revision})
    s["InterviewPlanSummary"] = obj({
        "plan_id": ident, "main_question_count": count,
        "source_counts": obj({"bank": {"type": "integer", "minimum": 1, "maximum": 3},
                              "generated": {"type": "integer", "minimum": 1, "maximum": 3}}),
        "material_versions": array(ref("InterviewMaterialVersion"), 2),
        "sample_only": {"type": "boolean"}})
    s["InterviewAnswerView"] = obj({
        "answer_id": ident, "text": string(min_length=1, maxLength=4000),
        "submitted_at": date,
        "input_source": enum("text", "asr", "asr_corrected"),
        "audio_asset_id": nullable(ident),
        "transcript_revision": nullable(revision)})
    s["InterviewTurnView"] = obj({
        "turn_id": ident, "question_id": ident,
        "position": {"type": "integer", "minimum": 1, "maximum": 6},
        "kind": enum("main", "follow_up"), "prompt": string(),
        "status": enum("pending", "answered", "skipped", "unanswered"),
        "answer": nullable(ref("InterviewAnswerView"))})
    s["InterviewJobSummary"] = obj({
        "id": ident,
        "state": enum("queued", "running", "succeeded", "failed", "interrupted", "superseded"),
        "attempt": {"type": "integer", "minimum": 1, "maximum": 2},
        "retryable": {"type": "boolean"}, "error_code": nullable(string())})
    actions = enum("answer", "skip", "pause", "resume", "finish",
                   "continue_without_follow_up", "retry_follow_up",
                   "retry_report", "create_basic_report")
    detail_required = [
        "session_id", "plan_id", "preparation_id", "role_id", "level", "mode",
        "status", "phase", "revision", "started_at", "ended_at", "end_reason",
        "plan_summary", "opening_text", "closing_text", "current_main_position",
        "answered_main_count", "skipped_main_count", "current_turn",
        "allowed_actions", "transcript", "follow_up_job", "report_job", "report_state"]
    s["InterviewDetail"] = obj({
        "session_id": ident, "plan_id": ident, "preparation_id": ident,
        "role_id": string(), "level": level, "mode": enum("text", "voice"),
        "status": enum("active", "paused", "ended"),
        "phase": enum("awaiting_answer", "deciding_follow_up", "closed"),
        "revision": revision, "started_at": date, "ended_at": nullable(date),
        "end_reason": nullable(enum("completed", "early_finish")),
        "plan_summary": ref("InterviewPlanSummary"), "opening_text": string(),
        "closing_text": nullable(string()),
        "current_main_position": nullable({"type": "integer", "minimum": 1, "maximum": 6}),
        "answered_main_count": {"type": "integer", "minimum": 0, "maximum": 6},
        "skipped_main_count": {"type": "integer", "minimum": 0, "maximum": 6},
        "current_turn": nullable(ref("InterviewTurnView")),
        "allowed_actions": array(actions),
        "transcript": array(ref("InterviewTurnView")),
        "follow_up_job": nullable(ref("InterviewJobSummary")),
        "report_job": nullable(ref("InterviewJobSummary")),
        "report_state": enum("not_requested", "pending", "failed", "ready"),
    }, detail_required)
    s["InterviewHistoryItem"] = obj({
        "session_id": ident, "plan_id": ident, "preparation_id": ident,
        "role_id": string(), "level": level,
        "mode": enum("text", "voice"),
        "status": enum("active", "paused", "ended"),
        "started_at": date, "ended_at": nullable(date),
        "end_reason": nullable(enum("completed", "early_finish")),
        "main_question_count": {"type": "integer", "minimum": 2, "maximum": 6},
        "answered_main_count": {"type": "integer", "minimum": 0, "maximum": 6},
        "report_state": enum("not_requested", "pending", "failed", "ready")})
    s["InterviewHistory"] = obj({"items": array(ref("InterviewHistoryItem")),
                                 "next_cursor": nullable(string())})
    s["InterviewEvidence"] = obj({"answer_id": ident,
                                  "quote": string(maxLength=240)})
    s["InterviewDimensionResult"] = obj({
        "dimension_id": string(), "name": string(),
        "weight": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
        "verdict": enum("scored", "insufficient_evidence", "uncertain"),
        "score": nullable({"type": "integer", "minimum": 0, "maximum": 4}),
        "evidence": array(ref("InterviewEvidence")),
        "explanation": {"type": "string"}, "improvement": {"type": "string"}})
    s["InterviewReportQuestion"] = obj({
        "question_id": ident,
        "position": {"type": "integer", "minimum": 1, "maximum": 6},
        "source": enum("bank", "generated"),
        "outcome": enum("answered", "skipped", "unanswered", "not_reached"),
        "prompt": nullable(string()), "bank_version": nullable(revision),
        "turns": array(ref("InterviewTurnView")),
        "assessment_trust": enum("reviewed", "provisional"),
        "rubric_version": string(),
        "question_score": nullable({"type": "integer", "minimum": 0, "maximum": 100}),
        "dimensions": array(ref("InterviewDimensionResult")),
        "follow_up_outcome": nullable(enum("not_needed", "asked", "unavailable", "cancelled"))})
    s["InterviewCoverage"] = obj({
        "planned": {"type": "integer", "minimum": 0, "maximum": 6},
        "presented": {"type": "integer", "minimum": 0, "maximum": 6},
        "answered": {"type": "integer", "minimum": 0, "maximum": 6},
        "skipped": {"type": "integer", "minimum": 0, "maximum": 6},
        "unanswered": {"type": "integer", "minimum": 0, "maximum": 6},
        "not_reached": {"type": "integer", "minimum": 0, "maximum": 6},
        "scored": {"type": "integer", "minimum": 0, "maximum": 6},
        "bank_planned": {"type": "integer", "minimum": 0, "maximum": 3},
        "bank_answered": {"type": "integer", "minimum": 0, "maximum": 3},
        "generated_planned": {"type": "integer", "minimum": 0, "maximum": 3},
        "generated_answered": {"type": "integer", "minimum": 0, "maximum": 3},
        "follow_up_presented": {"type": "integer", "minimum": 0, "maximum": 6},
        "follow_up_answered": {"type": "integer", "minimum": 0, "maximum": 6}})
    s["InterviewFinding"] = obj({
        "question_id": ident,
        "position": {"type": "integer", "minimum": 1, "maximum": 6},
        "dimension_id": string(), "text": string(),
        "evidence": array(ref("InterviewEvidence"))})
    s["InterviewReport"] = obj({
        "report_id": ident, "session_id": ident, "role_id": string(), "level": level,
        "material_versions": array(ref("InterviewMaterialVersion"), 2),
        "quality": enum("full", "partial", "no_answers", "unscored"),
        "rubric_policy_version": string(), "created_at": date,
        "end_reason": enum("completed", "early_finish"),
        "coverage": ref("InterviewCoverage"),
        "strengths": array(ref("InterviewFinding")),
        "improvements": array(ref("InterviewFinding")),
        "questions": array(ref("InterviewReportQuestion"))})

    # ------------------------------------------------------------------ M3 语音面试
    s["VoiceConfig"] = obj({
        "available": {"type": "boolean"},
        "max_answer_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
        "format": ref("VoiceFormat"),
        "tts_voice_id": string(),
        "speech_provider": string(),
    })
    s["VoiceFormat"] = {"const": "pcm_s16le/16000/mono"}
    s["VoiceAudioAsset"] = obj({
        "id": ident, "session_id": ident, "turn_id": ident, "take_id": ident,
        "sha256": string(pattern="^[a-f0-9]{64}$"),
        "byte_length": {"type": "integer", "minimum": 2, "maximum": 3840000},
        "duration_ms": {"type": "integer", "minimum": 1, "maximum": 120000},
        "format": ref("VoiceFormat"),
        "status": enum("stored", "expired"),
        "expires_at": date, "created_at": date,
    })
    s["VoiceTranscript"] = obj({
        "audio_asset_id": ident, "revision": revision,
        "status": enum("processing", "ready", "empty", "failed"),
        "text": nullable({"type": "string", "maxLength": 20000}),
        "provider": nullable(string()), "request_id": nullable(string()),
        "error_code": nullable(string()), "updated_at": date,
    })
    s["VoiceDraft"] = obj({
        "session_id": ident, "turn_id": ident,
        "voice_revision": {"type": "integer", "minimum": 0},
        "active_audio": nullable(ref("VoiceAudioAsset")),
        "transcript": nullable(ref("VoiceTranscript")),
        "asr_job": nullable(ref("InterviewJobSummary")),
    })
    s["VoiceUploadInput"] = obj({
        "file": {"type": "string", "format": "binary"},
        "take_id": ident,
        "session_revision": revision,
        "expected_voice_revision": {"type": "integer", "minimum": 0},
        "sha256": string(pattern="^[a-f0-9]{64}$"),
        "format": ref("VoiceFormat"),
    })
    s["VoiceUploadReceipt"] = obj({
        "session_id": ident, "turn_id": ident,
        "voice_revision": revision,
        "audio": ref("VoiceAudioAsset"),
    })
    s["TranscribeInput"] = obj({
        "expected_voice_revision": {"type": "integer", "minimum": 0}})
    s["SpeechRequestInput"] = obj({
        "segment": enum("opening", "turn", "closing"),
        "turn_id": nullable(ident),
    }, required=["segment"])
    s["SpeechAsset"] = obj({
        "id": ident, "session_id": ident,
        "segment": enum("opening", "turn", "closing"),
        "turn_id": nullable(ident),
        "status": enum("queued", "generating", "ready", "failed", "expired"),
        "voice_id": string(), "provider": string(),
        "format": string(),
        "duration_ms": nullable({"type": "integer", "minimum": 1}),
        "sha256": nullable(string(pattern="^[a-f0-9]{64}$")),
        "job_id": nullable(ident), "error_code": nullable(string()),
        "synthetic": {"type": "boolean"},
        "created_at": date, "updated_at": date,
    })

    s["User"] = obj({"id": ident, "username": string(pattern="^[a-z0-9_]{3,32}$")})
    s["Credentials"] = obj({"username": string(pattern="^[a-zA-Z0-9_]{3,32}$"),
                            "password": string(min_length=10, maxLength=128)})
    s["TokenResponse"] = obj({"access_token": string(), "token_type": {"const": "bearer"},
                              "expires_at": date, "user": ref("User")})
    s["Role"] = obj({"id": string(), "name": string(), "levels": array(level, 1),
                     "enabled": {"type": "boolean"}, "sample_only": {"type": "boolean"}})
    s["SkillCode"] = obj({"code": string(), "label": string()})
    s["JdTemplate"] = obj({"id": string(), "version": revision, "role_id": string(), "level": level,
                           "title": string(), "label": {"const": "内置练习岗位"},
                           "text": text, "profile": ref("JdProfile")})
    for name, item in [("Roles", "Role"), ("SkillCodes", "SkillCode"), ("JdTemplates", "JdTemplate"),
                       ("Materials", "MaterialDraft"), ("Preparations", "Preparation"),
                       ("Snapshots", "Snapshot")]:
        s[name] = obj({"items": array(ref(item))})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://mockmate.example/contracts/m1/schema", "title": "MockMate M1 + M2",
            "$comment": "生成源 tools/m1/build_contracts.py；结构契约不取代业务校验。", "$defs": s}


def openapi_refs(value):
    if isinstance(value, dict):
        return {k: (v.replace("#/$defs/", "#/components/schemas/") if k == "$ref"
                    else openapi_refs(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [openapi_refs(v) for v in value]
    return value


def build_openapi(schema):
    api = {"openapi": "3.1.0", "info": {"title": "MockMate /v1 contract (M1 + M2)", "version": "0.2.0",
            "description": "开发目标，尚未实现。维护源 tools/m1/build_contracts.py。所有私人对象由会话确定owner。"},
           "servers": [{"url": "/v1"}], "security": [{"BearerAuth": []}], "paths": {},
           "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}},
                          "schemas": openapi_refs(schema["$defs"])}}

    def endpoint(path, method, operation, summary, response=None, body=None, status=200,
                 anonymous=False, multipart=False, idempotent=True, query=None,
                 extra_success=None, errors=(400, 401, 404, 409, 413, 415, 422, 429, 502, 503),
                 response_content_type="application/json"):
        params = []
        for segment in path.split("/"):
            if segment.startswith("{"):
                name = segment[1:-1]
                field = {"type": "string"} if name == "template_id" else {"type": "string", "format": "uuid"}
                params.append({"name": name, "in": "path", "required": True, "schema": field})
        for item in query or []:
            params.append(item)
        if method in ("post", "put", "delete") and idempotent:
            params.append({"name": "Idempotency-Key", "in": "header", "required": True,
                           "description": "逻辑操作UUID；相同key与body回放，不同body冲突；重试不得换key。",
                           "schema": {"type": "string", "format": "uuid"}})
        op = {"operationId": operation, "summary": summary, "parameters": params, "responses": {}}
        for code in [status, *(extra_success or [])]:
            success = {"description": "成功；异步接口返回持久化Job，GET轮询结果" if code == 202 else "成功"}
            if response:
                success["content"] = {response_content_type: {"schema": {"$ref": f"#/components/schemas/{response}"}}}
            op["responses"][str(code)] = success
        for code in errors:
            op["responses"][str(code)] = {"description": "统一错误；具体code与语义见M1架构说明",
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}}
        if body:
            op["requestBody"] = {"required": True, "content": {
                "multipart/form-data" if multipart else "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{body}"}}}}
        if anonymous:
            op["security"] = []
        api["paths"].setdefault(path, {})[method] = op

    endpoint("/auth/register", "post", "register", "注册", "User", "Credentials", 201, True, idempotent=False)
    endpoint("/auth/login", "post", "login", "登录", "TokenResponse", "Credentials", anonymous=True, idempotent=False)
    endpoint("/auth/logout", "post", "logout", "撤销当前会话", status=204, idempotent=False)
    endpoint("/me", "get", "getMe", "本人", "User")
    endpoint("/roles", "get", "listRoles", "可用岗位", "Roles")
    endpoint("/skill-codes", "get", "listSkillCodes", "可用技能词表", "SkillCodes")
    endpoint("/jd-templates", "get", "listJdTemplates", "内置JD模板", "JdTemplates")
    endpoint("/jd-templates/{template_id}", "get", "getJdTemplate", "读取模板当前版本", "JdTemplate")
    endpoint("/materials", "get", "listMaterials", "本人材料", "Materials")
    endpoint("/materials/text", "post", "createTextMaterial", "保存文本材料", "MaterialDraft", "TextMaterialInput", 201)
    endpoint("/materials/file", "post", "createFileMaterial", "保存文件，尚不调用AI", "MaterialDraft", "FileMaterialInput", 201, multipart=True)
    endpoint("/materials/{material_id}", "get", "getMaterial", "读取当前材料草稿", "MaterialDraft")
    endpoint("/materials/{material_id}", "put", "editMaterial", "修改原文并清空旧解析", "MaterialDraft", "EditMaterialInput")
    endpoint("/materials/{material_id}", "delete", "deleteMaterial", "删除未被引用材料及快照/原件", status=204)
    endpoint("/materials/{material_id}/extract", "post", "extractMaterial", "本地文件提取任务", "Job", "RevisionInput", 202)
    endpoint("/materials/{material_id}/parse", "post", "parseMaterial", "AI结构化解析任务", "Job", "ParseMaterialInput", 202)
    endpoint("/materials/{material_id}/confirm", "post", "confirmMaterial", "保存核对后的不可变快照", "Snapshot", "ConfirmMaterialInput", 201)
    endpoint("/materials/{material_id}/snapshots", "get", "listMaterialSnapshots", "本人材料的确认版本列表", "Snapshots")
    endpoint("/snapshots/{snapshot_id}", "get", "getSnapshot", "读取本人材料快照", "Snapshot")
    endpoint("/preparations", "post", "createPreparation", "新建可缺材料的准备草稿", "Preparation", "CreatePreparationInput", 201)
    endpoint("/preparations", "get", "listPreparations", "本人准备列表，最多100条", "Preparations")
    endpoint("/preparations/{preparation_id}", "get", "getPreparation", "读取准备及当前实体ID", "Preparation")
    endpoint("/preparations/{preparation_id}", "put", "editPreparation", "修改完整准备配置并失效旧产物指针", "Preparation", "EditPreparationInput")
    endpoint("/preparations/{preparation_id}", "delete", "deletePreparation", "删除准备/计划/匹配并终止相关任务", status=204)
    endpoint("/preparations/{preparation_id}/match", "post", "matchPreparation", "分析已确认的材料", "Job", "AiJobInput", 202)
    endpoint("/match-reports/{report_id}", "get", "getMatchReport", "读取匹配报告", "MatchReport")
    endpoint("/preparations/{preparation_id}/generate-plan", "post", "generatePlan", "生成严格各半的计划", "Job", "AiJobInput", 202)
    endpoint("/plans/{plan_id}", "get", "getPlan", "只读取安全计划摘要，不重新生成", "PublicPlan")
    endpoint("/plans/{plan_id}/confirm", "post", "confirmPlan", "确认当前有效计划", "PublicPlan", "RevisionInput")
    endpoint("/jobs/{job_id}", "get", "getJob", "查询持久任务", "Job")
    endpoint("/jobs/{job_id}/retry", "post", "retryJob", "明确重试可恢复失败/未知任务", "Job", "RetryInput", 202)

    # ------------------------------------------------------------------ M2 文字面试
    M2_ERRORS = (400, 401, 404, 409, 422)
    endpoint("/plans/{plan_id}/interviews", "post", "startInterview",
             "开始文字面试；同计划重复进入打开原场", "InterviewStartReceipt",
             "StartInterviewInput", 201, extra_success=[200], errors=M2_ERRORS)
    endpoint("/interviews", "get", "listInterviews", "本人面试记录，按开场时间倒序", "InterviewHistory",
             query=[
                 {"name": "limit", "in": "query", "required": False,
                  "schema": {"type": "integer", "minimum": 1, "maximum": 50}},
                 {"name": "cursor", "in": "query", "required": False,
                  "schema": {"type": "string"}},
                 {"name": "plan_id", "in": "query", "required": False,
                  "schema": {"type": "string", "format": "uuid"}},
             ], errors=M2_ERRORS)
    endpoint("/interviews/{session_id}", "get", "getInterview",
             "面试间／恢复／问答查看", "InterviewDetail", errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/answers", "post", "submitInterviewAnswer",
             "提交主回答或追问回答", "InterviewReceipt", "InterviewAnswerInput", 202,
             extra_success=[200], errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/skip", "post", "skipInterviewTurn",
             "跳过当前主问题或追问", "InterviewReceipt", "InterviewSkipInput",
             errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/pause", "post", "pauseInterview",
             "暂停面试", "InterviewReceipt", "RevisionInput", errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/resume", "post", "resumeInterview",
             "继续面试", "InterviewReceipt", "RevisionInput", errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/continue-without-follow-up", "post",
             "continueWithoutFollowUp", "追问判定失败后保留主回答继续",
             "InterviewReceipt", "InterviewContinueInput", errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/finish", "post", "finishInterview",
             "提前结束并锁定问答", "InterviewReceipt", "RevisionInput", errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/report", "get", "getInterviewReport",
             "读取已发布报告；未结束/生成中/失败返回409", "InterviewReport",
             errors=M2_ERRORS)
    endpoint("/interviews/{session_id}/report/basic", "post", "createBasicInterviewReport",
             "报告任务失败后发布基础报告", "InterviewReceipt",
             "InterviewBasicReportInput", errors=M2_ERRORS)

    # ------------------------------------------------------------------ M3 语音面试
    M3_ERRORS = (400, 401, 404, 409, 413, 415, 422, 429, 503)
    endpoint("/voice/config", "get", "getVoiceConfig",
             "语音能力、时长与固定PCM格式；不返回Key", "VoiceConfig", errors=M3_ERRORS)
    endpoint("/interviews/{session_id}/turns/{turn_id}/voice-draft", "get", "getVoiceDraft",
             "读取当前回合语音草稿与任务摘要", "VoiceDraft", errors=M3_ERRORS)
    endpoint("/interviews/{session_id}/turns/{turn_id}/audio", "post", "uploadVoiceAudio",
             "上传固定16k单声道PCM，CAS激活take；不提交答案", "VoiceUploadReceipt",
             "VoiceUploadInput", 201, multipart=True, extra_success=[200], errors=M3_ERRORS)
    endpoint("/audio-assets/{audio_asset_id}/transcriptions", "post", "createAudioTranscription",
             "为当前take创建唯一ASR任务", "Job", "TranscribeInput", 202, errors=M3_ERRORS)
    endpoint("/audio-assets/{audio_asset_id}/transcript", "get", "getAudioTranscript",
             "读取原始转写；202处理中/200终态", "VoiceTranscript", extra_success=[202],
             errors=M3_ERRORS)
    endpoint("/interviews/{session_id}/speech", "post", "requestInterviewSpeech",
             "按服务端允许文本生成或读取语音缓存", "SpeechAsset",
             "SpeechRequestInput", 202, extra_success=[200], errors=M3_ERRORS)
    endpoint("/speech-assets/{speech_asset_id}", "get", "getSpeechAsset",
             "读取语音缓存状态", "SpeechAsset", errors=M3_ERRORS)
    endpoint("/speech-assets/{speech_asset_id}/content", "get", "getSpeechAssetContent",
             "鉴权读取完整WAV；无公共路径", status=200,
             response_content_type="audio/wav", errors=M3_ERRORS)
    api["paths"]["/speech-assets/{speech_asset_id}/content"]["get"]["responses"]["200"]["content"] = {
        "audio/wav": {"schema": {"type": "string", "format": "binary"}}
    }
    return api


def evidence(quote, origin="extracted"):
    return {"quote": quote, "origin": origin}


def snapshot(number, kind, title, text, profile, template_id=None):
    return {"id": uid(number), "material_id": uid(number + 100), "source_revision": 1,
            "kind": kind, "title": title, "text": text, "profile": profile,
            "template_id": template_id, "template_version": 1 if template_id else None,
            "confirmed_at": STAMP}


def build_materials():
    edu = "本科在读，计算机科学与技术专业。"
    proj = "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。"
    skill = "学习过 Java 集合基础和 JUnit 单元测试。"
    resume = {"kind": "resume", "education": [{"item_id": "edu-1", "degree": "本科在读",
        "major": "计算机科学与技术", "evidence": evidence(edu)}], "work_experience": [],
        "projects": [{"item_id": "project-1", "name": "校园图书借阅系统", "description": proj,
                      "skill_codes": ["java.core", "test.junit"], "evidence": evidence(proj)}],
        "skills": [{"item_id": "skill-1", "name": "Java 集合基础和 JUnit", "code": "java.core",
                    "evidence": evidence(skill)}]}
    no_project = copy.deepcopy(resume)
    no_project["projects"] = []
    jd_parts = ["内置练习岗位：Java 后端开发实习生。", "职责：在指导下完成 Java 功能开发并说明实现思路。",
                "要求：理解 Java 集合的使用及基本边界。", "要求：能够为简单功能设计边界测试。"]
    jd = {"kind": "jd", "role_id": "java-backend", "level": "intern", "title": "Java 后端开发实习生",
          "responsibilities": [{"item_id": "responsibility-1", "text": jd_parts[1], "evidence": evidence(jd_parts[1])}],
          "requirements": [{"item_id": "req-1", "text": "理解 Java 集合的使用及基本边界",
                            "skill_codes": ["java.core"], "importance": "must", "evidence": evidence(jd_parts[2])},
                           {"item_id": "req-2", "text": "能够为简单功能设计边界测试",
                            "skill_codes": ["test.junit"], "importance": "must", "evidence": evidence(jd_parts[3])}]}
    own_text = "自备练习JD：Java 后端实习。\n职责：在指导下编写 Java 基础功能。\n要求：了解线程安全，能区分共享读写和单线程使用。"
    own_jd = {"kind": "jd", "role_id": "java-backend", "level": "intern", "title": "Java 后端实习",
        "responsibilities": [{"item_id": "responsibility-1", "text": "在指导下编写 Java 基础功能",
                              "evidence": evidence("职责：在指导下编写 Java 基础功能。") }],
        "requirements": [{"item_id": "req-1", "text": "了解线程安全，能区分共享读写和单线程使用",
                          "skill_codes": ["java.concurrency"], "importance": "must",
                          "evidence": evidence("要求：了解线程安全，能区分共享读写和单线程使用。")}]}
    return {"test_only": True, "snapshots": [
        snapshot(1, "resume", "虚构学生简历（无工作经历）", "\n".join([edu, proj, skill, "没有工作经历。"]), resume),
        snapshot(2, "jd", "内置JD开发样例", "\n".join(jd_parts), jd, "java-backend-intern-v1"),
        snapshot(3, "resume", "虚构学生简历（无项目无工作）", "\n".join([edu, skill]), no_project),
        snapshot(4, "jd", "自备JD开发样例", own_text, own_jd)]}


def build_content(materials):
    rubric = {"id": "m1-explanation", "version": 1, "dimensions": [
        {"id": "accuracy", "name": "准确性", "weight": 0.4,
         "low_anchor": "关键机制或材料事实错误", "high_anchor": "事实与前提准确，允许合理替代方案"},
        {"id": "reasoning", "name": "推理与边界", "weight": 0.35,
         "low_anchor": "只有结论且不能说明适用条件", "high_anchor": "说明原因、前提与边界"},
        {"id": "verification", "name": "验证思路", "weight": 0.25,
         "low_anchor": "没有可检验思路", "high_anchor": "提出可执行的小样例或验证步骤"}]}
    hash_url = "https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/util/HashMap.html"
    list_url = "https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/util/ArrayList.html"
    facts = {"test_only": True, "cards": [
        {"id": "FC-HASHMAP", "version": 1, "status": "draft", "reviewer_ids": [], "url": hash_url,
         "license_status": "pending_owner_review",
         "summary": "HashMap不承诺遍历顺序。get/put的常数时间说明有散列分布前提；实现未同步。此处为待人工核验摘要。"},
        {"id": "FC-ARRAYLIST", "version": 1, "status": "draft", "reviewer_ids": [], "url": list_url,
         "license_status": "pending_owner_review",
         "summary": "ArrayList为可变数组列表，get为常数时间，添加为摊还常数时间，实现未同步；增长倍率不是接口承诺。此处为待人工核验摘要。"}]}
    specs = [
        ("HashMap遍历顺序", "用 HashMap 保存图书后，展示顺序能否依赖某次遍历结果？如果页面必须保持录入顺序，你会如何调整并验证？",
         "集合顺序契约", "java.core", "FC-HASHMAP", "不能依赖HashMap未承诺的遍历顺序；先明确显示需求，再选择有明确顺序语义的结构或显式排序，并用不同插入序列验证。",
         ["区分偶然结果与接口承诺", "根据显示需求设计并验证"], hash_url),
        ("ArrayList访问与添加", "假设图书列表主要按下标读取、偶尔追加，使用 ArrayList 有什么依据？请区分单次追加与摊还成本，并说明如何验证选型。",
         "集合访问成本", "java.core", "FC-ARRAYLIST", "按下标访问与追加的成本性质应结合官方契约解释；摊还成本不等于每次操作都一样快；用符合访问模式的小样例验证。",
         ["说明下标访问与追加特点", "区分单次和摊还并说明验证"], list_url),
        ("共享修改的线程边界", "两个线程同时修改同一个图书 ArrayList 时，可以直接沿用单线程代码吗？请说明需要保护的操作边界，并给出一种验证思路。",
         "共享集合修改边界", "java.core", "FC-ARRAYLIST", "ArrayList本身未同步。先识别共享状态和复合操作，再选择线程隔离、外部同步或合适替代方案；不能将测试偶然无错视为安全证明。",
         ["识别共享修改需要保护", "给出边界与验证限制"], list_url),
        ("HashMap性能前提", "如果使用 HashMap 查询图书，请解释通常所说的查询性能结论依赖什么前提。遇到分布不均的键，你会先核查哪些信息？",
         "散列性能前提", "java.core", "FC-HASHMAP", "常数时间说明依赖合理散列分布，不能无条件保证所有输入；核查键类型、散列与访问模式并在受控样例比较。",
         ["指出散列分布前提", "用受控样例验证而非承诺固定耗时"], hash_url),
        ("空值与未命中", "图书查询接口使用 HashMap 时，如果返回 null，怎样区分不存在的键与键对应空值？请先说明接口约定，再设计边界验证。",
         "空值与未命中契约", "java.core", "FC-HASHMAP", "HashMap允许空值，因此仅凭get返回null不足以区分情况；可明确禁止空值或结合键存在性检查，验证空集合、缺失键及存在空值等场景。",
         ["识别空值与未命中的歧义", "给出约定和边界样例"], hash_url),
        ("列表容量与逻辑大小", "为图书 ArrayList 提前设置容量后，能否马上按这些下标读取图书？请区分容量和已有元素数量，并说明验证步骤。",
         "容量与大小", "java.core", "FC-ARRAYLIST", "容量用于底层空间安排，size表示实际元素数，预留容量不等于已加入元素；用空列表、追加前后size和合法下标说明。",
         ["区分capacity与size", "通过追加前后样例验证"], list_url),
    ]
    candidates = []
    for i, (title, stem, focus, skill, fact, approach, points, url) in enumerate(specs, 1):
        evaluation = {"goal": focus, "reference_approach": approach,
                      "key_points": [{"point": p, "weight": 0.5} for p in points],
                      "acceptable_alternatives": ["前提明确且符合相同契约的等价方案"],
                      "common_mistakes": ["把一次运行结果当成全部输入的保证"],
                      "rubric": rubric, "basis": [{"kind": "fact_card", "ref_id": fact,
                                                     "note": "官方文档候选摘要，需真实人工审核"}],
                      "answer_status": "model_generated", "trust": "provisional", "uncertainties": []}
        candidates.append({"question_id": f"M1-JAVA-{i:04d}", "version": 1, "canonical_id": f"m1-java-{i}",
                           "status": "draft", "answer_status": "model_generated", "title": title,
                           "role_ids": ["java-backend"], "levels": ["intern", "junior"],
                           "stem": stem, "focus": focus, "topic_key": f"java.sample.{i}",
                           "skill_codes": [skill], "evaluation": evaluation,
                           "provenance": "authored", "source_urls": [url],
                           "license_status": "pending_owner_review"})
    for question in candidates:
        question["content_sha256"] = content_digest(question)
    for card in facts["cards"]:
        card["content_sha256"] = content_digest(card)
    template_snapshot = materials["snapshots"][1]
    catalog = {"sample_only": True,
               "roles": [{"id": "java-backend", "name": "Java 后端（开发样板）",
                          "levels": ["intern", "junior"], "enabled": False, "sample_only": True}],
               "skill_codes": ["java.core", "java.concurrency", "test.junit"],
               "skill_code_labels": {
                   "java.core": "Java 基础",
                   "java.concurrency": "Java 并发",
                   "test.junit": "JUnit 测试",
               },
               "jd_templates": [{"id": "java-backend-intern-v1", "version": 1, "role_id": "java-backend",
                                 "level": "intern", "title": "Java 后端实习开发样板", "label": "内置练习岗位",
                                 "text": template_snapshot["text"], "profile": template_snapshot["profile"]}],
               "rubrics": [rubric]}
    reviews = {"test_only": False, "decisions": [],
               "required_fields": ["resource_type", "resource_id", "version", "content_sha256",
                                   "reviewer_id", "decision", "reviewed_at", "note"],
               "note": "空ledger，不表示任何真实审核；bank_question和fact_card均需两位真实审核者。"}
    return catalog, {"test_only": False, "questions": candidates}, facts, reviews


def build_plan(materials, bank, facts):
    resume, jd = materials["snapshots"][:2]
    common = {"id": uid(10), "preparation_id": uid(20), "input_revision": 1, "revision": 1,
              "state": "ready", "role_id": "java-backend", "level": "intern", "main_question_count": 6,
              "source_counts": {"bank": 3, "generated": 3}, "resume_snapshot_id": resume["id"],
              "jd_snapshot_id": jd["id"], "focus_areas": ["Java集合", "课程项目", "边界验证"],
              "limitations": ["仅合成结构样例；非真实AI或人工审核结果", "生成参考仍需谨慎解释"],
              "created_at": STAMP, "confirmed_at": None}
    questions = []
    bank_sources = copy.deepcopy(bank["questions"][:3])
    fact_sources = copy.deepcopy(facts["cards"])
    reviews = []
    for resource_type, resources, id_field in (("bank_question", bank_sources, "question_id"),
                                                ("fact_card", fact_sources, "id")):
        for resource in resources:
            resource["status"] = "approved"
            if resource_type == "bank_question":
                resource["answer_status"] = "verified"
                resource["license_status"] = "clear"
                resource["evaluation"].update(answer_status="verified", trust="reviewed")
            else:
                resource["reviewer_ids"] = ["TEST-ONLY-A", "TEST-ONLY-B"]
                resource["license_status"] = "clear"
            resource["content_sha256"] = content_digest(resource)
            for reviewer in ("TEST-ONLY-A", "TEST-ONLY-B"):
                reviews.append({"resource_type": resource_type, "resource_id": resource[id_field],
                                "version": resource["version"], "content_sha256": resource["content_sha256"],
                                "reviewer_id": reviewer, "decision": "approve", "reviewed_at": STAMP,
                                "note": "TEST ONLY：合成审核字段，不是真实人工批准"})
    for i, candidate in enumerate(bank_sources, 1):
        ev = copy.deepcopy(candidate["evaluation"])
        ev.update(answer_status="verified", trust="reviewed")
        questions.append({"id": uid(30 + i), "position": i, "source": "bank",
            "bank_ref": {"question_id": candidate["question_id"], "version": 1,
                         "canonical_id": candidate["canonical_id"], "review_status": "approved",
                         "answer_status": "verified", "reviewer_ids": ["TEST-ONLY-A", "TEST-ONLY-B"],
                         "content_sha256": candidate["content_sha256"], "license_status": "clear"},
            "question_type": "knowledge",
            "canonical_id": candidate["canonical_id"], "stem": candidate["stem"],
            "focus": candidate["focus"], "topic_key": candidate["topic_key"],
            "skill_codes": candidate["skill_codes"],
            "anchors": [{"snapshot_id": jd["id"], "item_id": "req-1",
                         "quote": jd["profile"]["requirements"][0]["evidence"]["quote"]}], "evaluation": ev})
    specs = [
        ("结合你参与的校园图书借阅课程项目，如果再次实现借阅模块，你会先明确哪些输入与状态约束？请描述一次合法借阅的检查过程。",
         "借阅流程约束", "project.borrow.constraints", ["先澄清借阅前置条件", "逐步检查并说明状态变化"]),
        ("你提到参与了借阅模块的边界测试，请选择一条实际测试说明输入与预期结果；如果当时没有这条记录，也可以明确给出现在会补充的假设用例。",
         "测试证据与预期", "project.borrow.tests", ["区分实际做过与现在假设", "给出输入和可检验预期"]),
        ("假设图书借阅页面提示操作失败，但再次查询发现图书已借出，你会怎样核实真实状态，并避免未经确认地再次提交？请说明排查顺序。",
         "未知结果的核实", "project.borrow.uncertain", ["先核实状态而非盲目重试", "说明用户提示和复现方法"]),
    ]
    for offset, (stem, goal, topic, points) in enumerate(specs, 4):
        anchor = {"snapshot_id": resume["id"], "item_id": "project-1",
                  "quote": resume["profile"]["projects"][0]["evidence"]["quote"]}
        ev = {"goal": goal, "reference_approach": "围绕已确认课程项目说明前提、分析步骤与验证；假设方案明确标为假设，不强求唯一实现。",
              "key_points": [{"point": p, "weight": 0.5} for p in points],
              "acceptable_alternatives": ["满足相同前提和验证目标的等价方案"],
              "common_mistakes": ["把未实际发生的经历说成材料已确认事实"],
              "rubric": copy.deepcopy(bank["questions"][0]["evaluation"]["rubric"]),
              "basis": [{"kind": "material", "ref_id": f"{resume['id']}/project-1", "note": "以课程项目原文为事实边界"},
                        {"kind": "reasoning", "ref_id": "hypothetical-analysis", "note": "仅评价思路与验证，不断言唯一技术实现"}],
              "answer_status": "model_generated", "trust": "provisional", "uncertainties": []}
        questions.append({"id": uid(30 + offset), "position": offset, "source": "generated", "bank_ref": None,
                          "question_type": "experience",
                          "canonical_id": f"fixture-generated-{offset}", "stem": stem, "focus": goal,
                          "topic_key": topic, "skill_codes": ["test.junit"], "anchors": [anchor], "evaluation": ev})
    internal = {**common, "test_only": True, "material_snapshots": [resume, jd], "questions": questions,
                "bank_source_snapshots": bank_sources, "fact_cards": fact_sources, "content_reviews": reviews,
                "prompt_version": "m1-v1-test-only", "model_id": "SYNTHETIC-NO-AI-CALL",
                "semantic_review": {"duplicate_pairs": [], "grounding_issues": [], "uncertain": False, "reviewed_at": STAMP}}
    return internal, common


def build_cases(plan):
    cases = []

    def add(case_id, value, expected):
        cases.append({"id": case_id, "expected_error": expected, "plan": value})

    add("valid-six-test-only", copy.deepcopy(plan), None)
    for count in (2, 4):
        item = copy.deepcopy(plan)
        half = count // 2
        item["questions"] = item["questions"][:half] + item["questions"][3:3 + half]
        for position, question in enumerate(item["questions"], 1):
            question["position"] = position
        item["main_question_count"] = count
        item["source_counts"] = {"bank": half, "generated": half}
        item["bank_source_snapshots"] = item["bank_source_snapshots"][:half]
        add(f"valid-{count}-test-only", item, None)
    modifications = [
        ("wrong-quota", "SOURCE_QUOTA", lambda p: p["questions"][3].update(source="bank", bank_ref=copy.deepcopy(p["questions"][0]["bank_ref"]))),
        ("duplicate-canonical", "DUPLICATE_QUESTION", lambda p: p["questions"][3].update(canonical_id=p["questions"][0]["canonical_id"])),
        ("duplicate-normalized-stem", "DUPLICATE_QUESTION", lambda p: p["questions"][3].update(stem=p["questions"][0]["stem"] + "！")),
        ("missing-evaluation", "SCHEMA_INVALID", lambda p: p["questions"][3].pop("evaluation")),
        ("weight-sum", "EVALUATION_INVALID", lambda p: p["questions"][3]["evaluation"]["key_points"][0].update(weight=0.8)),
        ("fake-quote", "EVIDENCE_INVALID", lambda p: p["questions"][3]["anchors"][0].update(quote="这是一段不存在的企业工作经历")),
        ("semantic-duplicate", "DUPLICATE_QUESTION", lambda p: p["semantic_review"].update(duplicate_pairs=[{"left_id": p["questions"][0]["id"], "right_id": p["questions"][3]["id"], "reason": "测试用同义改写"}])),
        ("semantic-uncertain", "DEDUP_UNCERTAIN", lambda p: p["semantic_review"].update(uncertain=True)),
        ("unreviewed-bank", "SCHEMA_INVALID", lambda p: p["questions"][0]["bank_ref"].update(review_status="draft")),
        ("generated-reuse", "GENERATED_REUSE", lambda p: p["questions"][3].update(bank_ref=copy.deepcopy(p["questions"][0]["bank_ref"]))),
        ("odd-count", "SCHEMA_INVALID", lambda p: p.update(main_question_count=3)),
        ("duplicate-position", "POSITION_INVALID", lambda p: p["questions"][1].update(position=1)),
        ("stale-source-counts", "SOURCE_QUOTA", lambda p: p["source_counts"].update(bank=1)),
        ("missing-basis", "SCHEMA_INVALID", lambda p: p["questions"][3]["evaluation"].update(basis=[])),
        ("no-materials", "SCHEMA_INVALID", lambda p: p.update(material_snapshots=[])),
        ("generated-false-verification", "EVALUATION_INVALID", lambda p: p["questions"][3]["evaluation"].update(answer_status="verified", trust="reviewed")),
        ("unknown-fact-card", "EVALUATION_INVALID", lambda p: p["questions"][3]["evaluation"]["basis"].append({"kind": "fact_card", "ref_id": "NOT-EXISTS", "note": "测试未知依据"})),
        ("unreviewed-fact-card", "EVALUATION_INVALID", lambda p: p["fact_cards"][0].update(status="draft")),
        ("missing-second-reviewer", "BANK_INELIGIBLE", lambda p: p.update(content_reviews=[r for r in p["content_reviews"] if not (r["resource_type"] == "bank_question" and r["resource_id"] == p["questions"][0]["bank_ref"]["question_id"] and r["reviewer_id"] == "TEST-ONLY-B")])),
        ("changed-approved-content", "BANK_INELIGIBLE", lambda p: p["bank_source_snapshots"][0].update(stem="审核后被修改的题目，不再对应原先的内容摘要")),
        ("fabricated-experience", "GROUNDING_INVALID", lambda p: p["semantic_review"].update(grounding_issues=[{"question_id": p["questions"][3]["id"], "claim": "候选人有三年企业经验", "reason": "简历仅有课程项目"}])),
        ("ready-with-confirmation-time", "PLAN_STATE_INVALID", lambda p: p.update(confirmed_at=STAMP)),
    ]
    for name, expected, mutate in modifications:
        item = copy.deepcopy(plan)
        mutate(item)
        add(name, item, expected)
    return {"test_only": True, "cases": cases}


def build_m2_examples():
    """M2 请求／回包合成样例；用于两端编解码与契约校验，不含真实用户数据。"""
    sid = uid(900)
    pid = uid(901)
    prep = uid(902)
    session_turn_main = uid(910)
    session_turn_follow = uid(911)
    question_main = uid(912)
    question_follow = uid(913)
    answer_main = uid(920)
    answer_follow = uid(921)
    follow_job = uid(930)
    report_job = uid(931)
    report_id = uid(940)
    materials = [
        {"kind": "resume", "snapshot_id": uid(1), "material_id": uid(101),
         "title": "虚构学生简历（无工作经历）", "source_revision": 1},
        {"kind": "jd", "snapshot_id": uid(2), "material_id": uid(102),
         "title": "内置JD开发样例", "source_revision": 1},
    ]
    plan_summary = {
        "plan_id": pid, "main_question_count": 6,
        "source_counts": {"bank": 3, "generated": 3},
        "material_versions": materials, "sample_only": True,
    }
    main_answer = {"answer_id": answer_main, "text": "我先检查相同键是否存在，再决定更新还是插入。",
                   "submitted_at": STAMP, "input_source": "text",
                   "audio_asset_id": None, "transcript_revision": None}
    follow_answer = {"answer_id": answer_follow, "text": "这个判断依赖单线程访问；共享读写时需要外部同步。",
                     "submitted_at": STAMP, "input_source": "text",
                     "audio_asset_id": None, "transcript_revision": None}
    turn_main = {"turn_id": session_turn_main, "question_id": question_main, "position": 1,
                 "kind": "main", "prompt": "图书查询接口使用 HashMap 时，怎样区分不存在的键与键对应空值？",
                 "status": "answered", "answer": main_answer}
    turn_follow_pending = {"turn_id": session_turn_follow, "question_id": question_main,
                           "position": 1, "kind": "follow_up",
                           "prompt": "你提到先检查相同键是否存在，能进一步说明判断依据和适用边界吗？",
                           "status": "pending", "answer": None}
    turn_follow_answered = {**turn_follow_pending, "status": "answered", "answer": follow_answer}
    job_queued = {"id": follow_job, "state": "queued", "attempt": 1, "retryable": False,
                  "error_code": None}
    job_failed = {"id": report_job, "state": "failed", "attempt": 2, "retryable": False,
                  "error_code": "AI_INVALID_OUTPUT"}
    detail_base = {
        "session_id": sid, "plan_id": pid, "preparation_id": prep,
        "role_id": "java-backend", "level": "intern", "mode": "text",
        "started_at": STAMP, "plan_summary": plan_summary,
        "opening_text": "本次文字模拟面试共 6 道主问题，题库与现场生成各 3 道。",
    }
    detail_active = {
        **detail_base, "status": "active", "phase": "awaiting_answer", "revision": 4,
        "ended_at": None, "end_reason": None, "closing_text": None,
        "current_main_position": 1, "answered_main_count": 1, "skipped_main_count": 0,
        "current_turn": turn_follow_pending,
        "allowed_actions": ["pause", "finish", "answer", "skip"],
        "transcript": [turn_main, turn_follow_pending],
        "follow_up_job": {**job_queued, "state": "succeeded", "retryable": False},
        "report_job": None, "report_state": "not_requested",
    }
    detail_deciding = {
        **detail_base, "status": "active", "phase": "deciding_follow_up", "revision": 3,
        "ended_at": None, "end_reason": None, "closing_text": None,
        "current_main_position": 1, "answered_main_count": 0, "skipped_main_count": 0,
        "current_turn": None,
        "allowed_actions": ["pause", "finish"],
        "transcript": [turn_main], "follow_up_job": job_queued,
        "report_job": None, "report_state": "not_requested",
    }
    detail_ended = {
        **detail_base, "status": "ended", "phase": "closed", "revision": 12,
        "ended_at": STAMP, "end_reason": "completed",
        "closing_text": "本次文字面试已结束，问答已锁定。",
        "current_main_position": None, "answered_main_count": 6, "skipped_main_count": 0,
        "current_turn": None,
        "allowed_actions": ["create_basic_report"],
        "transcript": [turn_main, turn_follow_answered],
        "follow_up_job": {**job_queued, "state": "succeeded", "retryable": False},
        "report_job": job_failed, "report_state": "failed",
    }
    coverage_full = {
        "planned": 2, "presented": 2, "answered": 2, "skipped": 0,
        "unanswered": 0, "not_reached": 0, "scored": 2,
        "bank_planned": 1, "bank_answered": 1,
        "generated_planned": 1, "generated_answered": 1,
        "follow_up_presented": 1, "follow_up_answered": 1,
    }
    dimensions = [
        {"dimension_id": "accuracy", "name": "准确性", "weight": 0.4,
         "verdict": "scored", "score": 3,
         "evidence": [{"answer_id": answer_main, "quote": "我先检查相同键是否存在"}],
         "explanation": "原话说明了先查后写的顺序。", "improvement": "补充空值契约的具体例子。"},
        {"dimension_id": "reasoning", "name": "推理与边界", "weight": 0.35,
         "verdict": "scored", "score": 3,
         "evidence": [{"answer_id": answer_follow, "quote": "共享读写时需要外部同步"}],
         "explanation": "追问回答补充了共享访问边界。", "improvement": "说明同步方案的选择依据。"},
        {"dimension_id": "verification", "name": "验证思路", "weight": 0.25,
         "verdict": "scored", "score": 2,
         "evidence": [{"answer_id": answer_main, "quote": "再决定更新还是插入"}],
         "explanation": "提到检查但缺少可执行样例。", "improvement": "给出缺失键与空值两组输入。"},
        {"dimension_id": "expression_structure", "name": "表达结构", "weight": 0.0001,
         "verdict": "scored", "score": 3,
         "evidence": [{"answer_id": answer_main, "quote": "我先检查相同键是否存在"}],
         "explanation": "按步骤组织。", "improvement": "每个结论后补一句边界。"},
    ]
    uncertain_dimensions = copy.deepcopy(dimensions)
    uncertain_dimensions[0] = {
        "dimension_id": "accuracy", "name": "准确性", "weight": 0.4,
        "verdict": "uncertain", "score": None, "evidence": [],
        "explanation": "评分引文无法在本场本题原回答中逐字定位，已降为受限结果。",
        "improvement": "用原话中的具体事实重新说明。",
    }
    report_question_full = {
        "question_id": question_main, "position": 1, "source": "bank",
        "outcome": "answered", "prompt": turn_main["prompt"], "bank_version": 1,
        "turns": [turn_main, turn_follow_answered],
        "assessment_trust": "reviewed", "rubric_version": "m1-explanation@1",
        "question_score": 70, "dimensions": dimensions,
        "follow_up_outcome": "asked",
    }
    report_question_uncertain = {
        **report_question_full, "question_score": None, "dimensions": uncertain_dimensions,
    }
    report_base = {
        "report_id": report_id, "session_id": sid, "role_id": "java-backend",
        "level": "intern", "material_versions": materials,
        "rubric_policy_version": "m2-rubric-policy-v1", "created_at": STAMP,
        "end_reason": "completed", "coverage": coverage_full,
    }
    finding = {
        "question_id": question_main, "position": 1, "dimension_id": "accuracy",
        "text": "1. 准确性：原话说明了先查后写的顺序。",
        "evidence": [{"answer_id": answer_main, "quote": "我先检查相同键是否存在"}],
    }
    report_full = {
        **report_base, "quality": "full",
        "strengths": [finding], "improvements": [finding],
        "questions": [report_question_full],
    }
    report_partial = {
        **report_base, "quality": "partial",
        "strengths": [], "improvements": [finding],
        "questions": [report_question_uncertain],
    }
    report_basic = {
        **report_base, "quality": "unscored", "strengths": [], "improvements": [],
        "questions": [{**report_question_full, "question_score": None,
                       "dimensions": []}],
    }
    report_no_answers = {
        **report_base, "quality": "no_answers",
        "coverage": {**coverage_full, "answered": 0, "scored": 0,
                     "skipped": 1, "not_reached": 1, "presented": 1,
                     "bank_answered": 0, "generated_answered": 0,
                     "follow_up_presented": 0, "follow_up_answered": 0},
        "strengths": [], "improvements": [],
        "questions": [
            {**report_question_full, "outcome": "skipped", "turns": [],
             "question_score": None, "dimensions": [], "follow_up_outcome": None},
            {**report_question_full, "position": 2, "outcome": "not_reached",
             "prompt": None, "turns": [], "question_score": None,
             "dimensions": [], "follow_up_outcome": None},
        ],
    }
    error = {"error": {"code": "REPORT_PENDING", "message": "报告仍在生成中，请稍后再试",
                       "retryable": False, "details": [], "request_id": uid(999)}}
    job_follow_failed = {
        "id": follow_job, "kind": "follow_up", "target_id": sid, "input_revision": 3,
        "state": "failed", "attempt": 1, "retryable": True, "result_ref": None,
        "error": {"code": "AI_TIMEOUT", "message": "AI 服务超时，结果未知",
                  "retryable": True, "details": [], "request_id": uid(998)},
        "created_at": STAMP, "updated_at": STAMP,
    }
    job_report_failed = {
        "id": report_job, "kind": "interview_report", "target_id": sid, "input_revision": 12,
        "state": "failed", "attempt": 2, "retryable": False, "result_ref": None,
        "error": {"code": "AI_INVALID_OUTPUT", "message": "AI 输出不符合契约",
                  "retryable": False, "details": [], "request_id": uid(997)},
        "created_at": STAMP, "updated_at": STAMP,
    }
    return {
        "test_only": True,
        "start_request": {"plan_revision": 2, "external_processing_consent": True},
        "start_created": {"session_id": sid, "revision": 1, "mode": "text"},
        "start_reopened": {"session_id": sid, "revision": 1, "mode": "text"},
        "answer_request": {"revision": 1, "turn_id": session_turn_main,
                           "text": "我先检查相同键是否存在，再决定更新还是插入。"},
        "answer_receipt_main": {"session_id": sid, "revision": 2,
                                "answer_id": answer_main, "job_id": follow_job},
        "answer_receipt_follow_up": {"session_id": sid, "revision": 4,
                                     "answer_id": answer_follow, "job_id": None},
        "detail_active": detail_active,
        "detail_deciding": detail_deciding,
        "detail_ended": detail_ended,
        "history": {
            "items": [{"session_id": sid, "plan_id": pid, "preparation_id": prep,
                       "role_id": "java-backend", "level": "intern",
                       "mode": "text", "status": "ended", "started_at": STAMP, "ended_at": STAMP,
                       "end_reason": "completed", "main_question_count": 6,
                       "answered_main_count": 6, "report_state": "ready"}],
            "next_cursor": "eyJzdGFydGVkX2F0IjoiMjAyNi0xMS0xNlQwMDowMDowMCswMDowMCIsImlkIjoiMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwOTAwIn0",
        },
        "report_full": report_full,
        "report_partial": report_partial,
        "report_basic": report_basic,
        "report_no_answers": report_no_answers,
        "error_report_pending": error,
        "job_follow_failed": job_follow_failed,
        "job_report_failed": job_report_failed,
    }


def build_m3_examples():
    """M3 语音请求／回包合成样例：只覆盖协议，不含真实音频或个人材料。"""
    sid = uid(950)
    pid = uid(951)
    prep = uid(952)
    question = uid(953)
    turn_main = uid(954)
    turn_follow = uid(955)
    answer_main = uid(956)
    audio = uid(960)
    speech = uid(961)
    asr_job = uid(962)
    tts_job = uid(963)
    take = uid(964)
    materials = [
        {"kind": "resume", "snapshot_id": uid(1), "material_id": uid(101),
         "title": "虚构学生简历（无工作经历）", "source_revision": 1},
        {"kind": "jd", "snapshot_id": uid(2), "material_id": uid(102),
         "title": "内置JD开发样例", "source_revision": 1},
    ]
    plan_summary = {
        "plan_id": pid, "main_question_count": 6,
        "source_counts": {"bank": 3, "generated": 3},
        "material_versions": materials, "sample_only": True,
    }
    audio_asset = {
        "id": audio, "session_id": sid, "turn_id": turn_main, "take_id": take,
        "sha256": "a" * 64, "byte_length": 64000, "duration_ms": 2000,
        "format": "pcm_s16le/16000/mono", "status": "stored",
        "expires_at": "2026-11-22T00:00:00Z", "created_at": STAMP,
    }
    transcript_ready = {
        "audio_asset_id": audio, "revision": 1, "status": "ready",
        "text": "我先检查相同键是否存在，再决定更新还是插入。",
        "provider": "deepseek", "request_id": "req_synthetic_asr",
        "error_code": None, "updated_at": STAMP,
    }
    transcript_processing = {
        "audio_asset_id": audio, "revision": 1, "status": "processing",
        "text": None, "provider": None, "request_id": None,
        "error_code": None, "updated_at": STAMP,
    }
    transcript_empty = {
        **transcript_ready, "status": "empty", "text": "",
        "request_id": "req_synthetic_empty",
    }
    transcript_failed = {
        **transcript_ready, "status": "failed", "text": None,
        "request_id": "req_synthetic_failed", "error_code": "AI_TIMEOUT",
    }
    job_queued = {"id": asr_job, "state": "queued", "attempt": 1,
                  "retryable": False, "error_code": None}
    job_failed = {"id": asr_job, "state": "failed", "attempt": 2,
                  "retryable": False, "error_code": "AI_TIMEOUT"}
    voice_answer = {
        "answer_id": answer_main,
        "text": "我先检查相同键是否存在，再决定更新还是插入。",
        "submitted_at": STAMP, "input_source": "asr_corrected",
        "audio_asset_id": audio, "transcript_revision": 1,
    }
    voice_turn_main = {
        "turn_id": turn_main, "question_id": question, "position": 1,
        "kind": "main", "prompt": "图书查询接口使用 HashMap 时，怎样区分不存在的键与键对应空值？",
        "status": "answered", "answer": voice_answer,
    }
    voice_turn_follow = {
        "turn_id": turn_follow, "question_id": question, "position": 1,
        "kind": "follow_up",
        "prompt": "你提到先检查相同键是否存在，能进一步说明判断依据和适用边界吗？",
        "status": "pending", "answer": None,
    }
    detail_voice = {
        "session_id": sid, "plan_id": pid, "preparation_id": prep,
        "role_id": "java-backend", "level": "intern", "mode": "voice",
        "status": "active", "phase": "awaiting_answer", "revision": 4,
        "started_at": STAMP, "ended_at": None, "end_reason": None,
        "plan_summary": plan_summary,
        "opening_text": "本次语音模拟面试共 6 道主问题。回答问题前会先播报题干，录音最多 120 秒。",
        "closing_text": None, "current_main_position": 1,
        "answered_main_count": 1, "skipped_main_count": 0,
        "current_turn": voice_turn_follow,
        "allowed_actions": ["pause", "finish", "answer", "skip"],
        "transcript": [voice_turn_main, voice_turn_follow],
        "follow_up_job": {"id": asr_job, "state": "succeeded", "attempt": 1,
                          "retryable": False, "error_code": None},
        "report_job": None, "report_state": "not_requested",
    }
    speech_base = {
        "id": speech, "session_id": sid, "segment": "turn", "turn_id": turn_main,
        "voice_id": "stella", "provider": "deepseek",
        "format": "pcm_s16le/24000/mono",
        "created_at": STAMP, "updated_at": STAMP, "synthetic": False,
    }
    speech_ready = {
        **speech_base, "status": "ready", "duration_ms": 3200,
        "sha256": "b" * 64, "job_id": tts_job, "error_code": None,
    }
    speech_queued = {
        **speech_base, "status": "queued", "duration_ms": None,
        "sha256": None, "job_id": tts_job, "error_code": None,
    }
    speech_failed = {
        **speech_base, "status": "failed", "duration_ms": None,
        "sha256": None, "job_id": tts_job, "error_code": "AI_TIMEOUT",
    }
    speech_synthetic = {
        **speech_ready, "provider": "test", "synthetic": True,
        "sha256": "c" * 64,
    }
    return {
        "test_only": True,
        "voice_config": {
            "available": True, "max_answer_seconds": 120,
            "format": "pcm_s16le/16000/mono",
            "tts_voice_id": "stella", "speech_provider": "deepseek",
        },
        "voice_start_request": {
            "plan_revision": 2, "external_processing_consent": True,
            "mode": "voice", "audio_processing_consent": True,
        },
        "voice_start_created": {"session_id": sid, "revision": 1, "mode": "voice"},
        "voice_start_reopened_text": {"session_id": sid, "revision": 7, "mode": "text"},
        "voice_upload_request": {
            "take_id": take, "session_revision": 3, "expected_voice_revision": 0,
            "sha256": "a" * 64, "format": "pcm_s16le/16000/mono",
        },
        "voice_upload_receipt": {
            "session_id": sid, "turn_id": turn_main, "voice_revision": 1,
            "audio": audio_asset,
        },
        "voice_draft_active": {
            "session_id": sid, "turn_id": turn_main, "voice_revision": 1,
            "active_audio": audio_asset, "transcript": transcript_ready,
            "asr_job": {**job_queued, "state": "succeeded"},
        },
        "voice_draft_processing": {
            "session_id": sid, "turn_id": turn_main, "voice_revision": 1,
            "active_audio": audio_asset, "transcript": None, "asr_job": job_queued,
        },
        "voice_draft_empty": {
            "session_id": sid, "turn_id": turn_main, "voice_revision": 2,
            "active_audio": {**audio_asset, "id": uid(965), "take_id": uid(966)},
            "transcript": transcript_empty, "asr_job": {**job_queued, "state": "succeeded"},
        },
        "voice_draft_failed": {
            "session_id": sid, "turn_id": turn_main, "voice_revision": 2,
            "active_audio": {**audio_asset, "id": uid(965), "take_id": uid(966)},
            "transcript": transcript_failed, "asr_job": job_failed,
        },
        "transcript_processing": transcript_processing,
        "transcript_ready": transcript_ready,
        "transcript_empty": transcript_empty,
        "transcript_failed": transcript_failed,
        "answer_request_asr": {
            "revision": 3, "turn_id": turn_main,
            "text": "我先检查相同键是否存在，再决定更新还是插入。",
            "input_source": "asr", "audio_asset_id": audio, "transcript_revision": 1,
        },
        "answer_request_asr_corrected": {
            "revision": 3, "turn_id": turn_main,
            "text": "我先检查相同键是否存在，再决定更新、插入还是报错。",
            "input_source": "asr_corrected", "audio_asset_id": audio,
            "transcript_revision": 1,
        },
        "answer_request_voice_text_fallback": {
            "revision": 3, "turn_id": turn_main,
            "text": "改用文字回答：先检查键是否存在，再决定更新还是插入。",
            "input_source": "text",
        },
        "voice_detail_active": detail_voice,
        "voice_history": {
            "items": [{"session_id": sid, "plan_id": pid, "preparation_id": prep,
                       "role_id": "java-backend", "level": "intern", "mode": "voice",
                       "status": "active", "started_at": STAMP, "ended_at": None,
                       "end_reason": None, "main_question_count": 6,
                       "answered_main_count": 1, "report_state": "not_requested"}],
            "next_cursor": None,
        },
        "speech_request_turn": {"segment": "turn", "turn_id": turn_main},
        "speech_asset_ready": speech_ready,
        "speech_asset_queued": speech_queued,
        "speech_asset_failed": speech_failed,
        "speech_asset_synthetic": speech_synthetic,
    }


def outputs():
    schema = build_schema()
    materials = build_materials()
    catalog, bank, facts, reviews = build_content(materials)
    internal, public = build_plan(materials, bank, facts)
    return {"contracts/m1/m1.schema.json": schema, "contracts/m1/openapi.json": build_openapi(schema),
            "fixtures/m1/materials.json": materials, "fixtures/m1/catalog.json": catalog,
            "fixtures/m1/bank-candidates.json": bank, "fixtures/m1/fact-cards.json": facts,
            "fixtures/m1/content-reviews.json": reviews,
            "fixtures/m1/internal-plan.example.json": internal,
            "fixtures/m1/public-plan.example.json": public, "fixtures/m1/rule-cases.json": build_cases(internal),
            "fixtures/m1/m2-examples.json": build_m2_examples(),
            "fixtures/m1/m3-examples.json": build_m3_examples()}


def main():
    parser = argparse.ArgumentParser(description="生成或检查M1/M2契约与合成样例，不启动业务或调用AI")
    parser.add_argument("--check", action="store_true", help="只比较生成物，不写文件")
    args = parser.parse_args()
    stale = []
    for relative, value in outputs().items():
        target = ROOT / relative
        expected = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if args.check:
            if not target.exists() or target.read_text(encoding="utf-8") != expected:
                stale.append(relative)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding="utf-8", newline="\n")
    if stale:
        raise SystemExit("生成物不同步，请运行 build_contracts.py：" + ", ".join(stale))
    print("M1/M2/M3 generated artifacts are current" if args.check else "M1/M2/M3 artifacts generated (12 files)")


if __name__ == "__main__":
    main()
