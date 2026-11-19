"""文本规范化、材料内容校验与文件提取。

规则来源：开发文档/M1/01 第 4 节、02 第 3–4 节。
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass

from app.core.errors import AppError

MAX_MATERIAL_CHARACTERS = 20000

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


def normalize_text(raw: str) -> str:
    """统一换行为 LF；不裁剪正文字符，不改变顺序。"""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def check_material_text(raw: str, *, max_characters: int = MAX_MATERIAL_CHARACTERS) -> str:
    text = normalize_text(raw)
    if not text.strip():
        raise AppError("TEXT_INVALID", "材料文本不能全为空白", details=["text"])
    if len(text) > max_characters:
        raise AppError(
            "TEXT_INVALID",
            f"材料文本超过 {max_characters} 字符上限",
            details=[f"length={len(text)}"],
        )
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iter_profile_items(profile: dict):
    kind = profile.get("kind")
    if kind == "resume":
        for key in ("education", "work_experience", "projects", "skills"):
            for item in profile.get(key) or []:
                yield key, item
    elif kind == "jd":
        for key in ("responsibilities", "requirements"):
            for item in profile.get(key) or []:
                yield key, item
    else:  # pragma: no cover - 由 Pydantic 先行拦截
        raise AppError("PROFILE_INVALID", "缺少合法的 profile.kind")


def sanitize_profile_skill_codes(
    profile: dict | None,
    allowed_skill_codes: set[str],
) -> int:
    """清理材料解析结果中不在启用词表内的技能编码，返回移除的标签数。

    只触及简历 `skills[].code`、`projects[].skill_codes` 和 JD `requirements[].skill_codes`；
    多选项去重并保留首个顺序，保留条目 ID、名称、描述、引用与必需程度。
    """
    if not isinstance(profile, dict):
        return 0
    kind = profile.get("kind")
    removed = 0

    def _filter_codes(raw_codes: object) -> list[str] | None:
        nonlocal removed
        if not isinstance(raw_codes, list):
            return None
        filtered: list[str] = []
        seen: set[str] = set()
        for code in raw_codes:
            if isinstance(code, str) and code in allowed_skill_codes:
                if code not in seen:
                    seen.add(code)
                    filtered.append(code)
            else:
                removed += 1
        return filtered

    if kind == "resume":
        for project in profile.get("projects") or []:
            if isinstance(project, dict) and "skill_codes" in project:
                filtered = _filter_codes(project.get("skill_codes"))
                if filtered is not None:
                    project["skill_codes"] = filtered
        for skill in profile.get("skills") or []:
            if isinstance(skill, dict) and "code" in skill:
                code = skill.get("code")
                if isinstance(code, str) and code not in allowed_skill_codes:
                    skill["code"] = None
                    removed += 1
    elif kind == "jd":
        for req in profile.get("requirements") or []:
            if isinstance(req, dict) and "skill_codes" in req:
                filtered = _filter_codes(req.get("skill_codes"))
                if filtered is not None:
                    req["skill_codes"] = filtered
    return removed


def sanitize_question_skill_codes(
    question: dict | None,
    allowed_skill_codes: set[str],
) -> int:
    """清理生成题中不在启用词表内的可选技能编码，返回移除的标签数。"""
    if not isinstance(question, dict):
        return 0
    raw_codes = question.get("skill_codes")
    if not isinstance(raw_codes, list):
        return 0
    filtered: list[str] = []
    seen: set[str] = set()
    removed = 0
    for code in raw_codes:
        if isinstance(code, str) and code in allowed_skill_codes:
            if code not in seen:
                seen.add(code)
                filtered.append(code)
        else:
            removed += 1
    question["skill_codes"] = filtered
    return removed


def _detail_with_optional_item_id(
    base: list[str], *, item_id: str, allow_user_confirmed: bool
) -> list[str]:
    if allow_user_confirmed:
        return [*base, f"item_id={item_id}"]
    return base


_DATE_RANGE_RE = re.compile(r"(?:19|20)\d{2}\s*[./年-]\s*\d{1,2}")
_EXPERIENCE_ENTRY_PREFIX_RE = re.compile(
    r"^(?:工作|实习|任职)?经历\s*[一二三四五六七八九十0-9]+\s*[:：]"
)
_LIST_MARKER_PREFIX_RE = re.compile(
    r"^(?:[-*•·▪●]\s+|(?:\d{1,2}[.、)）]|（\d{1,2}）|\(\d{1,2}\))\s*)"
)
_NUMBERED_ITEM_RE = re.compile(
    r"^(?:(\d{1,2})[.、)）]|（(\d{1,2})）|\((\d{1,2})\))\s*"
)
_ORG_LABEL_RE = re.compile(
    r"^(?:(?:工作|实习|任职|所在)?(?:公司|单位|组织|雇主|企业|机构)(?:名称)?|"
    r"(?:company|organization|org|employer)(?:\s+name)?)\s*[:：]",
    re.IGNORECASE,
)
_TITLE_LABEL_RE = re.compile(
    r"^(?:(?:实习|担任|现任)?(?:职位|岗位|职务|角色)(?:名称)?|"
    r"(?:job\s+)?title|position|role)\s*[:：]",
    re.IGNORECASE,
)
_RESP_LABEL_RE = re.compile(
    r"^(?:工作内容|工作描述|工作职责|主要职责|职责|内容|描述|时间|期间|"
    r"responsibilities|responsibility|description|duties|duration|period)\s*[:：]",
    re.IGNORECASE,
)
_OTHER_WORK_FIELD_LABEL_RE = re.compile(
    r"^(?:工作内容|工作描述|工作职责|主要职责|主要工作|主要业绩|工作业绩|业绩|成果|"
    r"职责|内容|描述|工作时间|起止时间|任职时间|实习时间|时间|期间|"
    r"部门|团队|工作地点|地点|城市|合作方|对接方|客户|汇报对象|技术栈|"
    r"项目(?:名称|职责|描述|内容)?|"
    r"responsibilities|responsibility|description|duties|duration|period|"
    r"department|team|location|city|partner|client|project|"
    r"achievements?|summary|overview|tech\s+stack|stack|dates?|time|timeline)\s*[:：]",
    re.IGNORECASE,
)
_INLINE_WORK_FIELD_SPLIT_RE = re.compile(
    r"\s+(?=(?:"
    r"(?:工作|实习|任职|所在)?(?:公司|单位|组织|雇主|企业|机构)(?:名称)?|"
    r"(?:实习|担任|现任)?(?:职位|岗位|职务|角色)(?:名称)?|"
    r"工作内容|工作描述|工作职责|主要职责|主要工作|主要业绩|工作业绩|业绩|成果|"
    r"职责|内容|描述|工作时间|起止时间|任职时间|实习时间|时间|期间|"
    r"部门|团队|工作地点|地点|城市|合作方|对接方|客户|汇报对象|技术栈|"
    r"项目(?:名称|职责|描述|内容)?|"
    r"(?:company|organization|org|employer)(?:\s+name)?|"
    r"(?:job\s+)?title|position|role|"
    r"responsibilities|responsibility|description|duties|duration|period|"
    r"department|team|location|city|partner|client|project|"
    r"achievements?|summary|overview|tech\s+stack|stack|dates?|time|timeline"
    r")\s*[:：])",
    re.IGNORECASE,
)
_SECTION_HEADER_RE = re.compile(
    r"^(?:(?:教育|工作|实习|任职|项目|个人)?(?:背景|经历|经验|技能|评价)|"
    r"(?:work\s+)?experience|education|projects|skills)\s*[:：]?$",
    re.IGNORECASE,
)
_FIELD_LABEL_PREFIX_RE = re.compile(
    r"^(?:(?:工作|实习|任职|所在)?(?:公司|单位|组织|雇主|企业|机构)(?:名称)?|"
    r"部门|(?:实习|担任|现任)?(?:岗位|职位|职务|角色)(?:名称)?|时间|期间|"
    r"工作内容|工作描述|工作职责|主要职责|职责|内容|描述|"
    r"(?:job\s+)?title|position|org|organization|company|employer|role|"
    r"responsibilities|responsibility|description|duties|duration|period)\s*[:：]",
    re.IGNORECASE,
)
_DASH_DELIM_RE = re.compile(r"\s+[-–—]\s+")
# 显式字段值后的无标签行只有明确的排版续行证据才允许并入字段值，
# 避免把职责叙述、项目说明等当成单位或职务的续行。
_SENTENCE_TERMINATOR_RE = re.compile(r"[。！？!?；;]\s*$")
# 合法折行只接受“完整的后缀片段”，避免把以职务词开头的职责句并入字段值。
_ORG_VALUE_CONTINUATION_ZH_RE = re.compile(
    r"^(?:股份有限公司|有限责任公司|有限公司|分公司|子公司|集团|"
    r"研究所|研究院|实验室|工作室|事务所|银行|学院|大学|医院|中心)"
    r"(?:[（(][^）)]*[）)])?$"
)
_ORG_VALUE_CONTINUATION_EN_RE = re.compile(
    r"^(?:ltd|limited|inc|llc|corp|corporation|group|company)\b[.,]?$",
    re.IGNORECASE,
)
_TITLE_VALUE_CONTINUATION_ZH_RE = re.compile(
    r"^(?:(?:后端|前端|全栈|服务端|客户端|移动端|数据|测试|算法|产品|运营|"
    r"开发|质量|安全|市场|销售|财务|人力|行政|视觉|交互|UI|UX|"
    r"Java|Python|Go|C\+\+|高级|资深|初级|中级|见习|实习)?"
    r"(?:实习生|工程师|助理|专员|经理|主管|总监|顾问|设计师|架构师|"
    r"分析师|研究员|技术员|负责人|组长|主任|教授|讲师|学生|志愿者))+"
    r"(?:[（(][^）)]*[）)])?$"
)
_TITLE_VALUE_CONTINUATION_EN_RE = re.compile(
    r"^(?:(?:senior|junior|lead|principal|staff|backend|frontend|full[- ]?stack|"
    r"data|software|qa|test|product|ui|ux|java|python)\s+){0,2}"
    r"(?:intern|engineer|assistant|specialist|manager|director|analyst|"
    r"developer|designer|consultant)\.?$",
    re.IGNORECASE,
)
# 独占标签后的第一个值允许“后端开发”这类还没有“实习生”后缀的完整片段。
_TITLE_FIRST_VALUE_ZH_RE = re.compile(
    r"^(?:(?:后端|前端|全栈|服务端|客户端|移动端|数据|测试|算法|产品|运营|"
    r"开发|质量|安全|市场|销售|财务|人力|行政|视觉|交互|UI|UX|"
    r"Java|Python|Go|C\+\+|高级|资深|初级|中级|见习|实习)?"
    r"(?:实习生|工程师|助理|专员|经理|主管|总监|顾问|设计师|架构师|"
    r"分析师|研究员|技术员|负责人|组长|主任|教授|讲师|学生|志愿者|"
    r"开发|测试|运营|产品|设计))+"
    r"(?:[（(][^）)]*[）)])?$"
)
# 分栏识别只把明确的动作型职责开头当作叙述行，避免误伤“在线科技”等单位名。
_RESPONSIBILITY_ACTION_RE = re.compile(
    r"(?:负责|参与|协助|维护|编写|设计|实现|支持|对接|推动|优化|处理|管理|"
    r"跟进|配合|完成|确保|搭建|主导|组织|整理|分析|开发|测试)"
)
# 分栏版式的栏位识别只看列语义与上下文，不依赖具体单位／职务值。
_ORG_COLUMN_SUFFIX_RE = re.compile(
    r"(?:股份有限公司|有限责任公司|有限公司|分公司|子公司|公司|集团|银行|"
    r"学院|大学|学校|医院|实验室|研究所|研究院|中心|工作室|事务所|企业|"
    r"机构|委员会|工厂|厂|店)$"
)
_TITLE_COLUMN_SUFFIX_RE = re.compile(
    r"(?:实习生|工程师|助理|专员|经理|主管|总监|顾问|设计师|架构师|"
    r"分析师|研究员|技术员|负责人|组长|主任|教授|讲师|学生|志愿者|"
    r"开发|测试|运营|产品|设计)$"
)
# 有明确表头时按列名确定字段位置；列名只接受无歧义的常见写法。
_WORK_TABLE_HEADER_ALIASES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "org",
        frozenset(
            {
                "公司",
                "公司名称",
                "单位",
                "单位名称",
                "工作单位",
                "实习单位",
                "所在公司",
                "所在单位",
                "任职公司",
                "任职单位",
                "组织",
                "组织名称",
                "企业",
                "企业名称",
                "雇主",
                "雇主名称",
                "机构",
                "机构名称",
                "company",
                "companyname",
                "organization",
                "organizationname",
                "org",
                "orgname",
                "employer",
                "employername",
                "workplace",
                "firm",
            }
        ),
    ),
    (
        "title",
        frozenset(
            {
                "职位",
                "职位名称",
                "岗位",
                "岗位名称",
                "职务",
                "职务名称",
                "角色",
                "角色名称",
                "实习职位",
                "担任职位",
                "jobtitle",
                "title",
                "position",
                "positionname",
                "role",
                "rolename",
                "job",
            }
        ),
    ),
    (
        "resp",
        frozenset(
            {
                "职责",
                "工作职责",
                "主要职责",
                "职责描述",
                "工作内容",
                "工作描述",
                "主要工作",
                "内容",
                "描述",
                "业绩",
                "成果",
                "responsibilities",
                "responsibility",
                "description",
                "duties",
                "duty",
                "achievements",
                "summary",
                "overview",
            }
        ),
    ),
    (
        "date",
        frozenset(
            {
                "时间",
                "起止时间",
                "任职时间",
                "实习时间",
                "工作时间",
                "期间",
                "日期",
                "duration",
                "period",
                "dates",
                "date",
                "time",
                "timeline",
            }
        ),
    ),
    (
        "other",
        frozenset(
            {
                "部门",
                "团队",
                "工作地点",
                "地点",
                "城市",
                "合作方",
                "对接方",
                "客户",
                "汇报对象",
                "技术栈",
                "项目",
                "项目名称",
                "备注",
                "department",
                "team",
                "location",
                "city",
                "partner",
                "client",
                "project",
                "projectname",
                "techstack",
                "stack",
                "note",
                "notes",
                "remark",
                "remarks",
            }
        ),
    ),
)


_MARKDOWN_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")


def _unescaped_pipe_positions(value: str) -> list[int]:
    positions: list[int] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            index += 2
            continue
        if char in "|｜":
            positions.append(index)
        index += 1
    return positions


def _split_unescaped_pipes(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            index += 2
            continue
        if char in "|｜":
            parts.append(value[start:index])
            start = index + 1
        index += 1
    parts.append(value[start:])
    return parts


def _unescape_table_cell(value: str) -> str:
    r"""还原表格单元格转义：`\\`、`\|`／`\｜` 和换行标记 `\n`。"""
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\" or index + 1 >= len(value):
            result.append(char)
            index += 1
            continue
        escaped = value[index + 1]
        if escaped == "n":
            result.append("\n")
        elif escaped in "\\|｜":
            result.append(escaped)
        else:
            result.append(char)
            result.append(escaped)
        index += 2
    return "".join(result)


@dataclass(frozen=True)
class _WorkTableHeader:
    columns: list[str]
    fenced: bool
    has_separator: bool
    header_line: str
    separator_line: str | None = None


@dataclass(frozen=True)
class _MatchedWorkSegment:
    text: str
    table_header: _WorkTableHeader | None = None


@dataclass(frozen=True)
class _HeaderBoundWorkFields:
    has_table_context: bool = False
    unalignable: bool = False
    has_org: bool = False
    org_val: str | None = None
    org_conflict: bool = False
    has_title: bool = False
    title_val: str | None = None
    title_conflict: bool = False


def _table_line_is_fenced(line: str) -> bool:
    """判断表头是否使用首尾包围式竖线，作为整张表的外框规则。"""
    text = line.strip()
    positions = _unescaped_pipe_positions(text)
    return bool(
        len(positions) >= 2
        and positions[0] == 0
        and positions[-1] == len(text) - 1
    )


def _split_table_line(
    line: str, *, fenced: bool | None = None
) -> list[str] | None:
    """按未转义竖线拆分一行表格文本，表头与数据行共用这一结果。

    `fenced=True` 表示所属表头使用首尾包围式语法，行两端的竖线是外框，允许
    数据行省略任意一侧；`fenced=False` 表示裸竖线表，首尾竖线之间的空串是
    真实边缘单元格。未提供外框规则时保留旧行为：只有首尾都是未转义竖线时
    才移除一对外侧竖线。内部连续竖线始终保留为真正的空列，单元格内的转义
    竖线不拆列。
    """
    text = line.strip()
    positions = _unescaped_pipe_positions(text)
    if not positions:
        return None
    if fenced is True:
        start = 1 if positions[0] == 0 else 0
        end = len(text) - 1 if positions[-1] == len(text) - 1 else len(text)
        text = text[start:end]
    elif fenced is None:
        if (
            len(positions) >= 2
            and positions[0] == 0
            and positions[-1] == len(text) - 1
        ):
            text = text[1:-1]
    return [
        _unescape_table_cell(cell.strip())
        for cell in _split_unescaped_pipes(text)
    ]


def _markdown_separator_cell_count(line: str) -> int | None:
    """判断是否为有效 Markdown 表格分隔行，返回分隔单元格列数。

    使用 `fenced=True` 拆列以兼容双侧外框、无外框以及单侧外框分隔行；
    单格且未包围竖线（如 `| ---`）不作为分隔行。
    """
    cells = _split_table_line(line, fenced=True)
    if not cells or not all(
        _MARKDOWN_TABLE_SEPARATOR_CELL_RE.match(cell) for cell in cells
    ):
        return None
    if len(cells) == 1 and not _table_line_is_fenced(line):
        return None
    return len(cells)


def _is_markdown_table_separator(line: str) -> bool:
    return _markdown_separator_cell_count(line) is not None


def _split_pipe_or_dash_units(
    value: str, *, fenced: bool | None = None
) -> list[str]:
    table_cells = _split_table_line(value, fenced=fenced)
    raw_parts = table_cells if table_cells is not None else [value]
    parts: list[str] = []
    for raw in raw_parts:
        if _DASH_DELIM_RE.search(raw):
            parts.extend(p.strip() for p in _DASH_DELIM_RE.split(raw) if p.strip())
            continue
        stripped = raw.strip()
        if stripped:
            parts.append(stripped)
    return parts


def _work_table_column_kind(cell: str) -> str | None:
    key = normalized_text(cell)
    if not key:
        return None
    for kind, aliases in _WORK_TABLE_HEADER_ALIASES:
        if key in aliases:
            return kind
    return None


def _work_table_header_columns(
    line: str, *, fenced: bool | None = None
) -> list[str] | None:
    """识别“公司｜职位｜职责”这类明确表头，返回每列的字段语义。"""
    if _is_markdown_table_separator(line):
        return None
    cells = _split_table_line(line, fenced=fenced)
    if cells is None or len(cells) < 2:
        return None
    kinds: list[str] = []
    for cell in cells:
        # 额外列（项目、备注、空列等）不参与绑定；只要单位和职务列各有且仅有一列即可。
        kinds.append(_work_table_column_kind(cell) or "other")
    # 单位与职务必须各有一列，重复列不猜顺序。
    if kinds.count("org") != 1 or kinds.count("title") != 1:
        return None
    return kinds


def _detect_work_table_header(
    header_line: str, next_line: str | None = None
) -> _WorkTableHeader | None:
    """联合表头与紧邻分隔行识别工作经历表头及其外框版式规则。"""
    header_stripped = header_line.strip()
    if not header_stripped or _is_markdown_table_separator(header_stripped):
        return None

    next_stripped = next_line.strip() if next_line is not None else ""
    sep_count = _markdown_separator_cell_count(next_stripped) if next_stripped else None
    if sep_count is not None:
        md_columns = _work_table_header_columns(header_stripped, fenced=True)
        if md_columns is not None and len(md_columns) == sep_count:
            return _WorkTableHeader(
                columns=md_columns,
                fenced=True,
                has_separator=True,
                header_line=header_stripped,
                separator_line=next_stripped,
            )

    is_fenced = _table_line_is_fenced(header_stripped)
    columns = _work_table_header_columns(header_stripped, fenced=is_fenced)
    if columns is None:
        return None
    return _WorkTableHeader(
        columns=columns,
        fenced=is_fenced,
        has_separator=False,
        header_line=header_stripped,
        separator_line=None,
    )


def _split_work_table_cells(
    line: str, *, fenced: bool | None = None
) -> list[str] | None:
    return _split_table_line(line, fenced=fenced)


def _align_work_table_row(
    line: str, header: _WorkTableHeader
) -> tuple[str, list[str] | None]:
    """按所属表头版式与列数对齐数据行，返回 (`not_table_row`|`aligned`|`unalignable`, cells)。"""
    cells = _split_work_table_cells(line, fenced=header.fenced)
    if cells is None:
        return "not_table_row", None
    if len(cells) < len(header.columns):
        return "aligned", [*cells, *([""] * (len(header.columns) - len(cells)))]
    if len(cells) == len(header.columns):
        return "aligned", cells
    if header.columns[-1] == "resp":
        return "aligned", cells[: len(header.columns)]
    return "unalignable", None


def _work_table_row_values(
    line: str, header_columns: list[str], *, fenced: bool | None = None
) -> list[str] | None:
    """按表头位置取单元格；保留空单元格，只把表头末尾职责列内的竖线并回职责。"""
    status, cells = _align_work_table_row(
        line,
        _WorkTableHeader(
            columns=header_columns,
            fenced=bool(fenced),
            has_separator=False,
            header_line="",
        ),
    )
    if status != "aligned":
        return None
    return cells


def _key_value_row(line: str) -> tuple[str, str] | None:
    """识别两列键值行，返回字段语义与右列完整值。

    只有左列是已知字段名时才成立；横向表头与 Markdown 分隔行不参与。
    单边外框的行在按默认规则拆分后会多出一个语法空单元格，这里只在该空
    单元格确实位于边界时去掉，保留右列中的转义竖线与换行。
    """
    stripped = line.strip()
    if not stripped or _is_markdown_table_separator(stripped):
        return None
    if _work_table_header_columns(stripped) is not None:
        return None
    cells = _split_table_line(stripped)
    if cells is None:
        return None
    if (
        len(cells) == 3
        and cells[0] == ""
        and _work_table_column_kind(cells[1]) is not None
    ):
        cells = cells[1:]
    elif (
        len(cells) == 3
        and cells[-1] == ""
        and _work_table_column_kind(cells[0]) is not None
    ):
        cells = cells[:-1]
    if len(cells) != 2:
        return None
    kind = _work_table_column_kind(cells[0])
    if kind is None:
        return None
    # 左右都是字段名时更像两列表头，不把它当成键值对，避免把表头值写进经历。
    if _work_table_column_kind(cells[1]) is not None:
        return None
    return kind, cells[1]


@dataclass
class _KeyValueWorkGroup:
    start_line: int
    end_line: int
    org_entries: list[list[str]]
    title_entries: list[list[str]]
    has_resp: bool = False

    def add(self, kind: str, value: str) -> None:
        if kind == "org":
            self.org_entries.append([value] if value else [])
        elif kind == "title":
            self.title_entries.append([value] if value else [])
        elif kind == "resp":
            self.has_resp = True


def _key_value_work_groups(text: str) -> list[_KeyValueWorkGroup]:
    """按连续字段标签聚合两列键值经历，重复单位或职位标签时另起一段。"""
    groups: list[_KeyValueWorkGroup] = []
    current: _KeyValueWorkGroup | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None and (current.org_entries or current.title_entries):
            groups.append(current)
        current = None

    for line_idx, raw_line in enumerate(text.splitlines()):
        row = _key_value_row(raw_line) if raw_line.strip() else None
        if row is None:
            _flush()
            continue
        kind, value = row
        repeated = (
            kind == "org" and current is not None and bool(current.org_entries)
        ) or (
            kind == "title" and current is not None and bool(current.title_entries)
        )
        if repeated:
            _flush()
        if current is None:
            current = _KeyValueWorkGroup(line_idx, line_idx, [], [])
        current.end_line = line_idx
        current.add(kind, value)
    _flush()
    return groups


def _table_header_for_span(
    text: str, span_start: int
) -> tuple[_WorkTableHeader, int] | None:
    """返回包含 span_start 的数据行所属表格的最近表头结构与起始字符偏移。

    只在同一张连续表格内向上继承：空行、章节标题、显式字段标签、列表前缀或
    非分栏行都会截断；职责列内的竖线不会把后续文本变成新的列。
    """
    lines = text.splitlines()
    line_starts: list[int] = []
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line_starts.append(offset)
        offset += len(raw_line)
    if not lines or not line_starts:
        return None
    line_idx = 0
    for idx, start in enumerate(line_starts):
        if start <= span_start:
            line_idx = idx
        else:
            break
    first_line = lines[line_idx].strip()
    first_next = lines[line_idx + 1].strip() if line_idx + 1 < len(lines) else None
    if (
        not first_line
        or _is_markdown_table_separator(first_line)
        or _detect_work_table_header(first_line, first_next) is not None
        or _key_value_row(first_line) is not None
        or _LIST_MARKER_PREFIX_RE.match(first_line)
        or _EXPERIENCE_ENTRY_PREFIX_RE.match(first_line)
        or _OTHER_WORK_FIELD_LABEL_RE.match(first_line)
        or _FIELD_LABEL_PREFIX_RE.match(first_line)
        or _split_work_table_cells(first_line) is None
    ):
        return None
    for idx in range(line_idx - 1, -1, -1):
        candidate = lines[idx].strip()
        if not candidate:
            break
        if _is_markdown_table_separator(candidate):
            continue
        if _key_value_row(candidate) is not None:
            break
        if (
            _SECTION_HEADER_RE.match(candidate)
            or _EXPERIENCE_ENTRY_PREFIX_RE.match(candidate)
            or _LIST_MARKER_PREFIX_RE.match(candidate)
            or _OTHER_WORK_FIELD_LABEL_RE.match(candidate)
            or _FIELD_LABEL_PREFIX_RE.match(candidate)
        ):
            break
        next_candidate = lines[idx + 1].strip() if idx + 1 < len(lines) else None
        detected = _detect_work_table_header(candidate, next_candidate)
        if detected is not None:
            header_content_start = line_starts[idx] + (
                len(lines[idx]) - len(lines[idx].lstrip())
            )
            return detected, header_content_start
        if _split_work_table_cells(candidate) is None:
            break
    return None


def _has_unclosed_bracket(value: str) -> bool:
    return (
        value.count("(") > value.count(")")
        or value.count("（") > value.count("）")
    )


def _is_field_value_continuation(
    unit: str, active_field: str, current_lines: list[str]
) -> bool:
    """判断无标签行是否具备明确排版续行证据，可作为显式字段值的折行。"""
    candidate = unit.strip()
    if not candidate or _SENTENCE_TERMINATOR_RE.search(candidate):
        return False
    if current_lines and _has_unclosed_bracket("\n".join(current_lines)):
        return True
    if active_field == "org":
        return bool(
            _ORG_VALUE_CONTINUATION_ZH_RE.match(candidate)
            or _ORG_VALUE_CONTINUATION_EN_RE.match(candidate)
        )
    if active_field == "title":
        return bool(
            _TITLE_VALUE_CONTINUATION_ZH_RE.match(candidate)
            or _TITLE_VALUE_CONTINUATION_EN_RE.match(candidate)
        )
    return False


def _is_first_field_value(unit: str, active_field: str) -> bool:
    """判断独占标签后的紧邻文本是否可作为该字段的第一个值。

    只接受同段、非职责句、非日期／列表／分栏行的文本；无法确定时留空并转人工核对。
    """
    candidate = unit.strip()
    if not candidate or _SENTENCE_TERMINATOR_RE.search(candidate):
        return False
    if _is_date_only_segment(candidate) or _DATE_RANGE_RE.match(candidate):
        return False
    if len(_structured_entry_parts(candidate)) >= 2:
        return False
    if active_field == "title" and (
        _TITLE_FIRST_VALUE_ZH_RE.match(candidate)
        or _TITLE_VALUE_CONTINUATION_EN_RE.match(candidate)
    ):
        return True
    if active_field == "org" and _ORG_COLUMN_SUFFIX_RE.search(candidate):
        return True
    # 真正的单位／职务片段不含职责动作；出现动作词时按职责句处理，留给人工核对。
    if _RESPONSIBILITY_ACTION_RE.search(candidate):
        return False
    return True


def _is_date_only_segment(segment: str) -> bool:
    cleaned = re.sub(r"[\s\-–—~至到./年月日()（）]", "", segment)
    return bool(cleaned) and cleaned.isdigit()


def _structured_entry_parts(line: str, *, fenced: bool | None = None) -> list[str]:
    parts = _split_pipe_or_dash_units(line, fenced=fenced)
    if len(parts) < 2:
        return []
    return [part for part in parts if not _is_date_only_segment(part)]


def _is_structured_entry_line(line: str, *, fenced: bool | None = None) -> bool:
    """分栏行必须具备至少两个栏位，且不是以职责动作开头的叙述行。"""
    parts = _structured_entry_parts(line, fenced=fenced)
    if len(parts) < 2:
        return False
    if _RESPONSIBILITY_ACTION_RE.match(parts[0]):
        return False
    if _SENTENCE_TERMINATOR_RE.search(parts[0]):
        return False
    if _SENTENCE_TERMINATOR_RE.search(parts[1]):
        return False
    return True


def _find_norm_line_span(lines: list[str], norm_target: str) -> tuple[int, int] | None:
    if not norm_target:
        return None
    for idx, line in enumerate(lines):
        if norm_target in _normalized_table_aware_text(line):
            return idx, idx
    for idx in range(len(lines) - 1):
        if norm_target in _normalized_table_aware_text(lines[idx] + lines[idx + 1]):
            return idx, idx + 1
    return None


def _extract_list_number(line: str) -> int | None:
    match = _NUMBERED_ITEM_RE.match(line)
    if not match:
        return None
    raw_num = next((g for g in match.groups() if g is not None), None)
    return int(raw_num) if raw_num is not None else None


def _work_text_segments(text: str) -> list[tuple[int, int]]:
    """识别原文中的独立经历段落边界，返回各段落的字符起止范围。"""
    segments: list[tuple[int, int]] = []
    seg_start: int | None = None
    last_end: int | None = None
    has_org_label = False
    has_title_label = False
    has_structured_header = False
    has_exp_entry_prefix = False
    in_detail_body = False
    started_with_number = False
    started_bare = False
    last_number: int | None = None
    active_group_idx: int | None = None
    active_table: _WorkTableHeader | None = None
    active_table_sep_idx: int | None = None

    groups = _key_value_work_groups(text)
    group_by_line: dict[int, int] = {}
    for group_idx, group in enumerate(groups):
        for line_idx in range(group.start_line, group.end_line + 1):
            group_by_line[line_idx] = group_idx

    def _reset_flags() -> None:
        nonlocal has_org_label, has_title_label, has_structured_header
        nonlocal has_exp_entry_prefix, in_detail_body, started_with_number
        nonlocal started_bare, last_number
        has_org_label = False
        has_title_label = False
        has_structured_header = False
        has_exp_entry_prefix = False
        in_detail_body = False
        started_with_number = False
        started_bare = False
        last_number = None

    def _close_segment() -> None:
        nonlocal seg_start, last_end
        if seg_start is not None and last_end is not None and last_end > seg_start:
            segments.append((seg_start, last_end))
        seg_start = None
        last_end = None
        _reset_flags()

    raw_lines = text.splitlines(keepends=True)
    offset = 0
    for line_idx, raw_line in enumerate(raw_lines):
        line_start = offset
        offset += len(raw_line)
        line_no_nl = raw_line.rstrip("\r\n")
        stripped = line_no_nl.strip()
        if not stripped:
            active_table = None
            active_table_sep_idx = None
            continue
        content_start = line_start + (len(line_no_nl) - len(line_no_nl.lstrip()))
        content_end = line_start + len(line_no_nl.rstrip())

        group_idx = group_by_line.get(line_idx)
        if group_idx is not None:
            active_table = None
            active_table_sep_idx = None
            group = groups[group_idx]
            if active_group_idx != group_idx:
                _close_segment()
                active_group_idx = group_idx
                seg_start = content_start
                # 键值行本身已经给出字段边界，后续同级标签或分栏行应另起经历。
                has_org_label = bool(group.org_entries)
                has_title_label = bool(group.title_entries)
                has_structured_header = True
                in_detail_body = group.has_resp
                started_bare = False
            last_end = content_end
            continue
        active_group_idx = None

        if _SECTION_HEADER_RE.match(stripped):
            _close_segment()
            active_table = None
            active_table_sep_idx = None
            continue

        if active_table_sep_idx is not None and line_idx == active_table_sep_idx:
            active_table_sep_idx = None
            continue

        list_num = _extract_list_number(stripped)
        has_list_prefix = bool(_LIST_MARKER_PREFIX_RE.match(stripped))
        body = (
            _LIST_MARKER_PREFIX_RE.sub("", stripped, count=1).strip()
            if has_list_prefix
            else stripped
        )

        is_org = bool(_ORG_LABEL_RE.match(body))
        is_title = bool(_TITLE_LABEL_RE.match(body))
        is_resp = bool(_RESP_LABEL_RE.match(body))
        is_exp_prefix = bool(_EXPERIENCE_ENTRY_PREFIX_RE.match(stripped))
        is_other_label = bool(
            _OTHER_WORK_FIELD_LABEL_RE.match(body)
            or _FIELD_LABEL_PREFIX_RE.match(body)
        )

        if (
            not has_list_prefix
            and not is_exp_prefix
            and not is_other_label
            and not is_org
            and not is_title
            and not is_resp
        ):
            next_stripped = (
                raw_lines[line_idx + 1].rstrip("\r\n").strip()
                if line_idx + 1 < len(raw_lines)
                else None
            )
            detected_table = _detect_work_table_header(stripped, next_stripped)
            if detected_table is not None:
                _close_segment()
                active_table = detected_table
                active_table_sep_idx = (
                    line_idx + 1 if detected_table.has_separator else None
                )
                continue

        if _is_markdown_table_separator(stripped):
            continue

        is_active_table_row = False
        if active_table is not None:
            if (
                has_list_prefix
                or is_exp_prefix
                or is_other_label
                or is_org
                or is_title
                or is_resp
                or _split_table_line(stripped, fenced=active_table.fenced) is None
            ):
                active_table = None
                active_table_sep_idx = None
            else:
                is_active_table_row = True

        table_fenced = active_table.fenced if active_table is not None else None
        is_structured = is_active_table_row or (
            not _FIELD_LABEL_PREFIX_RE.match(body)
            and _is_structured_entry_line(body, fenced=table_fenced)
        )

        start_new = False
        if seg_start is not None:
            if is_exp_prefix:
                start_new = True
            elif is_org:
                if has_org_label or (in_detail_body and (has_title_label or has_structured_header)):
                    start_new = True
            elif is_title:
                if has_title_label:
                    start_new = True
            elif is_structured:
                if (
                    has_structured_header
                    or has_org_label
                    or has_title_label
                    or (not in_detail_body and started_bare)
                ) and not (in_detail_body and has_list_prefix):
                    start_new = True
            elif not is_resp and list_num is not None and started_with_number:
                if not in_detail_body and last_number is not None and list_num == last_number + 1:
                    start_new = True

        if start_new:
            _close_segment()

        if seg_start is None:
            seg_start = content_start
            started_with_number = list_num is not None and not is_resp
            started_bare = (
                list_num is None
                and not is_org
                and not is_title
                and not is_resp
                and not is_structured
                and not is_exp_prefix
            )

        last_end = content_end
        if is_exp_prefix:
            has_exp_entry_prefix = True
            in_detail_body = False
        if is_org:
            has_org_label = True
            in_detail_body = False
        if is_title:
            has_title_label = True
            in_detail_body = False
        if is_resp:
            in_detail_body = True
        if is_structured and not (in_detail_body and has_list_prefix):
            has_structured_header = True
            in_detail_body = False
        if list_num is not None and started_with_number and not in_detail_body:
            last_number = list_num

    _close_segment()
    return segments


def _matching_work_segments(text: str, quote: str) -> list[_MatchedWorkSegment]:
    """返回原文中完整包含 quote 的单个经历段落及所属表头上下文列表。"""
    segments = _work_text_segments(text)
    if not segments:
        return [_MatchedWorkSegment(text=text, table_header=None)]
    if len(segments) == 1:
        seg_start, seg_end = segments[0]
        header_info = _table_header_for_span(text, seg_start)
        return [
            _MatchedWorkSegment(
                text=text[seg_start:seg_end],
                table_header=header_info[0] if header_info is not None else None,
            )
        ]
    matched: list[_MatchedWorkSegment] = []
    seen_spans: set[tuple[int, int]] = set()
    start = 0
    while True:
        idx = text.find(quote, start)
        if idx < 0:
            break
        end = idx + len(quote)
        trimmed_start = idx + (len(quote) - len(quote.lstrip()))
        trimmed_end = end - (len(quote) - len(quote.rstrip()))
        for seg_idx, (seg_start, seg_end) in enumerate(segments):
            header_info = _table_header_for_span(text, seg_start)
            matches_segment = (
                seg_start <= trimmed_start and trimmed_end <= seg_end
            )
            if (
                not matches_segment
                and header_info is not None
                and (seg_idx == 0 or segments[seg_idx - 1][1] <= header_info[1])
            ):
                header_start = header_info[1]
                matches_segment = (
                    header_start <= trimmed_start < seg_start
                    and seg_start < trimmed_end <= seg_end
                )
            if matches_segment:
                span = (seg_start, seg_end)
                if span not in seen_spans:
                    seen_spans.add(span)
                    matched.append(
                        _MatchedWorkSegment(
                            text=text[seg_start:seg_end],
                            table_header=(
                                header_info[0] if header_info is not None else None
                            ),
                        )
                    )
        start = idx + 1
    return matched


def _normalized_table_aware_text(value: str) -> str:
    """校验前还原表格行内的转义，非表格行保持原样。"""
    lines = [
        _unescape_table_cell(line) if _split_table_line(line) is not None else line
        for line in value.splitlines()
    ]
    return normalized_text("\n".join(lines))


def _quote_fits_single_text_segment(text: str, quote: str) -> bool:
    segments = _work_text_segments(text)
    if len(segments) <= 1:
        return True
    return bool(_matching_work_segments(text, quote))


def _split_work_line_units(body: str, *, active_field: str | None) -> list[str]:
    parts = _split_pipe_or_dash_units(body)
    if len(parts) >= 2 and any(
        _ORG_LABEL_RE.match(p)
        or _TITLE_LABEL_RE.match(p)
        or _OTHER_WORK_FIELD_LABEL_RE.match(p)
        or _RESP_LABEL_RE.match(p)
        or _FIELD_LABEL_PREFIX_RE.match(p)
        or _is_date_only_segment(p)
        for p in parts
    ):
        units = parts
    else:
        units = [body]
    expanded: list[str] = []
    for unit in units:
        if (
            active_field in ("org", "title")
            or _ORG_LABEL_RE.match(unit)
            or _TITLE_LABEL_RE.match(unit)
        ) and _INLINE_WORK_FIELD_SPLIT_RE.search(unit):
            expanded.extend(
                p.strip() for p in _INLINE_WORK_FIELD_SPLIT_RE.split(unit) if p.strip()
            )
        else:
            expanded.append(unit)
    return expanded


def _resolve_labeled_field_values(entries: list[list[str]]) -> tuple[bool, str | None, bool]:
    if not entries:
        return False, None, False
    joined = ["\n".join(lines).strip() for lines in entries]
    non_empty = [val for val in joined if val]
    if not non_empty:
        return True, "", False
    norm_distinct = {normalized_text(val) for val in non_empty if normalized_text(val)}
    if len(entries) > 1 and len(norm_distinct) != 1:
        return True, None, True
    return True, non_empty[0], False


def _extract_explicit_work_fields(
    segment_text: str,
) -> tuple[bool, str | None, bool, bool, str | None, bool]:
    """提取段落内显式标注的单位和职务值域（复用分段与行标签规则）。"""
    org_entries: list[list[str]] = []
    title_entries: list[list[str]] = []
    active_field: str | None = None
    awaiting_first_value = False

    for raw_line in segment_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            # 独占标签跨段落时不接收下一段文字，避免把后续职责当作字段值。
            active_field = "other"
            awaiting_first_value = False
            continue
        if _SECTION_HEADER_RE.match(stripped):
            active_field = None
            awaiting_first_value = False
            continue
        has_list_prefix = bool(_LIST_MARKER_PREFIX_RE.match(stripped))
        body = (
            _LIST_MARKER_PREFIX_RE.sub("", stripped, count=1).strip()
            if has_list_prefix
            else stripped
        )
        if not body:
            continue
        if _EXPERIENCE_ENTRY_PREFIX_RE.match(body):
            active_field = None
            awaiting_first_value = False
            body = _EXPERIENCE_ENTRY_PREFIX_RE.sub("", body, count=1).strip()
            if not body:
                continue

        units = _split_work_line_units(body, active_field=active_field)
        for idx, unit in enumerate(units):
            is_org = bool(_ORG_LABEL_RE.match(unit))
            is_title = bool(_TITLE_LABEL_RE.match(unit))
            is_other_label = bool(
                _OTHER_WORK_FIELD_LABEL_RE.match(unit)
                or _RESP_LABEL_RE.match(unit)
                or _FIELD_LABEL_PREFIX_RE.match(unit)
            )
            if is_org:
                val = _ORG_LABEL_RE.sub("", unit, count=1).strip()
                org_entries.append([val] if val else [])
                active_field = "org"
                awaiting_first_value = not val
            elif is_title:
                val = _TITLE_LABEL_RE.sub("", unit, count=1).strip()
                title_entries.append([val] if val else [])
                active_field = "title"
                awaiting_first_value = not val
            elif is_other_label:
                active_field = "other"
                awaiting_first_value = False
            elif active_field in ("org", "title"):
                current_lines = org_entries[-1] if active_field == "org" else title_entries[-1]
                is_first_unit_with_list = idx == 0 and has_list_prefix
                is_structured = _is_structured_entry_line(unit)
                is_date_line = _is_date_only_segment(unit) or bool(_DATE_RANGE_RE.match(unit))
                if is_first_unit_with_list or is_structured or is_date_line:
                    active_field = "other"
                    awaiting_first_value = False
                elif not current_lines:
                    if awaiting_first_value and _is_first_field_value(unit, active_field):
                        current_lines.append(unit)
                        awaiting_first_value = False
                    else:
                        active_field = "other"
                        awaiting_first_value = False
                elif _is_field_value_continuation(unit, active_field, current_lines):
                    current_lines.append(unit)
                else:
                    active_field = "other"
                    awaiting_first_value = False

    has_org, org_val, org_conflict = _resolve_labeled_field_values(org_entries)
    has_title, title_val, title_conflict = _resolve_labeled_field_values(title_entries)
    return has_org, org_val, org_conflict, has_title, title_val, title_conflict


def _is_org_column(value: str) -> bool:
    candidate = value.strip()
    return bool(
        candidate
        and _ORG_COLUMN_SUFFIX_RE.search(candidate)
        and not _RESPONSIBILITY_ACTION_RE.match(candidate)
        and not _SENTENCE_TERMINATOR_RE.search(candidate)
    )


def _is_title_column(value: str) -> bool:
    candidate = value.strip()
    return bool(
        candidate
        and _TITLE_COLUMN_SUFFIX_RE.search(candidate)
        and not _RESPONSIBILITY_ACTION_RE.match(candidate)
        and not _SENTENCE_TERMINATOR_RE.search(candidate)
    )


def _extract_structured_work_fields(
    segment_text: str,
) -> tuple[bool, str | None, bool, str | None]:
    """识别单位｜职务（｜日期｜描述）分栏版式，并绑定前两列的值域。

    只有栏位语义和上下文都明确时才绑定；职责文本自带竖线、栏位顺序不明或已有
    显式字段标签时保持原样，交给有限纠正和人工核对。
    """
    org_entries: list[list[str]] = []
    title_entries: list[list[str]] = []
    header_seen = False
    context_blocked = False

    for raw_line in segment_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or _SECTION_HEADER_RE.match(stripped):
            continue
        has_list_prefix = bool(_LIST_MARKER_PREFIX_RE.match(stripped))
        body = (
            _LIST_MARKER_PREFIX_RE.sub("", stripped, count=1).strip()
            if has_list_prefix
            else stripped
        )
        if not body:
            continue
        if _EXPERIENCE_ENTRY_PREFIX_RE.match(body):
            context_blocked = False
            header_seen = False
            body = _EXPERIENCE_ENTRY_PREFIX_RE.sub("", body, count=1).strip()
            if not body:
                continue
        if (
            _OTHER_WORK_FIELD_LABEL_RE.match(body)
            or _RESP_LABEL_RE.match(body)
            or _FIELD_LABEL_PREFIX_RE.match(body)
        ):
            context_blocked = True
            header_seen = False
            continue
        if has_list_prefix:
            context_blocked = True
            continue
        if context_blocked or header_seen:
            continue
        parts = _structured_entry_parts(body)
        if len(parts) < 2:
            continue
        if not (_is_org_column(parts[0]) and _is_title_column(parts[1])):
            continue
        org_entries.append([parts[0]])
        title_entries.append([parts[1]])
        header_seen = True

    has_org, org_val, org_conflict = _resolve_labeled_field_values(org_entries)
    has_title, title_val, title_conflict = _resolve_labeled_field_values(title_entries)
    if org_conflict or title_conflict:
        return True, None, True, None
    return has_org, org_val, has_title, title_val


def _extract_header_bound_work_fields(
    segment_text: str,
    *,
    inherited_header: _WorkTableHeader | None = None,
) -> _HeaderBoundWorkFields:
    """识别明确表头，并按列位置绑定后续数据行的单位与职务。

    只继承同一连续表格区域内的表头；列值按完整字段值比较，不按整条引文做子串
    匹配，因此无后缀公司名和英文职务也可直接使用。空单元格保留为缺失值，遇
    到无法可靠对齐的行保留已识别表头事实并标为 `unalignable`，避免退回自由文本。
    """
    org_entries: list[list[str]] = []
    title_entries: list[list[str]] = []
    active_header: _WorkTableHeader | None = inherited_header
    has_table_context = inherited_header is not None
    unalignable = False
    skip_sep_idx: int | None = None

    raw_lines = segment_text.splitlines()
    for line_idx, raw_line in enumerate(raw_lines):
        stripped = raw_line.strip()
        if not stripped or _SECTION_HEADER_RE.match(stripped):
            active_header = None
            skip_sep_idx = None
            continue
        if skip_sep_idx is not None and line_idx == skip_sep_idx:
            skip_sep_idx = None
            continue
        has_list_prefix = bool(_LIST_MARKER_PREFIX_RE.match(stripped))
        body = (
            _LIST_MARKER_PREFIX_RE.sub("", stripped, count=1).strip()
            if has_list_prefix
            else stripped
        )
        if not body:
            continue
        if _EXPERIENCE_ENTRY_PREFIX_RE.match(body):
            active_header = None
            skip_sep_idx = None
            body = _EXPERIENCE_ENTRY_PREFIX_RE.sub("", body, count=1).strip()
            if not body:
                continue
        if (
            has_list_prefix
            or _OTHER_WORK_FIELD_LABEL_RE.match(body)
            or _FIELD_LABEL_PREFIX_RE.match(body)
        ):
            active_header = None
            skip_sep_idx = None
            continue
        if _is_markdown_table_separator(body):
            continue
        next_stripped = (
            raw_lines[line_idx + 1].strip() if line_idx + 1 < len(raw_lines) else None
        )
        detected = _detect_work_table_header(body, next_stripped)
        if detected is not None:
            active_header = detected
            has_table_context = True
            skip_sep_idx = line_idx + 1 if detected.has_separator else None
            continue
        # 两列键值行是自描述字段，不能继续套用上一张横向表的列位置。
        if _key_value_row(body) is not None:
            active_header = None
            skip_sep_idx = None
            continue
        if active_header is None:
            continue
        status, cells = _align_work_table_row(body, active_header)
        if status == "not_table_row":
            active_header = None
            skip_sep_idx = None
            continue
        if status == "unalignable" or cells is None:
            unalignable = True
            continue
        org_entries.append([cells[active_header.columns.index("org")]])
        title_entries.append([cells[active_header.columns.index("title")]])

    has_org, org_val, org_conflict = _resolve_labeled_field_values(org_entries)
    has_title, title_val, title_conflict = _resolve_labeled_field_values(title_entries)
    return _HeaderBoundWorkFields(
        has_table_context=has_table_context,
        unalignable=unalignable,
        has_org=has_org,
        org_val=org_val,
        org_conflict=org_conflict,
        has_title=has_title,
        title_val=title_val,
        title_conflict=title_conflict,
    )


def _extract_key_value_work_fields(
    segment_text: str,
) -> tuple[bool, str | None, bool, bool, str | None, bool]:
    """提取两列键值行右列的单位与职务完整值，重复标签组分别校验。"""
    org_entries: list[list[str]] = []
    title_entries: list[list[str]] = []
    for group in _key_value_work_groups(segment_text):
        org_entries.extend(group.org_entries)
        title_entries.extend(group.title_entries)
    has_org, org_val, org_conflict = _resolve_labeled_field_values(org_entries)
    has_title, title_val, title_conflict = _resolve_labeled_field_values(title_entries)
    return has_org, org_val, org_conflict, has_title, title_val, title_conflict


def _segment_grounds_work_fields(
    segment_text: str,
    norm_org: str,
    norm_title: str,
    *,
    inherited_header: _WorkTableHeader | None = None,
) -> tuple[bool, str | None]:
    header_fields = _extract_header_bound_work_fields(
        segment_text, inherited_header=inherited_header
    )
    if header_fields.unalignable:
        return False, "quote"
    if header_fields.has_table_context and not (
        header_fields.has_org and header_fields.has_title
    ):
        return False, "quote"
    if header_fields.org_conflict:
        return False, "organization"
    if header_fields.has_org and norm_org != normalized_text(
        header_fields.org_val or ""
    ):
        return False, "organization"
    if header_fields.title_conflict:
        return False, "title"
    if header_fields.has_title and norm_title != normalized_text(
        header_fields.title_val or ""
    ):
        return False, "title"
    if header_fields.has_table_context:
        return True, None

    (
        key_has_org,
        key_org,
        key_org_conflict,
        key_has_title,
        key_title,
        key_title_conflict,
    ) = _extract_key_value_work_fields(segment_text)
    if key_org_conflict:
        return False, "organization"
    if key_has_org and norm_org != normalized_text(key_org or ""):
        return False, "organization"
    if key_title_conflict:
        return False, "title"
    if key_has_title and norm_title != normalized_text(key_title or ""):
        return False, "title"

    has_org, org_val, org_conflict, has_title, title_val, title_conflict = (
        _extract_explicit_work_fields(segment_text)
    )
    if org_conflict:
        return False, "organization"
    if has_org:
        norm_explicit_org = normalized_text(org_val or "")
        if not norm_explicit_org or norm_org != norm_explicit_org:
            return False, "organization"
    if title_conflict:
        return False, "title"
    if has_title:
        norm_explicit_title = normalized_text(title_val or "")
        if not norm_explicit_title or norm_title != norm_explicit_title:
            return False, "title"

    structured_has_org, structured_org, structured_has_title, structured_title = (
        _extract_structured_work_fields(segment_text)
    )
    if structured_has_org and norm_org != normalized_text(structured_org or ""):
        return False, "organization"
    if structured_has_title and norm_title != normalized_text(structured_title or ""):
        return False, "title"
    return True, None


def _work_quote_spans_multiple_entries(
    quote: str, norm_org: str, norm_title: str
) -> bool:
    """判断工作经历引文是否跨越了多段明显分隔的经历。"""
    stripped = quote.strip()
    if len(_work_text_segments(stripped)) >= 2:
        return True

    paragraphs = [p.strip() for p in re.split(r"\n[ \t]*\n+", stripped) if p.strip()]
    if len(paragraphs) >= 2:
        both_in_same_paragraph = any(
            norm_org in normalized_text(p) and norm_title in normalized_text(p)
            for p in paragraphs
        )
        if not both_in_same_paragraph:
            return True
        entry_like_paragraphs = sum(
            1
            for p in paragraphs
            if any(
                _is_structured_entry_line(ln.strip())
                or bool(_DATE_RANGE_RE.search(ln))
                or bool(_EXPERIENCE_ENTRY_PREFIX_RE.match(ln.strip()))
                or bool(_ORG_LABEL_RE.match(ln.strip()))
                for ln in p.splitlines()
                if ln.strip()
            )
        )
        if entry_like_paragraphs >= 2:
            return True

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False

    org_span = _find_norm_line_span(lines, norm_org)
    title_span = _find_norm_line_span(lines, norm_title)
    if org_span is not None and title_span is not None:
        first_end = min(org_span[1], title_span[1])
        second_start = max(org_span[0], title_span[0])
        if second_start > first_end:
            date_lines = [idx for idx, ln in enumerate(lines) if _DATE_RANGE_RE.search(ln)]
            if len(date_lines) >= 2 and any(
                first_end < d <= second_start for d in date_lines
            ):
                return True
    return False


def validate_profile(
    profile: dict | None,
    kind: str,
    text: str,
    *,
    allow_user_confirmed: bool = False,
    allowed_skill_codes: set[str] | None = None,
) -> None:
    """结构之外的内容校验：kind 一致、item_id 唯一、证据必须能在原文中找到。

    解析器输出只允许 `origin=extracted`；只有用户核对提交的 profile 才允许
    `user_confirmed`。传入技能词表时，任何非空技能编码都必须属于已启用词表。
    """
    if not isinstance(profile, dict):
        raise AppError("PROFILE_INVALID", "profile 必须是对象")

    if profile.get("kind") != kind:
        actual_kind = profile.get("kind")
        safe_actual = actual_kind if actual_kind in ("resume", "jd") else "invalid_value"
        details = (
            [f"expected={kind}", f"actual={safe_actual}"]
            if allow_user_confirmed
            else [
                "reason=kind_mismatch",
                "source=provider_output",
                "field=kind",
                f"expected={kind}",
                f"actual={safe_actual}",
            ]
        )
        raise AppError(
            "PROFILE_INVALID",
            "profile 类型必须与材料类型一致",
            details=details,
        )

    required_text_fields = {
        "work_experience": ("organization", "title", "description"),
        "projects": ("name", "description"),
        "skills": ("name",),
        "responsibilities": ("text",),
        "requirements": ("text",),
    }

    work_quotes = [
        (item.get("evidence") or {}).get("quote")
        for item in (profile.get("work_experience") or [])
        if isinstance(item, dict)
    ]

    item_ids: set[str] = set()
    item_count = 0
    for section, item in _iter_profile_items(profile):
        item_count += 1
        item_id = item.get("item_id")
        if not item_id or not isinstance(item_id, str) or not item_id.strip():
            raise AppError(
                "PROFILE_INVALID",
                "每个条目必须有 item_id",
                details=(
                    [section]
                    if allow_user_confirmed
                    else [
                        "reason=blank_field",
                        "source=provider_output",
                        f"section={section}",
                        f"field={section}.item_id",
                    ]
                ),
            )
        if item_id in item_ids:
            raise AppError(
                "PROFILE_INVALID",
                "item_id 不能重复",
                details=(
                    [item_id]
                    if allow_user_confirmed
                    else [
                        "reason=duplicate_item_id",
                        "source=provider_output",
                        f"section={section}",
                        f"field={section}.item_id",
                        "duplicate_count=1",
                    ]
                ),
            )
        item_ids.add(item_id)

        source_details = [] if allow_user_confirmed else ["source=provider_output"]

        for field_key in required_text_fields.get(section, ()):
            field_val = item.get(field_key)
            if not isinstance(field_val, str) or not field_val.strip():
                raise AppError(
                    "PROFILE_INVALID",
                    "条目必填字段不能为空白",
                    details=_detail_with_optional_item_id(
                        [
                            "reason=blank_field",
                            *source_details,
                            f"section={section}",
                            f"field={field_key}",
                        ],
                        item_id=item_id,
                        allow_user_confirmed=allow_user_confirmed,
                    ),
                )

        evidence = item.get("evidence") or {}
        quote = evidence.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise AppError(
                "EVIDENCE_INVALID",
                "每个条目必须有非空白原文引用",
                details=_detail_with_optional_item_id(
                    ["reason=blank_quote", *source_details, f"section={section}"],
                    item_id=item_id,
                    allow_user_confirmed=allow_user_confirmed,
                ),
            )
        if quote not in text:
            raise AppError(
                "EVIDENCE_INVALID",
                "引用内容必须能在当前原文中找到",
                details=_detail_with_optional_item_id(
                    ["reason=quote_not_in_text", *source_details, f"section={section}"],
                    item_id=item_id,
                    allow_user_confirmed=allow_user_confirmed,
                ),
            )
        origin = evidence.get("origin")
        if origin not in {"extracted", "user_confirmed"}:
            raise AppError(
                "EVIDENCE_INVALID",
                "证据来源标记非法",
                details=_detail_with_optional_item_id(
                    ["reason=invalid_evidence_origin", *source_details, f"section={section}"],
                    item_id=item_id,
                    allow_user_confirmed=allow_user_confirmed,
                ),
            )
        if origin == "user_confirmed" and not allow_user_confirmed:
            raise AppError(
                "EVIDENCE_INVALID",
                "模型解析结果只能标记为 extracted；user_confirmed 仅用于用户核对提交",
                details=_detail_with_optional_item_id(
                    ["reason=invalid_evidence_origin", *source_details, f"section={section}"],
                    item_id=item_id,
                    allow_user_confirmed=allow_user_confirmed,
                ),
            )

        if section == "work_experience" and not allow_user_confirmed:
            norm_quote = _normalized_table_aware_text(quote)
            norm_org = normalized_text(_unescape_table_cell(item.get("organization") or ""))
            if not norm_org or norm_org not in norm_quote:
                raise AppError(
                    "EVIDENCE_INVALID",
                    "工作经历的单位必须在同条引文中有依据",
                    details=[
                        "reason=ungrounded_work_experience",
                        "source=provider_output",
                        f"section={section}",
                        "field=organization",
                    ],
                )
            norm_title = normalized_text(_unescape_table_cell(item.get("title") or ""))
            if not norm_title or norm_title not in norm_quote:
                raise AppError(
                    "EVIDENCE_INVALID",
                    "工作经历的职务必须在同条引文中有依据",
                    details=[
                        "reason=ungrounded_work_experience",
                        "source=provider_output",
                        f"section={section}",
                        "field=title",
                    ],
                )
            other_quote_contained = any(
                isinstance(other_q, str)
                and other_q != quote
                and len(other_q.strip()) >= 4
                and other_q.strip() in quote
                for other_q in work_quotes
            )
            if (
                other_quote_contained
                or not _quote_fits_single_text_segment(text, quote)
                or _work_quote_spans_multiple_entries(quote, norm_org, norm_title)
            ):
                raise AppError(
                    "EVIDENCE_INVALID",
                    "工作经历引文不能跨越多个分隔条目",
                    details=[
                        "reason=ungrounded_work_experience",
                        "source=provider_output",
                        f"section={section}",
                        "field=quote",
                    ],
                )
            matched_segments = _matching_work_segments(text, quote)
            if matched_segments:
                first_failed_field: str | None = None
                grounded_in_segment = False
                for seg in matched_segments:
                    ok, failed_field = _segment_grounds_work_fields(
                        seg.text,
                        norm_org,
                        norm_title,
                        inherited_header=seg.table_header,
                    )
                    if ok:
                        grounded_in_segment = True
                        break
                    if first_failed_field is None and failed_field is not None:
                        first_failed_field = failed_field
                if not grounded_in_segment:
                    err_field = first_failed_field or "organization"
                    if err_field == "organization":
                        err_msg = "工作经历的单位必须在同条引文中有依据"
                    elif err_field == "title":
                        err_msg = "工作经历的职务必须在同条引文中有依据"
                    else:
                        err_msg = "工作经历表格行无法按表头可靠对齐列，请核对原文"
                    raise AppError(
                        "EVIDENCE_INVALID",
                        err_msg,
                        details=[
                            "reason=ungrounded_work_experience",
                            "source=provider_output",
                            f"section={section}",
                            f"field={err_field}",
                        ],
                    )

        if allowed_skill_codes is not None:
            codes: list[str] = []
            if section == "requirements":
                codes = list(item.get("skill_codes") or [])
            elif section == "projects":
                codes = list(item.get("skill_codes") or [])
            elif section == "skills":
                code = item.get("code")
                codes = [code] if code else []
            unknown = [code for code in codes if code not in allowed_skill_codes]
            if unknown:
                field_name = "code" if section == "skills" else "skill_codes"
                details_tail = (
                    unknown[:5]
                    if allow_user_confirmed
                    else [f"unknown_count={len(unknown)}"]
                )
                raise AppError(
                    "PROFILE_INVALID",
                    "技能编码不在已启用词表中",
                    details=[
                        *_detail_with_optional_item_id(
                            [*source_details, f"section={section}", f"field={field_name}"],
                            item_id=item_id,
                            allow_user_confirmed=allow_user_confirmed,
                        ),
                        *details_tail,
                    ],
                )

    if kind == "resume" and item_count == 0:
        details = (
            ["education", "work_experience", "projects", "skills"]
            if allow_user_confirmed
            else [
                "reason=empty_profile",
                "source=provider_output",
                "field=root",
                "returned=0",
                "education",
                "work_experience",
                "projects",
                "skills",
            ]
        )
        raise AppError(
            "PROFILE_INVALID",
            "简历至少需要一条教育、工作、项目或技能信息",
            details=details,
        )
    if kind == "jd" and item_count == 0:
        details = (
            []
            if allow_user_confirmed
            else [
                "reason=empty_profile",
                "source=provider_output",
                "field=root",
                "returned=0",
                "responsibilities",
                "requirements",
            ]
        )
        raise AppError("PROFILE_INVALID", "JD 至少需要一条职责或要求", details=details)


def normalized_text(value: str) -> str:
    """NFKC → casefold → 仅保留 Unicode 字母与数字。

    精确匹配与二元组相似度共用这一个定义，避免阈值和指纹各自漂移。
    """
    import unicodedata

    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in folded if char.isalnum())


def normalized_stem(stem: str) -> str:
    """生成问法归一化指纹：使用与相似度计算相同的规范化文本。"""
    return hashlib.sha256(normalized_text(stem).encode("utf-8")).hexdigest()


SENIOR_JD_MARKERS = (
    "资深",
    "高级工程师",
    "高级开发",
    "架构师",
    "技术专家",
    "team lead",
    "tech lead",
    "senior",
    "5年以上",
    "五年以上",
    "3年以上",
    "三年以上",
    "五年经验",
    "三年经验",
)


def detect_jd_level_conflict(text: str, level: str) -> list[str]:
    """只对原文明确写出的资深／多年经验做受控冲突，不猜测隐含级别。"""
    lowered = text.lower()
    markers = [marker for marker in SENIOR_JD_MARKERS if marker.lower() in lowered]
    if markers and level in {"intern", "junior"}:
        return [
            "JD 原文明确要求资深或多年经验，当前仅支持实习/初级",
            *[f"matched={marker}" for marker in markers[:3]],
        ]
    return []


def _decode_txt(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AppError(
        "FILE_TEXT_UNAVAILABLE",
        "无法以 UTF-8 读取该 TXT 文件，请另存为 UTF-8 后重试或粘贴文本",
    )


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - 依赖缺失属于部署错误
        raise AppError("FILE_TEXT_UNAVAILABLE", "服务器缺少 PDF 解析组件")
    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                decrypted = reader.decrypt("")
            except Exception:
                decrypted = 0
            if not decrypted:
                raise AppError(
                    "FILE_TEXT_UNAVAILABLE", "PDF 已加密，无法提取文本；请粘贴文本后继续"
                )
        pages: list[str] = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
    except AppError:
        raise
    except Exception:
        raise AppError(
            "FILE_TEXT_UNAVAILABLE", "PDF 文件无法解析，可能已损坏或不是 PDF；也可粘贴文本"
        )
    return "\n".join(pages)


def _escape_docx_table_cell(value: str) -> str:
    """把单元格压成单行，并按表格解析规则转义分隔符与换行。"""
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = text.replace("\\", "\\\\")
    text = text.replace("|", "\\|").replace("｜", "\\｜")
    return text.replace("\n", "\\n")


def _extract_docx(data: bytes) -> str:
    try:
        import docx
        from docx.table import Table
    except ImportError:  # pragma: no cover
        raise AppError("FILE_TEXT_UNAVAILABLE", "服务器缺少 DOCX 解析组件")
    try:
        document = docx.Document(io.BytesIO(data))
        blocks: list[str] = []
        for item in document.iter_inner_content():
            if not isinstance(item, Table):
                blocks.append(item.text)
                continue
            rows: list[str] = []
            for row in item.rows:
                cells = [_escape_docx_table_cell(cell.text) for cell in row.cells]
                rows.append("| " + " | ".join(cells) + " |")
            if not rows:
                continue
            # 表格与前后段落、相邻表格之间留空行，避免表头跨表格继承。
            if blocks and blocks[-1] != "":
                blocks.append("")
            blocks.extend(rows)
            blocks.append("")
    except Exception:
        raise AppError(
            "FILE_TEXT_UNAVAILABLE", "DOCX 文件无法解析，可能已损坏或不是 DOCX；也可粘贴文本"
        )
    while blocks and blocks[-1] == "":
        blocks.pop()
    return "\n".join(blocks)


def extract_text_from_file(data: bytes, extension: str) -> str:
    extension = extension.lower()
    if extension == ".pdf":
        raw = _extract_pdf(data)
    elif extension == ".docx":
        raw = _extract_docx(data)
    elif extension == ".txt":
        raw = _decode_txt(data)
    else:  # pragma: no cover - 上游已拦截
        raise AppError("UNSUPPORTED_FILE_TYPE", "仅支持 PDF、DOCX、TXT")

    text = normalize_text(raw).strip()
    if not text:
        raise AppError(
            "FILE_TEXT_UNAVAILABLE",
            "文件没有可提取的文字（可能是扫描件）。请粘贴文本或换一份可复制文本的文件",
        )
    return check_material_text(text)


@dataclass
class FileValidationResult:
    extension: str
    mime: str
    sha256: str


def validate_file_metadata(filename: str, content_type: str | None, size: int, max_bytes: int) -> FileValidationResult:
    name = (filename or "").strip()
    if not name:
        raise AppError("UNSUPPORTED_FILE_TYPE", "缺少文件名")
    lowered = name.lower()
    if lowered.endswith(".doc"):
        raise AppError("UNSUPPORTED_FILE_TYPE", "不支持 .doc，请另存为 .docx 或 PDF")
    extension = next((ext for ext in SUPPORTED_EXTENSIONS if lowered.endswith(ext)), None)
    if extension is None:
        raise AppError("UNSUPPORTED_FILE_TYPE", "仅支持 PDF、DOCX、TXT 文件")

    declared = (content_type or "").split(";")[0].strip().lower()
    expected = SUPPORTED_EXTENSIONS[extension]
    allowed = {expected}
    if extension == ".txt":
        allowed |= {"text/plain", "application/octet-stream"}
    if extension == ".pdf":
        allowed |= {"application/x-pdf"}
    if declared and declared not in allowed:
        raise AppError(
            "UNSUPPORTED_FILE_TYPE",
            "文件声明类型与扩展名不一致",
            details=[f"declared={declared}", f"expected={expected}"],
        )
    if size <= 0:
        raise AppError("FILE_TOO_LARGE", "文件为空")
    if size > max_bytes:
        raise AppError("FILE_TOO_LARGE", f"文件超过 {max_bytes // (1024 * 1024)} MiB 上限")
    return FileValidationResult(extension=extension, mime=expected, sha256="")


def code_point_length(value: str) -> int:
    """与 Kotlin 的 code point 计数对齐（Python len 即 code point 数）。"""
    return len(value)
