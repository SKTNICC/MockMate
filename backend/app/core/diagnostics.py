"""模型输出边界、任务错误与调用记录的安全诊断投影。

所有来自外部模型或历史任务的错误路径、ID、用量与请求标识都必须经过本模块过滤，
防止模型自造字段名、提示注入标记、引文或内部异常文本泄漏到 Job API、日志或下一次纠正提示中。
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from app import schemas

RECOVERABLE_PROVIDER_OUTPUT_REASONS: frozenset[str] = frozenset(
    {
        "output_truncated",
        "output_aborted",
        "insufficient_system_resource",
        "unexpected_tool_calls",
        "unknown_finish_reason",
        "empty_content",
        "invalid_json",
        "duplicate_json_key",
        "invalid_json_number",
        "invalid_json_text",
        "non_object_json",
        "invalid_response_shape",
    }
)

RETRYABLE_PARSE_AI_REASONS: frozenset[str] = RECOVERABLE_PROVIDER_OUTPUT_REASONS | {
    "invalid_profile_schema",
    "invalid_jd_catalog_role",
    "invalid_work_experience",
    "missing_work_experience_fields",
}

RETRYABLE_PARSE_EVIDENCE_REASONS: frozenset[str] = frozenset(
    {
        "blank_quote",
        "quote_not_in_text",
        "invalid_evidence_origin",
        "ungrounded_work_experience",
        "blank_field",
        "duplicate_item_id",
        "empty_profile",
        "kind_mismatch",
    }
)

PARSE_SCHEMA_ROOT_FIELDS: frozenset[str] = frozenset(
    {
        "kind",
        "education",
        "work_experience",
        "projects",
        "skills",
        "role_id",
        "level",
        "title",
        "responsibilities",
        "requirements",
    }
)

LEGACY_PARSE_SCHEMA_MESSAGE = "AI 输出不符合契约结构"
LEGACY_PARSE_PROFILE_MESSAGES: frozenset[str] = frozenset(
    {
        "简历至少需要一条教育、工作、项目或技能信息",
        "JD 至少需要一条职责或要求",
        "item_id 不能重复",
    }
)

SAFE_STAGES: frozenset[str] = frozenset(
    {
        "extract",
        "parse",
        "match",
        "plan",
        "plan_review",
        "follow_up",
        "interview_score",
        "interview_report",
    }
)

SAFE_REASONS: frozenset[str] = (
    RETRYABLE_PARSE_AI_REASONS
    | RETRYABLE_PARSE_EVIDENCE_REASONS
    | frozenset(
        {
            "content_filter",
            "invalid_evidence_selection",
            "quote_mismatch_item",
            "invalid_question_schema",
            "invalid_question_count",
            "question_type_mismatch",
            "duplicate_stem",
            "duplicate_normalized_stem",
            "candidate_stem_reused",
            "invalid_anchor_selection",
            "unknown_skill_code",
            "invalid_review_schema",
            "invalid_review_reference",
            "semantic_duplicate",
            "semantic_uncertain",
            "grounding_issue",
            "slot_validation_failed",
            "invalid_follow_up_schema",
            "invalid_follow_up_advance",
            "invalid_follow_up_prompt",
            "invalid_follow_up_quote",
            "invalid_follow_up_target",
            "duplicate_follow_up",
            "invalid_score_schema",
            "unknown_dimension_id",
            "invalid_cached_score_slot",
        }
    )
)

SAFE_SOURCES: frozenset[str] = frozenset(
    {"provider_output", "user_confirmed", "extracted", "bank", "prior_generated"}
)

SAFE_STATUSES: frozenset[str] = frozenset(
    {
        "supported",
        "to_verify",
        "not_evidenced",
        "queued",
        "running",
        "succeeded",
        "failed",
        "interrupted",
        "superseded",
    }
)

SAFE_SECTIONS: frozenset[str] = frozenset(
    {
        "education",
        "work_experience",
        "projects",
        "skills",
        "responsibilities",
        "requirements",
    }
)

SAFE_FINISH_REASONS: frozenset[str] = frozenset(
    {
        "stop",
        "length",
        "content_filter",
        "aborted",
        "insufficient_system_resource",
        "tool_calls",
        "unknown",
        "other",
    }
)

SAFE_DUPLICATE_KINDS: frozenset[str] = frozenset({"exact", "near", "duplicate"})
SAFE_CONFLICT_SOURCES: frozenset[str] = frozenset({"prior_generated", "bank"})
SAFE_LAST_CODES: frozenset[str] = frozenset(
    {
        "AI_INVALID_OUTPUT",
        "AI_TIMEOUT",
        "AI_UNAVAILABLE",
        "AI_RATE_LIMITED",
        "AI_AUTH_FAILED",
        "AI_REQUEST_REJECTED",
        "EVIDENCE_INVALID",
        "EVALUATION_INVALID",
        "DUPLICATE_QUESTION",
        "DEDUP_UNCERTAIN",
        "GROUNDING_INVALID",
        "PROFILE_INVALID",
        "BANK_INSUFFICIENT",
        "INTERNAL_ERROR",
    }
)
SAFE_JOB_ERROR_CODES: frozenset[str] = SAFE_LAST_CODES | frozenset(
    {
        "JOB_LEASE_LOST",
        "AI_NOT_CONFIGURED",
        "FILE_TEXT_UNAVAILABLE",
        "JOB_INTERRUPTED",
        "CONSENT_REQUIRED",
        "JD_LEVEL_CONFLICT",
        "JOB_INPUT_CONFLICT",
        "MATERIALS_REQUIRED",
        "MATCH_REQUIRED",
        "PLAN_STALE",
        "PLAN_STATE_INVALID",
        "REVISION_CONFLICT",
        "NOT_FOUND",
        "INVALID_INPUT",
        "UNSUPPORTED_FILE_TYPE",
        "FILE_TOO_LARGE",
        "TEXT_INVALID",
        "VALIDATION_ERROR",
        "ROLE_LEVEL_MISMATCH",
        "ROLE_NOT_AVAILABLE",
        "BANK_INELIGIBLE",
        "INTERVIEW_STATE_INVALID",
        "REPORT_NOT_AVAILABLE",
        "REPORT_PENDING",
        "REPORT_FAILED",
    }
)
SAFE_EXPECTED_ACTUAL_VALUES: frozenset[str] = frozenset(
    {
        "resume",
        "jd",
        "experience",
        "knowledge",
        "invalid_value",
        "text/plain",
        "application/pdf",
        "application/x-pdf",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
SAFE_DIMENSION_IDS: frozenset[str] = frozenset(
    {
        "core",
        "accuracy",
        "depth",
        "clarity",
        "expression",
        "completeness",
        "communication",
        "logic",
        "structure",
    }
)
SAFE_BARE_STATIC_TOKENS: frozenset[str] = frozenset(
    {
        "generated-1",
        "generated-2",
        "generated-3",
        "role_id",
        "level",
        "text",
        "title",
        "cursor",
        "Idempotency-Key",
        "education",
        "work_experience",
        "projects",
        "skills",
        "responsibilities",
        "requirements",
        "ValidationError",
    }
)

KNOWN_SAFE_JOB_ERROR_MESSAGES: frozenset[str] = frozenset(
    {
        "AI 服务超时，结果未知",
        "AI 服务暂时不可用",
        "AI 服务限流，结果未知",
        "AI 服务认证失败，请检查服务端配置",
        "AI 服务拒绝了请求",
        "AI 输出不符合契约",
        "AI 输出不符合契约结构",
        "AI 输出触发上游内容过滤",
        "AI 输出因达到长度上限而未完整结束",
        "AI 输出被服务端中止",
        "AI 服务计算资源不足导致输出中断",
        "AI 返回了未请求的工具调用",
        "AI 返回了未知的结束原因",
        "AI 响应缺少合法的 choices/message/content",
        "AI 返回内容不是非空字符串",
        "AI 返回的 JSON 包含重复键",
        "AI 返回的 JSON 包含非有限数值",
        "AI 返回内容不是合法 JSON",
        "AI 返回的 JSON 必须是对象",
        "AI 返回的 JSON 包含不支持的控制字符或无效编码",
        "AI 服务未配置",
        "AI 服务未配置，无法执行模型任务",
        "测试 provider 只能在隔离测试环境启用",
        "任务租约已失效",
        "任务租约已失效，不能写入槽位结果",
        "题库可用题不足",
        "题库与生成题数量不满足各半要求",
        "JD 缺少显式技能要求",
        "JD 缺少显式技能要求，无法建立题库主问题的依据",
        "文件无法提取文本",
        "任务执行中断，外部结果未知",
        "任务执行中断，外部结果未知；不会自动重发外部调用",
        "题目或匹配引用无依据",
        "评价依据不完整",
        "本场存在重复主问题",
        "存在重复主问题",
        "语义去重复核发现重复题目",
        "语义去重结果不确定，不能发布 ready 计划",
        "语义去重不确定",
        "语义复核发现无依据陈述",
        "存在无依据陈述",
        "未确认材料外发",
        "JD 原文与当前岗位/级别设置冲突",
        "JD 原文明确要求资深或多年经验，不能自动改写为实习/初级；请修改 JD 或手动核对结构",
        "JD 解析出的岗位或级别不在服务端目录中",
        "任务执行失败",
        "工作经历缺少明确的组织或职务，请核对原文",
        "测试 provider 无法从该简历文本结构化出任何条目，请手动填写或更换文本",
        "测试 provider 未找到职责或要求行，请手动填写 JD 结构",
        "JD 没有可分析的显式要求",
        "没有可用于出题的本场材料锚点",
        "knowledge 槽位没有可用依据卡",
        "AI 未返回生成题",
        "模型每个槽位必须返回且只返回 1 道题",
        "生成槽位在有限纠正后仍不合法",
        "生成题类型与本次槽位要求不一致",
        "生成题与本次禁止题干重复",
        "生成题锚点不在本次允许锚点列表中",
        "生成题包含未启用的技能编码",
        "生成题不能自称已核验",
        "生成题包含未解决的不确定性，不能进入 ready 计划",
        "生成题缺少合理替代解",
        "评分要点权重之和不为 1",
        "评分量表维度权重之和不为 1",
        "生成题评分量表与本次提供的不一致",
        "生成题评价依据引用了不存在的材料条目",
        "生成题评价依据引用了不在本次允许锚点列表中的材料条目",
        "生成题引用了本次未提供的依据卡",
        "技术知识生成题必须引用本次提供的已审依据卡",
        "经历／推理生成题必须引用本场材料",
        "语义复核输出结构不符合契约",
        "语义复核包含未知或非法题目引用",
        "语义复核包含重复关系",
        "语义复核引用了未知题目",
        "语义复核包含重复无依据声明",
        "材料版本已变化",
        "材料没有可解析文本",
        "准备已删除",
        "材料版本已失效",
        "匹配结果引用了不存在的 JD 要求",
        "匹配结果 JD 要求重复",
        "匹配结果引用了重复的简历条目 ID",
        "匹配结果引用了不存在或不可引用的简历条目 ID",
        "supported 匹配必须选择至少一条有效简历条目",
        "supported 匹配必须有至少一条有效简历证据",
        "not_evidenced 匹配不能附带支持条目",
        "not_evidenced 匹配不能附带支持证据",
        "匹配结果未覆盖全部 JD 要求",
        "匹配说明与准备建议不能为空白",
        "题目或证据引用了不属于本场的材料",
        "引用原文不能为空白",
        "引用的条目不在材料结构中",
        "引用原文不属于所引用的材料条目",
        "引用原文在当前材料中不存在",
        "profile 必须是对象",
        "profile 类型必须与材料类型一致",
        "缺少合法的 profile.kind",
        "每个条目必须有 item_id",
        "item_id 不能重复",
        "条目必填字段不能为空白",
        "每个条目必须有非空白原文引用",
        "引用内容必须能在当前原文中找到",
        "证据来源标记非法",
        "模型解析结果只能标记为 extracted；user_confirmed 仅用于用户核对提交",
        "工作经历的单位必须在同条引文中有依据",
        "工作经历的职务必须在同条引文中有依据",
        "工作经历引文不能跨越多个分隔条目",
        "工作经历表格行无法按表头可靠对齐列，请核对原文",
        "技能编码不在已启用词表中",
        "简历至少需要一条教育、工作、项目或技能信息",
        "JD 至少需要一条职责或要求",
        "追问判定输出结构不符合契约",
        "无需追问时不能携带问题或引文",
        "追问长度必须在 8–220 码点之间",
        "追问引文必须逐字出现在本题主回答中",
        "追问缺少考察点",
        "追问包含参考答案、评分或点评内容",
        "追问与主问题重复",
        "追问与主问题过于相似",
        "追问与已曝光题目重复",
        "追问与已曝光题目过于相似",
        "追问任务已变化，请刷新",
        "追问任务已不是当前任务",
        "追问任务输入已过期",
        "同一主问题不能生成第二个追问回合",
        "评分输出结构不符合契约",
        "评分输出引用了其他题目",
        "评分输出包含未知维度",
        "评分输出包含重复维度",
        "scored 维度必须给出 0–4 分",
        "scored 维度必须引用原话",
        "非 scored 维度不能携带分数",
        "非 scored 维度不能携带引文",
        "评分输出缺少维度",
        "评分结果不完整，不能发布报告",
        "生成期间题库内容或审核准入已变化，不能发布旧计划",
        "生成期间依据卡审核状态已变化，不能发布旧计划",
        "服务器缺少 PDF 解析组件",
        "服务器缺少 DOCX 解析组件",
        "材料没有原文件",
        "文件路径非法",
        "原文件不存在，请重新上传",
        "无法以 UTF-8 读取该 TXT 文件，请另存为 UTF-8 后重试或粘贴文本",
        "PDF 已加密，无法提取文本；请粘贴文本后继续",
        "PDF 文件无法解析，可能已损坏或不是 PDF；也可粘贴文本",
        "DOCX 文件无法解析，可能已损坏或不是 DOCX；也可粘贴文本",
        "文件没有可提取的文字（可能是扫描件）。请粘贴文本或换一份可复制文本的文件",
        "仅支持 PDF、DOCX、TXT",
        "缺少文件名",
        "不支持 .doc，请另存为 .docx 或 PDF",
        "仅支持 PDF、DOCX、TXT 文件",
        "文件声明类型与扩展名不一致",
        "文件为空",
        "面试不存在",
        "面试已结束",
        "面试已不在追问判定阶段",
        "面试尚未结束",
        "报告任务已不是当前任务",
    }
)

SAFE_VALIDATION_ERROR_LABELS: dict[str, str] = {
    "missing": "Field required",
    "extra_forbidden": "Extra inputs are not permitted",
    "string_type": "Input should be a valid string",
    "string_too_short": "String is too short",
    "string_too_long": "String is too long",
    "string_pattern_mismatch": "String does not match pattern",
    "int_type": "Input should be a valid integer",
    "int_parsing": "Input should be a valid integer",
    "int_from_float": "Input should be a valid integer",
    "float_type": "Input should be a valid number",
    "float_parsing": "Input should be a valid number",
    "bool_type": "Input should be a valid boolean",
    "bool_parsing": "Input should be a valid boolean",
    "list_type": "Input should be a valid list",
    "too_short": "List is too short",
    "too_long": "List is too long",
    "model_type": "Input should be a valid object",
    "dict_type": "Input should be a valid object",
    "literal_error": "Input does not match allowed values",
    "uuid_type": "Input should be a valid UUID",
    "uuid_parsing": "Input should be a valid UUID",
    "greater_than": "Number out of range",
    "greater_than_equal": "Number out of range",
    "less_than": "Number out of range",
    "less_than_equal": "Number out of range",
    "json_invalid": "Invalid JSON",
    "json_type": "Invalid JSON",
    "union_tag_invalid": "Invalid union tag",
    "union_tag_not_found": "Missing union tag",
}

SAFE_VALIDATION_MESSAGES: frozenset[str] = frozenset(
    set(SAFE_VALIDATION_ERROR_LABELS.values()) | {"value_error", "invalid_value"}
)

SUPPORTED_USAGE_FIELDS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)

EXTERNAL_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9._:-]{1,64}$")
SAFE_TOKEN_RE = re.compile(r"^[a-zA-Z0-9._/\[\]-]{1,80}$")
_NONNEG_INT_RE = re.compile(r"^\d{1,10}$")
_DECIMAL_RE = re.compile(r"^\d{1,4}(?:\.\d{1,4})?$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SLOT_RE = re.compile(r"^(?:generated-[1-3]|semantic)$")
_SAFE_ITEM_ID_RE = re.compile(
    r"^(?:edu|work|project|skill|resp|responsibility|req|education|work_experience|"
    r"projects|skills|responsibilities|requirements)-[0-9]+$"
)
_SAFE_REQ_ID_RE = re.compile(r"^(?:req|requirement|requirements)-[0-9]+$")
_SAFE_BANK_ID_RE = re.compile(r"^M[0-9]+-[A-Z0-9-]+$")
_SAFE_MATERIAL_LIMIT_MSG_RE = re.compile(r"^材料文本超过 \d+ 字符上限$")
_SAFE_FILE_LIMIT_MSG_RE = re.compile(r"^文件超过 \d+ MiB 上限$")
SAFE_BARE_ID_RE = re.compile(
    r"^(generated-[1-3]|skill-[0-9]+|req-[0-9]+|project-[0-9]+|work-[0-9]+|"
    r"edu-[0-9]+|resp-[0-9]+|responsibility-[0-9]+|role_id|level|text|title|"
    r"cursor|Idempotency-Key|education|work_experience|projects|skills|"
    r"responsibilities|requirements|M[0-9]+-[A-Z0-9-]+|ValidationError)$"
)

KNOWN_LEGACY_SAFE_DETAILS: frozenset[str] = frozenset(
    {
        "AI 返回内容不是合法 JSON",
        "生成题与本次禁止题干重复",
        "生成题类型与本次槽位要求不一致",
        "AI 输出不符合契约结构",
    }
)

SAFE_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "stage",
        "reason",
        "slot",
        "field",
        "section",
        "item_id",
        "requirement_id",
        "dimension_id",
        "finish_reason",
        "source",
        "last_code",
        "duplicate_kind",
        "conflict_source",
        "conflict_slot",
        "conflict_question_id",
        "similarity",
        "threshold",
        "required",
        "available",
        "returned",
        "current_attempt",
        "current_revision",
        "expected",
        "actual",
        "length",
        "uncertainties_count",
        "unknown_count",
        "duplicate_count",
        "status",
    }
)

SAFE_RECORD_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "stage",
        "reason",
        "slot",
        "field",
        "section",
        "item_id",
        "requirement_id",
        "dimension_id",
        "finish_reason",
        "source",
        "duplicate_kind",
        "conflict_source",
        "conflict_slot",
        "conflict_question_id",
        "similarity",
        "threshold",
        "returned",
        "uncertainties_count",
        "unknown_count",
        "duplicate_count",
        "status",
    }
)

UNRECOGNIZED_DETAIL_TOKEN = "unrecognized_detail"


def _collect_schema_field_names() -> frozenset[str]:
    names: set[str] = {
        "root",
        "extra_field",
        "unknown_field",
        "rows",
        "resume_item_ids",
        "requirement_id",
        "status",
        "explanation",
        "suggested_focus",
        "questions",
    }
    for obj in vars(schemas).values():
        if isinstance(obj, type) and issubclass(obj, BaseModel):
            names.update(obj.model_fields.keys())
    return frozenset(names)


KNOWN_SCHEMA_FIELD_NAMES: frozenset[str] = _collect_schema_field_names()


def _unwrap_candidate_types(annotation: Any) -> list[Any]:
    if annotation is None or annotation is type(None):
        return []
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        return _unwrap_candidate_types(args[0]) if args else []
    if origin in (Union, types.UnionType):
        out: list[Any] = []
        for arg in get_args(annotation):
            out.extend(_unwrap_candidate_types(arg))
        return out
    return [annotation]


def _sanitize_loc(
    loc: tuple[Any, ...],
    err_type: str,
    model: type[BaseModel] | None,
) -> str:
    if not loc:
        return "root"
    if model is None:
        return sanitize_schema_field_path(
            ".".join(str(part) for part in loc),
            extra_hint=(err_type == "extra_forbidden"),
        )

    current_types: list[Any] = [model]
    safe_parts: list[str] = []
    for part in loc:
        if isinstance(part, int) and not isinstance(part, bool) and part >= 0:
            next_types: list[Any] = []
            matched_seq = False
            for candidate in current_types:
                origin = get_origin(candidate)
                if origin in (list, tuple, set, frozenset):
                    matched_seq = True
                    args = get_args(candidate)
                    if args:
                        next_types.extend(_unwrap_candidate_types(args[0]))
            if matched_seq:
                safe_parts.append(str(part))
                current_types = next_types
                continue
            safe_parts.append("unknown_field")
            break

        if isinstance(part, str):
            next_types = []
            matched_field = False
            for candidate in current_types:
                if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                    field_info = candidate.model_fields.get(part)
                    if field_info is not None:
                        matched_field = True
                        next_types.extend(_unwrap_candidate_types(field_info.annotation))
            if matched_field:
                safe_parts.append(part)
                current_types = next_types
                continue
            safe_parts.append("extra_field" if err_type == "extra_forbidden" else "unknown_field")
            break

        safe_parts.append("unknown_field")
        break

    return ".".join(safe_parts) if safe_parts else "root"


def _fixed_validation_label(err_type: str) -> str:
    if err_type.startswith("value_error"):
        return "value_error"
    return SAFE_VALIDATION_ERROR_LABELS.get(err_type, "invalid_value")


def safe_validation_details(
    exc: ValidationError,
    model: type[BaseModel] | None = None,
) -> list[str]:
    """生成不包含模型输入、自定义验证器文本或未知字段名的安全校验摘要。"""
    details: list[str] = []
    first_field: str | None = None
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        err_type = str(error.get("type") or "")
        loc_tuple = tuple(error.get("loc") or ())
        loc = _sanitize_loc(loc_tuple, err_type, model)
        if loc != "root" and first_field is None:
            first_field = loc
        label = _fixed_validation_label(err_type)
        entry = f"{loc}: {label}"
        if entry not in details:
            details.append(entry)
        if len(details) >= 4:
            break
    if first_field is not None:
        details.insert(0, f"field={first_field}")
    return details[:5]


def is_safe_schema_field_path(path: str) -> bool:
    stripped = (path or "").strip()
    if not stripped or len(stripped) > 80:
        return False
    parts = stripped.split(".")
    for part in parts:
        if not part:
            return False
        if part.isdigit():
            continue
        if part not in KNOWN_SCHEMA_FIELD_NAMES:
            return False
    return True


def sanitize_schema_field_path(path: str, *, extra_hint: bool = False) -> str:
    stripped = (path or "").strip()
    if not stripped:
        return "unknown_field"
    parts = stripped.split(".")
    safe_parts: list[str] = []
    for part in parts:
        if part.isdigit():
            safe_parts.append(part)
        elif part in KNOWN_SCHEMA_FIELD_NAMES:
            safe_parts.append(part)
        else:
            safe_parts.append("extra_field" if extra_hint else "unknown_field")
            break
    return ".".join(safe_parts) if safe_parts else "unknown_field"


def sanitize_usage(usage: object) -> dict[str, int] | None:
    if not isinstance(usage, dict):
        return None
    projected: dict[str, int] = {}
    for key in SUPPORTED_USAGE_FIELDS:
        val = usage.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            projected[key] = val
    return projected or None


def sanitize_external_request_id(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    stripped = raw.strip()
    if not stripped or not EXTERNAL_REQUEST_ID_RE.match(stripped):
        return ""
    return stripped


def safe_field_paths(details: object) -> list[str]:
    if not isinstance(details, list):
        return []
    paths: list[str] = []
    for entry in details:
        if not isinstance(entry, str):
            continue
        candidate = ""
        if entry.startswith("field="):
            candidate = entry.partition("=")[2].strip()
        elif ":" in entry:
            candidate = entry.partition(":")[0].strip()
        if candidate and is_safe_schema_field_path(candidate) and candidate not in paths:
            paths.append(candidate)
            if len(paths) >= 4:
                break
    return paths


def parse_detail_kv(details: object) -> dict[str, str]:
    if not isinstance(details, list):
        return {}
    kv: dict[str, str] = {}
    for entry in details:
        if isinstance(entry, str) and "=" in entry:
            key, _, value = entry.partition("=")
            kv[key.strip()] = value.strip()
    return kv


def is_legacy_parse_schema_detail(entry: object) -> bool:
    if not isinstance(entry, str) or ":" not in entry:
        return False
    field_path, _, msg = entry.partition(":")
    field_path = field_path.strip()
    if not field_path or not msg.strip():
        return False
    root = field_path.split(".", 1)[0]
    return root in PARSE_SCHEMA_ROOT_FIELDS


def is_recoverable_parse_error(
    code: str | None,
    details: object,
    *,
    message: object = None,
    allow_legacy: bool = False,
) -> bool:
    """解析阶段错误纠正与显式重试共用的统一可恢复判断。"""
    detail_list = details if isinstance(details, list) else []
    kv = parse_detail_kv(detail_list)
    if code == "AI_INVALID_OUTPUT":
        if kv.get("stage") in (None, "parse") and kv.get("reason") in RETRYABLE_PARSE_AI_REASONS:
            return True
        if allow_legacy and message == LEGACY_PARSE_SCHEMA_MESSAGE and detail_list:
            reason = kv.get("reason")
            if reason is not None and reason not in RETRYABLE_PARSE_AI_REASONS:
                return False
            return any(is_legacy_parse_schema_detail(entry) for entry in detail_list)
        return False
    if code in ("EVIDENCE_INVALID", "PROFILE_INVALID"):
        if kv.get("stage") in (None, "parse") and kv.get("reason") in RETRYABLE_PARSE_EVIDENCE_REASONS:
            # 对来自模型输出的解析失败，非 legacy 路径要求显式 stage=parse（worker 入口必带）
            if kv.get("stage") == "parse" or allow_legacy:
                return True
        if allow_legacy and code == "PROFILE_INVALID" and message in LEGACY_PARSE_PROFILE_MESSAGES:
            return True
        return False
    return False


def sanitize_job_error_code(
    raw_code: object,
    *,
    fallback: str = "INTERNAL_ERROR",
) -> str:
    """仅保留受控任务错误码白名单；未知或非法类型回退到通用错误码。"""
    if isinstance(raw_code, str):
        stripped = raw_code.strip()
        if stripped in SAFE_JOB_ERROR_CODES:
            return stripped
    return fallback


def sanitize_job_request_id(
    raw_request_id: object,
    *,
    fallback_job_id: object,
) -> str:
    """保留有效的后台任务 UUID 追踪标识；无法验证的历史文本回退服务端 Job ID。"""
    if isinstance(raw_request_id, str):
        stripped = raw_request_id.strip()
        if stripped and _UUID_RE.match(stripped):
            return stripped
    return str(fallback_job_id)


def normalize_job_error_envelope(
    raw_error: object,
    *,
    fallback_code: object = None,
    job_id: object = "",
) -> dict[str, Any] | None:
    """在历史错误对象进入投影/重试边界时规范化容器、错误码、详情和请求标识。"""
    if raw_error is None:
        return None
    if isinstance(raw_error, Mapping) and len(raw_error) == 0:
        return None
    safe_fallback_code = sanitize_job_error_code(fallback_code, fallback="INTERNAL_ERROR")
    if not isinstance(raw_error, Mapping):
        return {
            "code": safe_fallback_code,
            "message": None,
            "details": [],
            "request_id": str(job_id),
        }
    has_code = "code" in raw_error and raw_error.get("code") is not None
    code = (
        sanitize_job_error_code(raw_error.get("code"), fallback="INTERNAL_ERROR")
        if has_code
        else safe_fallback_code
    )
    raw_message = raw_error.get("message")
    message = raw_message if isinstance(raw_message, str) else None
    raw_details = raw_error.get("details")
    details = (
        [entry for entry in raw_details if isinstance(entry, str)]
        if isinstance(raw_details, list)
        else []
    )
    request_id = sanitize_job_request_id(
        raw_error.get("request_id"),
        fallback_job_id=job_id,
    )
    return {
        "code": code,
        "message": message,
        "details": details,
        "request_id": request_id,
    }


def resolve_job_error_envelope(
    raw_error: object,
    *,
    trusted_code: object,
    job_id: object,
    terminal: bool,
) -> dict[str, Any] | None:
    """Job 投影与历史错误兼容共用的受控错误视图。

    失败或中断任务始终给出最小的安全诊断：优先采用已验证的服务端错误列，
    列为空或非法时才采用白名单内的旧 JSON 错误码。旧 JSON 错误码未知、
    与可信列冲突或错误体为空时，不保留其消息与明细，只保留合法追踪标识。
    """
    normalized = normalize_job_error_envelope(
        raw_error, fallback_code=trusted_code, job_id=job_id
    )
    trusted = sanitize_job_error_code(trusted_code, fallback="")
    raw_mapping = raw_error if isinstance(raw_error, Mapping) else None
    raw_code_present = (
        raw_mapping is not None
        and "code" in raw_mapping
        and raw_mapping.get("code") is not None
    )
    json_code = (
        sanitize_job_error_code(raw_mapping.get("code"), fallback="")
        if raw_mapping is not None and raw_code_present
        else ""
    )

    if trusted:
        if normalized is None:
            if not terminal:
                return None
            return {
                "code": trusted,
                "message": None,
                "details": [],
                "request_id": str(job_id),
            }
        if not terminal:
            return normalized
        if raw_code_present and json_code != trusted:
            return {
                "code": trusted,
                "message": None,
                "details": [],
                "request_id": normalized["request_id"],
            }
        return {**normalized, "code": trusted}

    if normalized is not None:
        if raw_code_present and not json_code:
            return {
                "code": "INTERNAL_ERROR",
                "message": None,
                "details": [],
                "request_id": normalized["request_id"],
            }
        return normalized
    if terminal:
        return {
            "code": "INTERNAL_ERROR",
            "message": None,
            "details": [],
            "request_id": str(job_id),
        }
    return None


def sanitize_job_error_message(
    raw_message: object,
    code: str | None,
    fallback_map: dict[str, str],
) -> str:
    """Job API 安全消息视图：仅保留已知固定中文文案，无法识别的历史消息回退标准文案。"""
    if isinstance(raw_message, str):
        trimmed = raw_message.strip()
        if trimmed in KNOWN_SAFE_JOB_ERROR_MESSAGES:
            return trimmed
        if _SAFE_MATERIAL_LIMIT_MSG_RE.match(trimmed) or _SAFE_FILE_LIMIT_MSG_RE.match(trimmed):
            return trimmed
    return fallback_map.get(code or "", "任务执行失败")


def _is_safe_detail_kv(
    key_s: str,
    val_s: str,
    *,
    kind: str | None = None,
    allowed_refs: dict[str, set[str]] | None = None,
) -> bool:
    if key_s == "stage":
        return val_s in SAFE_STAGES
    if key_s == "reason":
        return val_s in SAFE_REASONS
    if key_s == "source":
        return val_s in SAFE_SOURCES
    if key_s == "status":
        return val_s in SAFE_STATUSES
    if key_s == "section":
        return val_s in SAFE_SECTIONS
    if key_s == "finish_reason":
        return val_s in SAFE_FINISH_REASONS
    if key_s == "duplicate_kind":
        return val_s in SAFE_DUPLICATE_KINDS
    if key_s == "conflict_source":
        return val_s in SAFE_CONFLICT_SOURCES
    if key_s == "last_code":
        return val_s in SAFE_LAST_CODES
    if key_s in ("slot", "conflict_slot"):
        return bool(_SLOT_RE.match(val_s) or _UUID_RE.match(val_s))
    if key_s in (
        "required",
        "available",
        "returned",
        "current_attempt",
        "current_revision",
        "length",
        "uncertainties_count",
        "unknown_count",
        "duplicate_count",
    ):
        return bool(_NONNEG_INT_RE.match(val_s))
    if key_s in ("similarity", "threshold"):
        return bool(_DECIMAL_RE.match(val_s))
    if key_s in ("expected", "actual"):
        return val_s in SAFE_EXPECTED_ACTUAL_VALUES or bool(_NONNEG_INT_RE.match(val_s))
    if key_s == "item_id":
        if kind == "parse":
            return False
        if allowed_refs is not None and "item_ids" in allowed_refs:
            return val_s in allowed_refs["item_ids"]
        return bool(_SAFE_ITEM_ID_RE.match(val_s) or _UUID_RE.match(val_s))
    if key_s == "requirement_id":
        if allowed_refs is not None and "requirement_ids" in allowed_refs:
            return val_s in allowed_refs["requirement_ids"]
        return bool(_SAFE_REQ_ID_RE.match(val_s) or _UUID_RE.match(val_s))
    if key_s == "dimension_id":
        if allowed_refs is not None and "dimension_ids" in allowed_refs:
            return val_s in allowed_refs["dimension_ids"]
        return val_s in SAFE_DIMENSION_IDS or bool(_UUID_RE.match(val_s))
    if key_s == "conflict_question_id":
        if allowed_refs is not None and "question_ids" in allowed_refs:
            return val_s in allowed_refs["question_ids"]
        return bool(_SAFE_BANK_ID_RE.match(val_s) or _UUID_RE.match(val_s))
    return False


def sanitize_error_details_for_records(
    details: object,
    *,
    kind: str | None = None,
    allowed_refs: dict[str, set[str]] | None = None,
) -> list[str]:
    """Worker 内部日志、调用记录与纠正提示共用的安全 detail 过滤器。"""
    if not isinstance(details, list):
        return []
    safe: list[str] = []
    for entry in details:
        if not isinstance(entry, str):
            continue
        trimmed = entry.strip()
        if not trimmed:
            continue
        if "=" in trimmed:
            key, _, val = trimmed.partition("=")
            key_s = key.strip()
            val_s = val.strip()
            if key_s not in SAFE_RECORD_DETAIL_KEYS or "text" in key_s.lower():
                continue
            if key_s == "field":
                if not is_safe_schema_field_path(val_s):
                    val_s = sanitize_schema_field_path(val_s)
                item = f"field={val_s}"
            elif _is_safe_detail_kv(key_s, val_s, kind=kind, allowed_refs=allowed_refs):
                item = f"{key_s}={val_s}"
            else:
                continue
        elif ":" in trimmed:
            path, _, msg = trimmed.partition(":")
            path_s = path.strip()
            msg_s = msg.strip()
            if not is_safe_schema_field_path(path_s):
                path_s = sanitize_schema_field_path(
                    path_s, extra_hint=("Extra inputs" in msg_s)
                )
            if msg_s not in SAFE_VALIDATION_MESSAGES:
                msg_s = "invalid_value"
            item = f"{path_s}: {msg_s}"
        else:
            continue
        if item not in safe:
            safe.append(item)
        if len(safe) >= 6:
            break
    return safe


def sanitize_error_details_for_api(
    details: object,
    *,
    kind: str | None = None,
    code: str | None = None,
    allowed_refs: dict[str, set[str]] | None = None,
) -> list[str]:
    """Job API 读出路径的兼容过滤：保留已知旧消息与合法路径，脱敏未知内容。"""
    if not isinstance(details, list):
        return []
    safe: list[str] = []
    for entry in details:
        if not isinstance(entry, str):
            continue
        trimmed = entry.strip()
        if not trimmed:
            continue
        item: str | None = None
        if "=" in trimmed:
            key, _, val = trimmed.partition("=")
            key_s = key.strip()
            val_s = val.strip()
            if key_s in SAFE_DETAIL_KEYS and "text" not in key_s.lower():
                if key_s == "field":
                    item = f"field={sanitize_schema_field_path(val_s)}"
                elif (
                    kind == "parse"
                    and key_s == "item_id"
                    and code in ("AI_INVALID_OUTPUT", "EVIDENCE_INVALID", "PROFILE_INVALID")
                ):
                    item = UNRECOGNIZED_DETAIL_TOKEN
                elif _is_safe_detail_kv(key_s, val_s, kind=kind, allowed_refs=allowed_refs):
                    item = f"{key_s}={val_s}"
                else:
                    item = UNRECOGNIZED_DETAIL_TOKEN
            else:
                item = UNRECOGNIZED_DETAIL_TOKEN
        elif ":" in trimmed:
            path, _, msg = trimmed.partition(":")
            path_s = path.strip()
            msg_s = msg.strip()
            clean_path = sanitize_schema_field_path(
                path_s, extra_hint=("Extra inputs" in msg_s)
            )
            clean_msg = msg_s if msg_s in SAFE_VALIDATION_MESSAGES else "invalid_value"
            item = f"{clean_path}: {clean_msg}"
        elif trimmed in KNOWN_LEGACY_SAFE_DETAILS or trimmed in SAFE_BARE_STATIC_TOKENS:
            item = trimmed
        elif allowed_refs is not None and any(
            trimmed in allowed_refs.get(ref_key, ())
            for ref_key in ("item_ids", "requirement_ids", "question_ids")
        ):
            item = trimmed
        elif allowed_refs is None and SAFE_BARE_ID_RE.match(trimmed):
            item = trimmed
        else:
            item = UNRECOGNIZED_DETAIL_TOKEN

        if item and item not in safe:
            safe.append(item)
    return safe
