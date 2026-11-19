"""AI provider 协议与适配器。

- disabled：默认；任何 AI 任务在入队前明确拒绝（503 AI_NOT_CONFIGURED）。
- deepseek：真实外部模型，密钥只从服务端环境读取。
- test：仅在 ALLOW_TEST_FIXTURES=true 的非生产环境启用，输出确定且醒目标注 test_only。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from typing import Annotated, Any, Literal

from app.core.config import Settings
from app.core.diagnostics import (
    RECOVERABLE_PROVIDER_OUTPUT_REASONS,
    safe_validation_details,
    sanitize_external_request_id,
    sanitize_usage,
)
from app.core.errors import AppError
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from app.schemas import (
    DuplicateReviewOutput,
    GeneratedQuestionOutput,
    JdProfile,
    MatchRow,
    NonBlankStrictStr,
    ResumeProfile,
)
from app.services.evidence import profile_items
from app.services.text import normalized_stem

ALLOWED_FINISH_REASONS = {
    "stop",
    "length",
    "content_filter",
    "aborted",
    "insufficient_system_resource",
    "tool_calls",
    "unknown",
}


class _DuplicateJsonKeyError(ValueError):
    pass


class _InvalidJsonNumberError(ValueError):
    pass


class _InvalidJsonTextError(ValueError):
    pass


def _validate_json_text(value: Any) -> None:
    """在严格 JSON 解码之后校验所有对象键与嵌套字符串，阻断 U+0000 与孤立 surrogate。"""
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            if "\x00" in current:
                raise _InvalidJsonTextError("null character in json string")
            try:
                current.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _InvalidJsonTextError("invalid utf-8 surrogate in json string") from exc
        elif isinstance(current, dict):
            for key, item in current.items():
                if isinstance(key, str):
                    if "\x00" in key:
                        raise _InvalidJsonTextError("null character in json key")
                    try:
                        key.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        raise _InvalidJsonTextError("invalid utf-8 surrogate in json key") from exc
                stack.append(item)
        elif isinstance(current, list):
            stack.extend(current)


_THINKING_UNSUPPORTED_TEXT_PATTERNS = (
    re.compile(
        r"\b(?:unknown|unsupported|unrecognized|unexpected|disallowed|invalid)\s+"
        r"(?:parameter|param|field|argument|option)s?\s*[:=]?\s*['\"`]?thinking['\"`]?\b"
    ),
    re.compile(
        r"['\"`]?thinking['\"`]?(?:\s+(?:parameter|param|field|option))?\s+"
        r"(?:is\s+|are\s+)?(?:not\s+supported|unsupported|not\s+allowed|disallowed|"
        r"unknown|unrecognized|unexpected|invalid\s+parameter|not\s+a\s+valid\s+parameter|"
        r"cannot\s+be\s+used)"
    ),
    re.compile(
        r"(?:extra\s+(?:inputs?|fields?|parameters?)\s+(?:are\s+)?not\s+"
        r"(?:permitted|allowed)|additional\s+properties\s+are\s+not\s+allowed)"
        r"\b[^.;\n]*\bthinking\b"
    ),
    re.compile(
        r"\bthinking\b[^.;\n]*\b(?:extra\s+inputs?\s+are\s+not\s+permitted|"
        r"does\s+not\s+support|not\s+supported|unsupported)\b"
    ),
)
_THINKING_UNSUPPORTED_CODES = frozenset(
    {
        "unknown_parameter",
        "unsupported_parameter",
        "unrecognized_field",
        "extra_forbidden",
    }
)
_THINKING_VALUE_OR_CONFIG_ERROR_CODES = frozenset(
    {
        "value_error",
        "literal_error",
        "enum",
        "type_error",
        "dict_type",
        "model_type",
        "string_type",
        "bool_type",
        "int_type",
        "float_type",
        "list_type",
        "missing",
        "json_invalid",
        "invalid_value",
        "invalid_type",
    }
)
_TOP_LEVEL_PARAM_UNSUPPORTED_PHRASES = (
    "extra inputs are not permitted",
    "additional properties are not allowed",
    "unknown parameter",
    "unknown field",
    "unrecognized parameter",
    "unrecognized field",
    "unrecognized request argument",
    "unexpected keyword argument",
    "unexpected field",
    "unsupported parameter",
    "unsupported field",
    "parameter is not supported",
    "field is not supported",
    "parameter not supported",
    "field not supported",
    "extra parameter",
    "extra field",
)
_THINKING_VALUE_OR_CONFIG_RE = re.compile(
    r"(?:\bthinking\.|"
    r"\bthinking\s+(?:mode|type|budget|config|setting)s?\b|"
    r"\b(?:value|values|mode|modes|enum|expected|must\s+be|should\s+be|"
    r"incompatible|combination|combined\s+with|together\s+with|"
    r"cannot\s+be\s+used\s+with|not\s+supported\s+with|not\s+allowed\s+with|"
    r"conflict(?:s|ing)?\s+with|when\s+)\b)"
)


def _message_indicates_unsupported_thinking_param(
    msg_lower: str, *, allow_top_level_param_phrases: bool
) -> bool:
    if not msg_lower or "content_filter" in msg_lower:
        return False
    if _THINKING_VALUE_OR_CONFIG_RE.search(msg_lower):
        return False
    if any(pattern.search(msg_lower) for pattern in _THINKING_UNSUPPORTED_TEXT_PATTERNS):
        return True
    if allow_top_level_param_phrases:
        return any(phrase in msg_lower for phrase in _TOP_LEVEL_PARAM_UNSUPPORTED_PHRASES)
    return False


def _has_content_filter_signal(data: dict[str, Any]) -> bool:
    for obj in (data, data.get("error")):
        if not isinstance(obj, dict):
            continue
        for key in ("code", "type", "message", "msg"):
            val = obj.get(key)
            if isinstance(val, str) and "content_filter" in val.lower():
                return True
    detail = data.get("detail")
    if isinstance(detail, str) and "content_filter" in detail.lower():
        return True
    if isinstance(detail, list):
        for item in detail:
            if not isinstance(item, dict):
                continue
            for key in ("code", "type", "msg", "message"):
                val = item.get(key)
                if isinstance(val, str) and "content_filter" in val.lower():
                    return True
    return False


def _is_top_level_thinking_loc(raw_loc: object) -> bool:
    if not isinstance(raw_loc, (list, tuple)) or not raw_loc:
        return False
    norm_loc: list[str | int] = []
    for part in raw_loc:
        if isinstance(part, str):
            norm_loc.append(part.strip().lower())
        elif isinstance(part, int) and not isinstance(part, bool):
            norm_loc.append(part)
        else:
            return False
    return norm_loc == ["thinking"] or norm_loc == ["body", "thinking"]


def _is_thinking_unsupported_400(response_text: str) -> bool:
    raw = (response_text or "").strip()
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError, OverflowError):
        if raw.lstrip().startswith(("{", "[")):
            return False
        body_lower = raw.lower()
        return _message_indicates_unsupported_thinking_param(
            body_lower, allow_top_level_param_phrases=False
        )

    if not isinstance(data, dict):
        return False
    if _has_content_filter_signal(data):
        return False

    detail = data.get("detail")
    if isinstance(detail, list):
        for item in detail:
            if not isinstance(item, dict):
                continue
            if not _is_top_level_thinking_loc(item.get("loc")):
                continue
            raw_type = item.get("type")
            raw_msg = item.get("msg")
            err_type = raw_type.strip().lower() if isinstance(raw_type, str) else ""
            err_msg = raw_msg.strip().lower() if isinstance(raw_msg, str) else ""
            if err_type in _THINKING_UNSUPPORTED_CODES:
                return True
            if (
                err_type in _THINKING_VALUE_OR_CONFIG_ERROR_CODES
                or err_type.startswith(("value_error.", "type_error."))
            ):
                continue
            if _message_indicates_unsupported_thinking_param(
                err_msg, allow_top_level_param_phrases=True
            ):
                return True
        return False
    if isinstance(detail, str):
        return _message_indicates_unsupported_thinking_param(
            detail.lower(), allow_top_level_param_phrases=False
        )
    if "detail" in data and detail is not None:
        return False

    if "error" in data and data.get("error") is not None and not isinstance(data.get("error"), dict):
        err_str = data.get("error")
        if isinstance(err_str, str):
            return _message_indicates_unsupported_thinking_param(
                err_str.lower(), allow_top_level_param_phrases=False
            )
        return False

    err = data.get("error") if isinstance(data.get("error"), dict) else data
    raw_code = err.get("code")
    code = raw_code.strip().lower() if isinstance(raw_code, str) else ""
    param = err.get("param")
    raw_msg = err.get("message") if isinstance(err.get("message"), str) else data.get("message")
    msg = raw_msg if isinstance(raw_msg, str) else ""
    msg_lower = msg.lower()
    if param is not None:
        if not isinstance(param, str):
            return False
        param_clean = param.strip().lower()
        if param_clean:
            if param_clean != "thinking":
                return False
            if code in _THINKING_UNSUPPORTED_CODES:
                return True
            if code in _THINKING_VALUE_OR_CONFIG_ERROR_CODES:
                return False
            return _message_indicates_unsupported_thinking_param(
                msg_lower, allow_top_level_param_phrases=True
            )
    if msg_lower:
        return _message_indicates_unsupported_thinking_param(
            msg_lower, allow_top_level_param_phrases=False
        )
    return False


def _no_duplicate_keys_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateJsonKeyError(key)
        obj[key] = value
    return obj


def _reject_json_constant(token: str) -> Any:
    raise _InvalidJsonNumberError(token)


def _check_finite_float(raw: str) -> float:
    val = float(raw)
    if not math.isfinite(val):
        raise _InvalidJsonNumberError("non-finite float")
    return val


def _strict_json_loads(raw_text: str) -> Any:
    return json.loads(
        raw_text,
        object_pairs_hook=_no_duplicate_keys_hook,
        parse_constant=_reject_json_constant,
        parse_float=_check_finite_float,
    )


class AiProvider(ABC):
    name: str
    model_id: str
    prompt_version: str
    test_only: bool = False

    @abstractmethod
    def parse_material(
        self,
        *,
        text: str,
        kind: str,
        role_id: str,
        level: str,
        skill_catalog: list[dict[str, str]] | None = None,
        correction: dict | None = None,
    ) -> dict:
        ...

    @abstractmethod
    def match_requirements(self, *, resume_snapshot, jd_snapshot) -> list[dict]:
        ...

    @abstractmethod
    def generate_questions(
        self,
        *,
        count: int,
        resume_snapshot,
        jd_snapshot,
        rubric: dict,
        avoid_stems: list[str],
        context: dict | None = None,
    ) -> list[dict]:
        ...

    @abstractmethod
    def review_questions(
        self,
        *,
        questions: list[dict],
        material_texts: list[str],
        fact_cards: list[dict] | None = None,
    ) -> dict:
        ...

    @abstractmethod
    def decide_follow_up(
        self,
        *,
        question: dict,
        answer_text: str,
        context: dict | None = None,
    ) -> dict:
        ...

    @abstractmethod
    def score_interview_question(
        self,
        *,
        question: dict,
        answers: list[dict],
        dimensions: list[dict],
    ) -> dict:
        ...


def _safe_validation_details(
    exc: ValidationError,
    model: type[BaseModel] | None = None,
) -> list[str]:
    return safe_validation_details(exc, model)


def _validate(model: type[BaseModel], data: object):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise AppError(
            "AI_INVALID_OUTPUT",
            "AI 输出不符合契约结构",
            details=_safe_validation_details(exc, model),
        )


def _merge_stage_details(
    existing: list[str],
    *,
    stage: str,
    default_reason: str | None = None,
    finish_reason: str | None = None,
    slot: str | None = None,
) -> list[str]:
    merged: list[str] = [f"stage={stage}"]
    if slot and f"slot={slot}" not in existing:
        merged.append(f"slot={slot}")
    has_reason = any(isinstance(d, str) and d.startswith("reason=") for d in existing)
    if not has_reason and default_reason:
        merged.append(f"reason={default_reason}")
    if "source=provider_output" not in existing:
        merged.append("source=provider_output")
    has_finish = any(isinstance(d, str) and d.startswith("finish_reason=") for d in existing)
    if not has_finish and finish_reason:
        merged.append(f"finish_reason={finish_reason}")
    for entry in existing:
        if not isinstance(entry, str) or entry.startswith("stage="):
            continue
        if entry not in merged:
            merged.append(entry)
    return merged


class MatchSelectionRow(BaseModel):
    """DeepSeek 匹配阶段的内部输出行：模型只选择可引用候选 ID，不直接输出 quote 或 snapshot_id。"""

    model_config = ConfigDict(extra="forbid")

    requirement_id: NonBlankStrictStr
    status: Literal["supported", "to_verify", "not_evidenced"]
    resume_item_ids: list[NonBlankStrictStr]
    explanation: NonBlankStrictStr
    suggested_focus: NonBlankStrictStr


class MatchSelectionOutput(BaseModel):
    """DeepSeek 匹配阶段的内部输出契约。"""

    model_config = ConfigDict(extra="forbid")

    rows: list[MatchSelectionRow] = Field(min_length=1)


def _build_match_candidates(resume_snapshot) -> tuple[list[dict], dict[str, str]]:
    """从已确认简历快照构建单次匹配候选表及 item_id -> verbatim quote 映射。"""
    resume_text = getattr(resume_snapshot, "text", "") or ""
    items = profile_items(getattr(resume_snapshot, "profile", None))
    candidates: list[dict] = []
    quote_by_id: dict[str, str] = {}
    for item_id, item in items.items():
        evidence = item.get("evidence") or {}
        quote = evidence.get("quote") if isinstance(evidence, dict) else None
        if not isinstance(quote, str) or not quote.strip() or quote not in resume_text:
            continue
        quote_by_id[item_id] = quote
        candidate = {k: v for k, v in item.items() if k != "evidence"}
        candidate["item_id"] = item_id
        candidate["quote"] = quote
        candidates.append(candidate)
    return candidates, quote_by_id


def get_provider(settings: Settings) -> AiProvider:
    provider = settings.ai_provider
    if provider == "deepseek":
        if not (settings.ai_api_key and settings.ai_model):
            raise AppError("AI_NOT_CONFIGURED", "AI 服务未配置，无法执行模型任务")
        from app.ai.deepseek import DeepSeekProvider

        return DeepSeekProvider(settings)
    if provider == "test":
        if not (settings.allow_test_fixtures and not settings.is_production):
            raise AppError("AI_NOT_CONFIGURED", "测试 provider 只能在隔离测试环境启用")
        return TestProvider()
    raise AppError("AI_NOT_CONFIGURED", "AI 服务未配置，无法执行模型任务")


# --------------------------------------------------------------------------- 测试 provider


SKILL_KEYWORDS: list[tuple[str, str]] = [
    ("线程", "java.concurrency"),
    ("并发", "java.concurrency"),
    ("junit", "test.junit"),
    ("测试", "test.junit"),
    ("java", "java.core"),
    ("集合", "java.core"),
    ("hashmap", "java.core"),
    ("arraylist", "java.core"),
]


def _skill_codes(text: str) -> list[str]:
    lowered = text.lower()
    codes: list[str] = []
    for keyword, code in SKILL_KEYWORDS:
        if keyword in lowered and code not in codes:
            codes.append(code)
    return codes


def _split_name_and_desc(line: str) -> tuple[str, str]:
    for separator in ("：", ":"):
        if separator in line:
            head, _, tail = line.partition(separator)
            head = head.strip()
            tail = tail.strip()
            if head and tail:
                return head[:60], tail
    return line[:60], line


class TestProvider(AiProvider):
    """确定性测试 provider：输出只用于流程验证，不代表真实模型或人工审核。"""

    name = "test"
    model_id = "TEST-PROVIDER-NO-AI-CALL"
    prompt_version = "m1-test-provider-v1"
    test_only = True

    @staticmethod
    def _artificial_delay() -> None:
        """仅测试 adapter 支持的人工延迟，用于真机“生成中 force-stop”场景。

        只在隔离环境设置 M1_TEST_PROVIDER_DELAY_MS 时生效；生产 provider 不读取该变量。
        """
        raw = os.environ.get("M1_TEST_PROVIDER_DELAY_MS", "")
        if not raw:
            return
        try:
            seconds = max(0.0, float(raw) / 1000.0)
        except ValueError:
            return
        if seconds:
            time.sleep(seconds)

    def _begin_call(self) -> None:
        self.last_call = {
            "provider": self.name,
            "model": self.model_id,
            "request_id": f"test-{uuid.uuid4().hex[:16]}",
            "usage": None,
            "outcome": "success",
        }

    def parse_material(
        self,
        *,
        text: str,
        kind: str,
        role_id: str,
        level: str,
        skill_catalog: list[dict[str, str]] | None = None,
        correction: dict | None = None,
    ) -> dict:
        self._begin_call()
        allowed_codes = (
            {str(item["code"]) for item in skill_catalog if item.get("code")}
            if skill_catalog is not None
            else None
        )

        def _filtered_codes(raw_line: str) -> list[str]:
            codes = _skill_codes(raw_line)
            if allowed_codes is None:
                return codes
            return [code for code in codes if code in allowed_codes]

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if kind == "resume":
            education, projects, skills, works = [], [], [], []
            for index, line in enumerate(lines, 1):
                if any(word in line for word in ("本科", "硕士", "博士", "大学", "学院", "专业")):
                    degree = next(
                        (word for word in ("博士在读", "硕士在读", "本科在读", "博士", "硕士", "本科") if word in line),
                        "学历",
                    )
                    major = ""
                    for marker in ("专业",):
                        pos = line.find(marker)
                        if pos > 0:
                            major = line[max(0, pos - 16) : pos].split("，")[-1].split("。")[-1].strip()
                    education.append(
                        {
                            "item_id": f"edu-{len(education) + 1}",
                            "degree": degree,
                            "major": major or None,
                            "evidence": {"quote": line, "origin": "extracted"},
                        }
                    )
                elif "项目" in line:
                    name, description = _split_name_and_desc(line)
                    projects.append(
                        {
                            "item_id": f"project-{len(projects) + 1}",
                            "name": name,
                            "description": description,
                            "skill_codes": _filtered_codes(line),
                            "evidence": {"quote": line, "origin": "extracted"},
                        }
                    )
                elif "工作" in line and any(word in line for word in ("公司", "实习", "任职", "岗位", "实验室")):
                    body = line
                    for prefix in ("工作经历：", "工作经历:", "工作：", "工作:"):
                        if body.startswith(prefix):
                            body = body[len(prefix) :].strip()
                            break
                    parts = [
                        part.strip()
                        for part in body.replace("|", "｜").split("｜")
                        if part.strip()
                    ]
                    if len(parts) < 2:
                        raise AppError(
                            "AI_INVALID_OUTPUT",
                            "工作经历缺少明确的组织或职务，请核对原文",
                            details=[
                                "stage=parse",
                                "reason=invalid_work_experience",
                                "field=work_experience",
                            ],
                        )
                    org = parts[0][:60]
                    title = parts[1][:60]
                    description = "｜".join(parts[2:]) if len(parts) >= 3 else line
                    works.append(
                        {
                            "item_id": f"work-{len(works) + 1}",
                            "organization": org,
                            "title": title,
                            "description": description,
                            "evidence": {"quote": line, "origin": "extracted"},
                        }
                    )
                elif any(word in line for word in ("学习", "技能", "掌握", "熟悉", "了解")):
                    skills.append(
                        {
                            "item_id": f"skill-{len(skills) + 1}",
                            "name": line[:60],
                            "code": (_filtered_codes(line) or [None])[0],
                            "evidence": {"quote": line, "origin": "extracted"},
                        }
                    )
            profile = {
                "kind": "resume",
                "education": education,
                "work_experience": works,
                "projects": projects,
                "skills": skills,
            }
            if not any((education, works, projects, skills)):
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "测试 provider 无法从该简历文本结构化出任何条目，请手动填写或更换文本",
                )
            return ResumeProfile.model_validate(profile).model_dump(mode="json")

        # JD
        title = lines[0][:60] if lines else "未命名 JD"
        responsibilities, requirements = [], []
        for line in lines[1:]:
            if line.startswith("职责") or "职责：" in line:
                responsibilities.append(
                    {
                        "item_id": f"responsibility-{len(responsibilities) + 1}",
                        "text": line,
                        "evidence": {"quote": line, "origin": "extracted"},
                    }
                )
            elif line.startswith("要求") or "要求：" in line:
                requirements.append(
                    {
                        "item_id": f"req-{len(requirements) + 1}",
                        "text": line,
                        "skill_codes": _filtered_codes(line),
                        "importance": "must",
                        "evidence": {"quote": line, "origin": "extracted"},
                    }
                )
        if not responsibilities and not requirements:
            raise AppError(
                "AI_INVALID_OUTPUT", "测试 provider 未找到职责或要求行，请手动填写 JD 结构"
            )
        if not responsibilities:
            responsibilities.append(
                {
                    "item_id": "responsibility-1",
                    "text": lines[0],
                    "evidence": {"quote": lines[0], "origin": "extracted"},
                }
            )
        if not requirements:
            requirements.append(
                {
                    "item_id": "req-1",
                    "text": lines[0],
                    "skill_codes": _filtered_codes(text),
                    "importance": "must",
                    "evidence": {"quote": lines[0], "origin": "extracted"},
                }
            )
        profile = {
            "kind": "jd",
            "role_id": role_id,
            "level": level,
            "title": title,
            "responsibilities": responsibilities,
            "requirements": requirements,
        }
        return JdProfile.model_validate(profile).model_dump(mode="json")

    def match_requirements(self, *, resume_snapshot, jd_snapshot) -> list[dict]:
        self._begin_call()
        resume_items = []
        profile = resume_snapshot.profile
        for section in ("projects", "skills", "education", "work_experience"):
            for item in profile.get(section) or []:
                resume_items.append(item)
        rows: list[dict] = []
        for requirement in jd_snapshot.profile.get("requirements") or []:
            codes = set(requirement.get("skill_codes") or [])
            matched = [
                item
                for item in resume_items
                if codes & set(item.get("skill_codes") or []) or self._text_hits(requirement["text"], item)
            ]
            if matched:
                status = "supported"
                explanation = "简历中有可定位的原文支持该项要求；结论只基于已确认材料。"
                focus = f"围绕「{requirement['text'][:20]}」准备具体例子"
            elif resume_items:
                status = "to_verify"
                explanation = "简历体现了相关学习或项目背景，但没有直接对应该项要求的原文。"
                focus = f"补充可核对的事实来支持「{requirement['text'][:20]}」"
            else:
                status = "not_evidenced"
                explanation = "当前材料没有可定位的支持原文。"
                focus = "先补充材料或说明真实掌握范围，不编造经历"
            evidence = [
                {
                    "snapshot_id": str(resume_snapshot.id),
                    "item_id": item["item_id"],
                    "quote": item["evidence"]["quote"],
                }
                for item in matched[:2]
            ]
            rows.append(
                MatchRow(
                    requirement_id=requirement["item_id"],
                    status=status,
                    resume_evidence=evidence,
                    explanation=explanation,
                    suggested_focus=focus,
                ).model_dump(mode="json")
            )
        if not rows:
            raise AppError("AI_INVALID_OUTPUT", "JD 没有可分析的显式要求")
        return rows

    @staticmethod
    def _text_hits(requirement_text: str, item: dict) -> bool:
        haystack = " ".join(
            str(item.get(key, "")) for key in ("name", "description", "text", "major", "degree")
        ).lower()
        for token in ("java", "junit", "线程", "集合"):
            if token in requirement_text.lower() and token in haystack:
                return True
        return False

    def generate_questions(
        self,
        *,
        count: int,
        resume_snapshot,
        jd_snapshot,
        rubric: dict,
        avoid_stems: list[str],
        context: dict | None = None,
    ) -> list[dict]:
        self._begin_call()
        self._artificial_delay()
        context = context or {}
        question_type = context.get("question_type", "experience")
        anchors = list(context.get("allowed_anchors") or [])
        if not anchors:
            for section in ("work_experience", "projects", "skills", "education"):
                for item in resume_snapshot.profile.get(section) or []:
                    quote = (item.get("evidence") or {}).get("quote")
                    if quote:
                        anchors.append(
                            {
                                "snapshot_id": str(resume_snapshot.id),
                                "item_id": item["item_id"],
                                "quote": quote,
                                "source_kind": "resume",
                                "label": item.get("name") or item.get("organization") or item["item_id"],
                            }
                        )
            for item in jd_snapshot.profile.get("requirements") or []:
                quote = (item.get("evidence") or {}).get("quote")
                if quote:
                    anchors.append(
                        {
                            "snapshot_id": str(jd_snapshot.id),
                            "item_id": item["item_id"],
                            "quote": quote,
                            "source_kind": "jd",
                            "label": item.get("text", item["item_id"]),
                            "skill_codes": list(item.get("skill_codes") or []),
                        }
                    )
        if not anchors:
            raise AppError("AI_INVALID_OUTPUT", "没有可用于出题的本场材料锚点")
        resume_anchors = [a for a in anchors if a.get("source_kind") == "resume"]
        jd_anchors = [a for a in anchors if a.get("source_kind") == "jd"]
        allowed_cards = list(context.get("fact_cards") or [])
        target_codes = list(context.get("target_skill_codes") or [])
        match_focus = list(context.get("match_focus") or [])
        slot_index = int(context.get("slot_index", 1))
        results: list[dict] = []
        used_stems = {normalized_stem(stem) for stem in avoid_stems}

        for index in range(count):
            if question_type == "knowledge":
                if not allowed_cards:
                    raise AppError("AI_INVALID_OUTPUT", "knowledge 槽位没有可用依据卡")
                card = allowed_cards[(slot_index // 2 - 1) % len(allowed_cards)]
                anchor = (jd_anchors or resume_anchors)[
                    (slot_index // 2 - 1) % len(jd_anchors or resume_anchors)
                ]
                summary = card.get("summary") or card["id"]
                stem = (
                    f"围绕「{summary[:28]}」这一技术事实，结合岗位要求"
                    f"「{anchor['label'][:32]}」，你会如何验证实际行为与边界？"
                )
                basis = [
                    {
                        "kind": "material",
                        "ref_id": f"{anchor['snapshot_id']}/{anchor['item_id']}",
                        "note": "以当前岗位要求或材料为场景边界",
                    },
                    {
                        "kind": "fact_card",
                        "ref_id": card["id"],
                        "note": "技术结论以已审依据卡为准",
                    },
                    {
                        "kind": "reasoning",
                        "ref_id": "hypothetical-analysis",
                        "note": "仅评价验证思路",
                    },
                ]
                canonical = f"test-knowledge-{index + 1}-{uuid.uuid4().hex[:8]}"
            else:
                offset = max(0, (slot_index - 1) // 2)
                if context.get("prefer_jd_scenario") and jd_anchors:
                    anchor_pool = jd_anchors
                else:
                    anchor_pool = resume_anchors or jd_anchors
                anchor = anchor_pool[offset % len(anchor_pool)]
                focus = (
                    match_focus[(slot_index - 1) % len(match_focus)]
                    if match_focus
                    else "验证材料中的事实边界"
                )
                templates = (
                    "围绕「{anchor}」，针对「{focus}」，你会先确认哪些前提，"
                    "又如何用可检验的步骤验证结论？",
                    "如果要把「{anchor}」中的做法迁移到新的限制条件下，针对「{focus}」，"
                    "你会怎样设计对照与边界用例？",
                    "请复盘「{anchor}」：针对「{focus}」，哪些结论有原文依据，"
                    "哪些还需要补充证据或重新验证？",
                )
                stem = templates[offset % len(templates)].format(
                    anchor=anchor["label"][:28], focus=focus[:32]
                )
                basis = [
                    {
                        "kind": "material",
                        "ref_id": f"{anchor['snapshot_id']}/{anchor['item_id']}",
                        "note": "以当前材料或岗位要求为事实边界",
                    },
                    {
                        "kind": "reasoning",
                        "ref_id": "hypothetical-analysis",
                        "note": "仅评价思路与验证，不断言唯一技术实现",
                    },
                ]
                canonical = f"test-generated-{index + 1}-{uuid.uuid4().hex[:8]}"

            stem = stem.replace("职责：", "").replace("要求：", "")[:500]
            from app.services.planning import SEMANTIC_DUPLICATE_THRESHOLD, stem_similarity

            def _collides(candidate_stem: str) -> bool:
                if normalized_stem(candidate_stem) in used_stems:
                    return True
                return any(
                    prior and stem_similarity(candidate_stem, prior) >= SEMANTIC_DUPLICATE_THRESHOLD
                    for prior in avoid_stems
                )

            if _collides(stem):
                alt_templates = (
                    (
                        "当运行环境出现非典型分布或并发写冲突时，核心数据结构在接口契约上不保证哪些行为，如何撰写独立用例复现？",
                        "针对容量预分配、并发可见性与空值语义三类常见误用，分别该如何构造最小反例并验证机制边界？",
                        "在资源受限或高频变更的前提下，为何经验性默认假设可能失效，你会依据哪些权威约束校准实现？",
                    )
                    if question_type == "knowledge"
                    else (
                        "从异常注入与故障隔离角度出发，遇到超时、空值或状态冲突时，系统应返回什么可观测结果并如何断言回归？",
                        "假设线上配置或外部依赖在边界输入下突然降级，你会通过哪几步日志定位、回滚策略与单元测试守住底线？",
                        "如果团队需要重构该模块接口以兼容并发调用，哪些前置契约最容易被破坏，又该怎样设计对照实验？",
                        "针对输入规模扩大或并发争用引发的性能退化，你会采纳哪种基线度量与压测方法去证实改进有效？",
                    )
                )
                replaced = False
                for alt in alt_templates:
                    if not _collides(alt):
                        stem = alt
                        replaced = True
                        break
                if not replaced:
                    attempt = 0
                    while normalized_stem(stem) in used_stems and attempt < 6:
                        attempt += 1
                        stem = stem[:420] + f"请重点说明第 {attempt} 个边界。"
            used_stems.add(normalized_stem(stem))

            evaluation = {
                "goal": f"考察对「{anchor['label'][:24]}」相关问题的理解与验证思路",
                "reference_approach": (
                    "先澄清要求与材料事实的边界，再说明实现或学习过程中的具体步骤，"
                    "最后给出可执行的验证方式，并明确哪些结论仍缺少证据。"
                ),
                "key_points": [
                    {"point": "能引用已确认材料或依据卡中的真实事实", "weight": 0.6},
                    {"point": "给出可检验的验证思路或边界用例", "weight": 0.4},
                ],
                "acceptable_alternatives": ["前提明确且符合相同契约的等价做法"],
                "common_mistakes": ["把一次运行结果当作全部输入的保证", "把没做过的事说成做过"],
                "rubric": rubric,
                "basis": basis,
                "answer_status": "model_generated",
                "trust": "provisional",
                "uncertainties": [],
            }
            question = {
                "question_type": question_type,
                "canonical_id": canonical,
                "stem": stem,
                "focus": (match_focus[index % len(match_focus)] if match_focus else anchor["label"])[:40],
                "topic_key": (target_codes[index % len(target_codes)] if target_codes else "test.general"),
                "skill_codes": target_codes,
                "anchors": [
                    {
                        "snapshot_id": anchor["snapshot_id"],
                        "item_id": anchor["item_id"],
                        "quote": anchor["quote"],
                    }
                ],
                "evaluation": evaluation,
            }
            results.append(GeneratedQuestionOutput.model_validate(question).model_dump(mode="json"))
        return results

    def review_questions(
        self,
        *,
        questions: list[dict],
        material_texts: list[str],
        fact_cards: list[dict] | None = None,
    ) -> dict:
        self._begin_call()
        from app.services.planning import stem_similarity

        duplicate_pairs: list[dict] = []
        seen: dict[str, dict] = {}
        for index, question in enumerate(questions):
            key = normalized_stem(question["stem"])
            if key in seen:
                duplicate_pairs.append(
                    {
                        "left_id": seen[key]["id"],
                        "right_id": question["id"],
                        "reason": "归一化题干相同",
                    }
                )
            seen[key] = question
        for i in range(len(questions)):
            for j in range(i + 1, len(questions)):
                similarity = stem_similarity(questions[i]["stem"], questions[j]["stem"])
                if similarity >= 0.70:
                    duplicate_pairs.append(
                        {
                            "left_id": questions[i]["id"],
                            "right_id": questions[j]["id"],
                            "reason": f"近似改写（similarity={similarity:.2f}）",
                        }
                    )
        return DuplicateReviewOutput(
            duplicate_pairs=duplicate_pairs,
            grounding_issues=[],
            uncertain=False,
        ).model_dump(mode="json")

    # -- M2 追问与评分：确定性样例，仅用于流程验证

    @staticmethod
    def _short_quote(text: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:20]
        return text.strip()[:20]

    def decide_follow_up(
        self,
        *,
        question: dict,
        answer_text: str,
        context: dict | None = None,
    ) -> dict:
        self._begin_call()
        context = context or {}
        quote = self._short_quote(answer_text)
        # 每类来源的第一道主问题确定性地追问一次，其余无需追问。
        if context.get("is_first_of_source") and quote:
            focus = (question.get("focus") or "这个判断")[:24]
            prompt = (
                f"围绕「{focus}」，你在回答中提到「{quote}」。"
                "能进一步说明判断依据，以及这个做法在什么边界条件下会失效吗？"
            )
            return {
                "decision": "ask",
                "prompt": prompt[:220],
                "answer_quote": quote,
                "target": "说明实现依据与适用边界",
            }
        return {"decision": "advance", "prompt": None, "answer_quote": None, "target": None}

    def score_interview_question(
        self,
        *,
        question: dict,
        answers: list[dict],
        dimensions: list[dict],
    ) -> dict:
        self._begin_call()
        text = "\n".join(item["text"] for item in answers if item.get("text"))
        first_answer_id = answers[0]["answer_id"] if answers else None
        if not text or first_answer_id is None or len(text.strip()) < 8:
            return {
                "question_id": question["id"],
                "dimensions": [
                    {
                        "dimension_id": dim["id"],
                        "verdict": "insufficient_evidence",
                        "score": None,
                        "evidence": [],
                        "explanation": "回答过短，无法在本场原话中定位该维度的有效证据。",
                        "improvement": "用具体事实、原因和验证步骤展开回答。",
                    }
                    for dim in dimensions
                ],
            }
        quote = text.strip()[:60]
        evidence = [{"answer_id": first_answer_id, "quote": quote}]
        base = 2
        if len(text) >= 40:
            base += 1
        if any(word in text for word in ("因为", "先", "步骤", "验证", "边界", "所以")):
            base += 1
        base = max(0, min(4, base))
        results = []
        for dim in dimensions:
            if dim["id"] == "expression_structure":
                score = 2
                if "\n" in text or "因为" in text or "先" in text:
                    score += 1
                if len(text) >= 80:
                    score += 1
                score = max(0, min(4, score))
                results.append(
                    {
                        "dimension_id": dim["id"],
                        "verdict": "scored",
                        "score": score,
                        "evidence": evidence,
                        "explanation": "文字按因果或步骤组织，能跟随主要结论。",
                        "improvement": "每个结论后补一句边界或验证方式。",
                    }
                )
                continue
            results.append(
                {
                    "dimension_id": dim["id"],
                    "verdict": "scored",
                    "score": base,
                    "evidence": evidence,
                    "explanation": "原话给出了基本要点，并部分说明原因或验证思路。",
                    "improvement": "补充适用前提、反例或可执行验证步骤。",
                }
            )
        return {"question_id": question["id"], "dimensions": results}


# --------------------------------------------------------------------------- DeepSeek


class DeepSeekProvider(AiProvider):
    name = "deepseek"
    test_only = False

    def __init__(self, settings: Settings, transport=None) -> None:
        self._settings = settings
        self._transport = transport
        self.model_id = settings.ai_model
        self.prompt_version = "m1-v1"
        self._base_url = settings.ai_base_url.rstrip("/")
        self._thinking_unsupported = False

    # -- HTTP
    def _chat_json(
        self,
        system: str,
        user: str,
        *,
        request_options: dict[str, Any] | None = None,
    ) -> tuple[dict, str]:
        import httpx

        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        if request_options:
            for key, value in request_options.items():
                if value is not None:
                    payload[key] = value
        self.last_call = {
            "provider": self.name,
            "model": self.model_id,
            "request_id": "",
            "usage": None,
            "finish_reason": "unknown",
            "outcome": "unknown",
            "http_requests": 1,
            "thinking_fallback": False,
        }

        def _post(req_payload: dict[str, Any]) -> httpx.Response:
            client_kwargs = {"timeout": self._settings.ai_timeout_seconds}
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            with httpx.Client(**client_kwargs) as client:
                return client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._settings.ai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=req_payload,
                )

        try:
            response = _post(payload)
            if response.status_code == 400 and "thinking" in payload:
                if _is_thinking_unsupported_400(response.text):
                    self._thinking_unsupported = True
                    self.last_call["thinking_fallback"] = True
                    self.last_call["http_requests"] = 2
                    fallback_payload = {k: v for k, v in payload.items() if k != "thinking"}
                    response = _post(fallback_payload)
        except httpx.TimeoutException:
            self.last_call["outcome"] = "timeout"
            raise AppError("AI_TIMEOUT", "AI 服务超时，结果未知")
        except httpx.HTTPError:
            self.last_call["outcome"] = "unavailable"
            raise AppError("AI_UNAVAILABLE", "AI 服务暂时不可用")

        if response.status_code in (401, 403):
            self.last_call["outcome"] = "auth_failed"
            raise AppError("AI_AUTH_FAILED", "AI 服务认证失败，请检查服务端配置")
        if response.status_code == 429:
            self.last_call["outcome"] = "rate_limited"
            raise AppError("AI_RATE_LIMITED", "AI 服务限流，结果未知")
        if response.status_code >= 500:
            self.last_call["outcome"] = "unavailable"
            raise AppError("AI_UNAVAILABLE", "AI 服务暂时不可用")
        if response.status_code >= 400:
            self.last_call["outcome"] = "request_rejected"
            raise AppError("AI_REQUEST_REJECTED", "AI 服务拒绝了请求")

        try:
            data = _strict_json_loads(response.text)
        except (ValueError, RecursionError, OverflowError):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 服务返回的 HTTP 响应不是合法 JSON",
                details=["reason=invalid_json", "finish_reason=unknown", "source=provider_output"],
            )
        if not isinstance(data, dict):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 服务返回的响应形状非法",
                details=["reason=invalid_response_shape", "finish_reason=unknown", "source=provider_output"],
            )

        request_id = sanitize_external_request_id(data.get("id")) or sanitize_external_request_id(
            response.headers.get("x-request-id")
        )
        usage = sanitize_usage(data.get("usage"))
        self.last_call["request_id"] = request_id
        self.last_call["usage"] = usage

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 响应缺少合法的 choices/message/content",
                details=["reason=invalid_response_shape", "finish_reason=unknown", "source=provider_output"],
            )
        first_choice = choices[0]
        message = first_choice.get("message")
        if not isinstance(message, dict):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 响应缺少合法的 choices/message/content",
                details=["reason=invalid_response_shape", "finish_reason=unknown", "source=provider_output"],
            )

        finish_reason_omitted = (
            "finish_reason" not in first_choice or first_choice.get("finish_reason") is None
        )
        if finish_reason_omitted:
            raw_finish: str | None = None
            safe_finish = "unknown"
        else:
            candidate_finish = first_choice.get("finish_reason")
            if not isinstance(candidate_finish, str) or not candidate_finish.strip():
                self.last_call["outcome"] = "known_invalid"
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "AI 响应缺少合法的 choices/message/content",
                    details=["reason=invalid_response_shape", "finish_reason=unknown", "source=provider_output"],
                )
            raw_finish = candidate_finish.strip()
            safe_finish = raw_finish if raw_finish in ALLOWED_FINISH_REASONS else "other"

        self.last_call["finish_reason"] = safe_finish

        if raw_finish == "content_filter":
            self.last_call["outcome"] = "request_rejected"
            raise AppError(
                "AI_REQUEST_REJECTED",
                "AI 输出触发上游内容过滤",
                details=["reason=content_filter", f"finish_reason={safe_finish}"],
            )
        if raw_finish == "length":
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 输出因达到长度上限而未完整结束",
                details=["reason=output_truncated", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        if raw_finish == "aborted":
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 输出被服务端中止",
                details=["reason=output_aborted", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        if raw_finish == "insufficient_system_resource":
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 服务计算资源不足导致输出中断",
                details=["reason=insufficient_system_resource", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        tool_calls_val = message.get("tool_calls")
        if raw_finish == "tool_calls" or (
            tool_calls_val is not None and tool_calls_val != []
        ):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回了未请求的工具调用",
                details=["reason=unexpected_tool_calls", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        if not finish_reason_omitted and raw_finish != "stop":
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回了未知的结束原因",
                details=["reason=unknown_finish_reason", f"finish_reason={safe_finish}", "source=provider_output"],
            )

        if "content" not in message:
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 响应缺少合法的 choices/message/content",
                details=["reason=invalid_response_shape", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        content = message["content"]
        if content is None or (isinstance(content, str) and not content.strip()):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回内容不是非空字符串",
                details=["reason=empty_content", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        if not isinstance(content, str):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 响应缺少合法的 choices/message/content",
                details=["reason=invalid_response_shape", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        try:
            parsed = _strict_json_loads(content)
        except _DuplicateJsonKeyError:
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回的 JSON 包含重复键",
                details=["reason=duplicate_json_key", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        except _InvalidJsonNumberError:
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回的 JSON 包含非有限数值",
                details=["reason=invalid_json_number", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        except (json.JSONDecodeError, RecursionError, ValueError, OverflowError):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回内容不是合法 JSON",
                details=["reason=invalid_json", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        if not isinstance(parsed, dict):
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回的 JSON 必须是对象",
                details=["reason=non_object_json", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        try:
            _validate_json_text(parsed)
        except _InvalidJsonTextError:
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 返回的 JSON 包含不支持的控制字符或无效编码",
                details=["reason=invalid_json_text", f"finish_reason={safe_finish}", "source=provider_output"],
            )
        self.last_call["outcome"] = "success"
        return parsed, request_id

    def parse_material(
        self,
        *,
        text: str,
        kind: str,
        role_id: str,
        level: str,
        skill_catalog: list[dict[str, str]] | None = None,
        correction: dict | None = None,
    ) -> dict:
        from app.ai.prompts import parse_prompt

        system, user = parse_prompt(
            text=text,
            kind=kind,
            role_id=role_id,
            level=level,
            skill_catalog=skill_catalog,
            correction=correction,
        )
        try:
            parsed, _ = self._chat_json(system, user)
            finish_reason = self.last_call.get("finish_reason") or "unknown"
            model = ResumeProfile if kind == "resume" else JdProfile
            return _validate(model, parsed).model_dump(mode="json")
        except AppError as exc:
            if exc.code == "AI_INVALID_OUTPUT":
                self.last_call["outcome"] = "known_invalid"
                finish_reason = self.last_call.get("finish_reason") or "unknown"
                raise AppError(
                    exc.code,
                    exc.message,
                    details=_merge_stage_details(
                        exc.details,
                        stage="parse",
                        default_reason="invalid_profile_schema",
                        finish_reason=finish_reason,
                    ),
                ) from exc
            raise

    def _raise_invalid_match(self, message: str, extra_details: list[str]) -> None:
        self.last_call["outcome"] = "known_invalid"
        finish_reason = self.last_call.get("finish_reason") or "unknown"
        raise AppError(
            "AI_INVALID_OUTPUT",
            message,
            details=_merge_stage_details(
                extra_details,
                stage="match",
                default_reason="invalid_evidence_selection",
                finish_reason=finish_reason,
            ),
        )

    def match_requirements(self, *, resume_snapshot, jd_snapshot) -> list[dict]:
        from app.ai.prompts import match_prompt

        candidates, quote_by_id = _build_match_candidates(resume_snapshot)
        system, user = match_prompt(
            resume_text=resume_snapshot.text,
            resume_candidates=candidates,
            jd_profile=jd_snapshot.profile,
        )
        try:
            parsed, _ = self._chat_json(system, user)
            selection = _validate(MatchSelectionOutput, parsed)
        except AppError as exc:
            if exc.code == "AI_INVALID_OUTPUT":
                self.last_call["outcome"] = "known_invalid"
                finish_reason = self.last_call.get("finish_reason") or "unknown"
                raise AppError(
                    exc.code,
                    exc.message,
                    details=_merge_stage_details(
                        exc.details,
                        stage="match",
                        default_reason="invalid_evidence_selection",
                        finish_reason=finish_reason,
                    ),
                ) from exc
            raise

        requirement_ids = {
            item["item_id"]
            for item in ((jd_snapshot.profile or {}).get("requirements") or [])
            if isinstance(item, dict) and item.get("item_id")
        }
        seen_reqs: set[str] = set()
        rows: list[dict] = []
        for row in selection.rows:
            if row.requirement_id not in requirement_ids:
                unknown_req_count = sum(
                    1 for candidate in selection.rows if candidate.requirement_id not in requirement_ids
                )
                self._raise_invalid_match(
                    "匹配结果引用了不存在的 JD 要求",
                    ["field=requirement_id", f"unknown_count={unknown_req_count}"],
                )
            if row.requirement_id in seen_reqs:
                self._raise_invalid_match(
                    "匹配结果 JD 要求重复",
                    [f"requirement_id={row.requirement_id}"],
                )
            seen_reqs.add(row.requirement_id)

            seen_items: set[str] = set()
            evidence: list[dict] = []
            for item_id in row.resume_item_ids:
                if item_id in seen_items:
                    self._raise_invalid_match(
                        "匹配结果引用了重复的简历条目 ID",
                        [f"requirement_id={row.requirement_id}", f"item_id={item_id}"],
                    )
                if item_id not in quote_by_id:
                    unknown_item_count = sum(
                        1 for candidate_id in row.resume_item_ids if candidate_id not in quote_by_id
                    )
                    self._raise_invalid_match(
                        "匹配结果引用了不存在或不可引用的简历条目 ID",
                        [
                            f"requirement_id={row.requirement_id}",
                            "field=resume_item_ids",
                            f"unknown_count={unknown_item_count}",
                        ],
                    )
                seen_items.add(item_id)
                evidence.append(
                    {
                        "snapshot_id": str(resume_snapshot.id),
                        "item_id": item_id,
                        "quote": quote_by_id[item_id],
                    }
                )

            if row.status == "supported" and not evidence:
                self._raise_invalid_match(
                    "supported 匹配必须选择至少一条有效简历条目",
                    [f"requirement_id={row.requirement_id}", "status=supported"],
                )
            if row.status == "not_evidenced" and evidence:
                self._raise_invalid_match(
                    "not_evidenced 匹配不能附带支持条目",
                    [f"requirement_id={row.requirement_id}", "status=not_evidenced"],
                )

            rows.append(
                MatchRow(
                    requirement_id=row.requirement_id,
                    status=row.status,
                    resume_evidence=evidence,
                    explanation=row.explanation,
                    suggested_focus=row.suggested_focus,
                ).model_dump(mode="json")
            )

        missing = requirement_ids - seen_reqs
        if missing:
            self._raise_invalid_match(
                "匹配结果未覆盖全部 JD 要求",
                [f"requirement_id={req_id}" for req_id in sorted(missing)[:3]],
            )
        return rows

    def _plan_request_options(self, context: dict | None) -> dict[str, Any]:
        options: dict[str, Any] = {}
        thinking_mode = (self._settings.ai_plan_generate_thinking or "").strip().lower()
        if thinking_mode in {"disabled", "enabled"} and not self._thinking_unsupported:
            options["thinking"] = {"type": thinking_mode}
        correction = (context or {}).get("correction")
        corr_details = (
            [str(item) for item in (correction.get("details") or [])]
            if isinstance(correction, dict)
            else []
        )
        if "reason=duplicate_stem" in corr_details or "reason=semantic_duplicate" in corr_details:
            options["temperature"] = 0.5
        max_tokens = self._settings.ai_plan_generate_max_tokens
        if isinstance(max_tokens, int) and max_tokens > 0:
            if "reason=output_truncated" in corr_details or "finish_reason=length" in corr_details:
                retry_max = self._settings.ai_plan_generate_retry_max_tokens
                if isinstance(retry_max, int) and retry_max > max_tokens:
                    max_tokens = retry_max
            options["max_tokens"] = max_tokens
        return options

    def generate_questions(
        self,
        *,
        count: int,
        resume_snapshot,
        jd_snapshot,
        rubric: dict,
        avoid_stems: list[str],
        context: dict | None = None,
    ) -> list[dict]:
        from app.ai.prompts import generate_prompt

        system, user = generate_prompt(
            count=count,
            resume_text=resume_snapshot.text,
            resume_profile=resume_snapshot.profile,
            resume_snapshot_id=str(resume_snapshot.id),
            jd_text=jd_snapshot.text,
            jd_snapshot_id=str(jd_snapshot.id),
            jd_profile=jd_snapshot.profile,
            rubric=rubric,
            avoid_stems=avoid_stems,
            context=context,
        )
        parsed, _ = self._chat_json(
            system,
            user,
            request_options=self._plan_request_options(context),
        )
        finish_reason = self.last_call.get("finish_reason") or "unknown"
        items = parsed.get("questions") if isinstance(parsed, dict) else None
        if not isinstance(items, list) or not items:
            self.last_call["outcome"] = "known_invalid"
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 未返回生成题",
                details=["reason=invalid_question_schema", f"finish_reason={finish_reason}", "source=provider_output"],
            )
        validated: list[dict] = []
        for item in items:
            try:
                validated.append(
                    _validate(GeneratedQuestionOutput, item).model_dump(mode="json")
                )
            except AppError as exc:
                self.last_call["outcome"] = "known_invalid"
                raise AppError(
                    exc.code,
                    exc.message,
                    details=[
                        "reason=invalid_question_schema",
                        f"finish_reason={finish_reason}",
                        "source=provider_output",
                        *exc.details,
                    ],
                ) from exc
        return validated

    def review_questions(
        self,
        *,
        questions: list[dict],
        material_texts: list[str],
        fact_cards: list[dict] | None = None,
    ) -> dict:
        from app.ai.prompts import review_prompt

        system, user = review_prompt(
            questions=questions,
            material_texts=material_texts,
            fact_cards=fact_cards or [],
        )
        try:
            parsed, _ = self._chat_json(system, user)
            return _validate(DuplicateReviewOutput, parsed).model_dump(mode="json")
        except AppError as exc:
            if exc.code == "AI_INVALID_OUTPUT":
                self.last_call["outcome"] = "known_invalid"
                finish_reason = self.last_call.get("finish_reason") or "unknown"
                raise AppError(
                    exc.code,
                    exc.message,
                    details=_merge_stage_details(
                        exc.details,
                        stage="plan_review",
                        slot="semantic",
                        default_reason="invalid_review_schema",
                        finish_reason=finish_reason,
                    ),
                ) from exc
            raise

    def decide_follow_up(
        self,
        *,
        question: dict,
        answer_text: str,
        context: dict | None = None,
    ) -> dict:
        from app.ai.prompts import follow_up_prompt
        from app.schemas import FollowUpDecisionOutput

        system, user = follow_up_prompt(
            question=question, answer_text=answer_text, context=context
        )
        try:
            parsed, _ = self._chat_json(system, user)
            return _validate(FollowUpDecisionOutput, parsed).model_dump(mode="json")
        except AppError as exc:
            if exc.code == "AI_INVALID_OUTPUT":
                self.last_call["outcome"] = "known_invalid"
                finish_reason = self.last_call.get("finish_reason") or "unknown"
                raise AppError(
                    exc.code,
                    exc.message,
                    details=_merge_stage_details(
                        exc.details,
                        stage="follow_up",
                        default_reason="invalid_follow_up_schema",
                        finish_reason=finish_reason,
                    ),
                ) from exc
            raise

    def score_interview_question(
        self,
        *,
        question: dict,
        answers: list[dict],
        dimensions: list[dict],
    ) -> dict:
        from app.ai.prompts import score_prompt
        from app.schemas import ScoreQuestionOutput

        system, user = score_prompt(
            question=question, answers=answers, dimensions=dimensions
        )
        try:
            parsed, _ = self._chat_json(system, user)
            return _validate(ScoreQuestionOutput, parsed).model_dump(mode="json")
        except AppError as exc:
            if exc.code == "AI_INVALID_OUTPUT":
                self.last_call["outcome"] = "known_invalid"
                finish_reason = self.last_call.get("finish_reason") or "unknown"
                raise AppError(
                    exc.code,
                    exc.message,
                    details=_merge_stage_details(
                        exc.details,
                        stage="interview_score",
                        default_reason="invalid_score_schema",
                        finish_reason=finish_reason,
                    ),
                ) from exc
            raise
