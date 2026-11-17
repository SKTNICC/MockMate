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
        raise AppError(
            "PROFILE_INVALID",
            "profile 类型必须与材料类型一致",
            details=[f"expected={kind}", f"actual={profile.get('kind')}"],
        )

    item_ids: set[str] = set()
    item_count = 0
    for section, item in _iter_profile_items(profile):
        item_count += 1
        item_id = item.get("item_id")
        if not item_id:
            raise AppError("PROFILE_INVALID", "每个条目必须有 item_id", details=[section])
        if item_id in item_ids:
            raise AppError("PROFILE_INVALID", "item_id 不能重复", details=[item_id])
        item_ids.add(item_id)

        evidence = item.get("evidence") or {}
        quote = evidence.get("quote")
        if not quote or not isinstance(quote, str):
            raise AppError("EVIDENCE_INVALID", "每个条目必须有原文引用", details=[item_id])
        if quote not in text:
            raise AppError(
                "EVIDENCE_INVALID",
                "引用内容必须能在当前原文中找到",
                details=[item_id],
            )
        origin = evidence.get("origin")
        if origin not in {"extracted", "user_confirmed"}:
            raise AppError("EVIDENCE_INVALID", "证据来源标记非法", details=[item_id])
        if origin == "user_confirmed" and not allow_user_confirmed:
            raise AppError(
                "EVIDENCE_INVALID",
                "模型解析结果只能标记为 extracted；user_confirmed 仅用于用户核对提交",
                details=[item_id],
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
                raise AppError(
                    "PROFILE_INVALID",
                    "技能编码不在已启用词表中",
                    details=unknown[:5],
                )

    if kind == "resume" and item_count == 0:
        raise AppError(
            "PROFILE_INVALID",
            "简历至少需要一条教育、工作、项目或技能信息",
            details=["education", "work_experience", "projects", "skills"],
        )
    if kind == "jd" and item_count == 0:
        raise AppError("PROFILE_INVALID", "JD 至少需要一条职责或要求")


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


def _extract_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError:  # pragma: no cover
        raise AppError("FILE_TEXT_UNAVAILABLE", "服务器缺少 DOCX 解析组件")
    try:
        document = docx.Document(io.BytesIO(data))
        paragraphs = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    paragraphs.append(cell.text)
    except Exception:
        raise AppError(
            "FILE_TEXT_UNAVAILABLE", "DOCX 文件无法解析，可能已损坏或不是 DOCX；也可粘贴文本"
        )
    return "\n".join(paragraphs)


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
