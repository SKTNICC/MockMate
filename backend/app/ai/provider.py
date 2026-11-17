"""AI provider 协议与适配器。

- disabled：默认；任何 AI 任务在入队前明确拒绝（503 AI_NOT_CONFIGURED）。
- deepseek：真实外部模型，密钥只从服务端环境读取。
- test：仅在 ALLOW_TEST_FIXTURES=true 的非生产环境启用，输出确定且醒目标注 test_only。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from app.core.config import Settings
from app.core.errors import AppError
from pydantic import BaseModel, ValidationError

from app.schemas import DuplicateReviewOutput, GeneratedQuestionOutput, MatchRow, ResumeProfile, JdProfile
from app.services.text import normalized_stem


class AiProvider(ABC):
    name: str
    model_id: str
    prompt_version: str
    test_only: bool = False

    @abstractmethod
    def parse_material(self, *, text: str, kind: str, role_id: str, level: str) -> dict:
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


def _validate(model: type[BaseModel], data: object):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        details = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:4]
        ]
        raise AppError("AI_INVALID_OUTPUT", "AI 输出不符合契约结构", details=details)


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

    def parse_material(self, *, text: str, kind: str, role_id: str, level: str) -> dict:
        self._begin_call()
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
                            "skill_codes": _skill_codes(line),
                            "evidence": {"quote": line, "origin": "extracted"},
                        }
                    )
                elif "工作" in line and any(word in line for word in ("公司", "实习", "任职", "岗位")):
                    name, description = _split_name_and_desc(line)
                    works.append(
                        {
                            "item_id": f"work-{len(works) + 1}",
                            "organization": name,
                            "title": "成员",
                            "description": description,
                            "evidence": {"quote": line, "origin": "extracted"},
                        }
                    )
                elif any(word in line for word in ("学习", "技能", "掌握", "熟悉", "了解")):
                    skills.append(
                        {
                            "item_id": f"skill-{len(skills) + 1}",
                            "name": line[:60],
                            "code": (_skill_codes(line) or [None])[0],
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
                        "skill_codes": _skill_codes(line),
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
                    "skill_codes": _skill_codes(text),
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

    # -- HTTP
    def _chat_json(self, system: str, user: str) -> tuple[dict, str]:
        import httpx

        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        self.last_call = {
            "provider": self.name,
            "model": self.model_id,
            "request_id": "",
            "usage": None,
            "outcome": "unknown",
        }
        try:
            client_kwargs = {"timeout": self._settings.ai_timeout_seconds}
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            with httpx.Client(**client_kwargs) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._settings.ai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
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
            data = response.json()
        except ValueError:
            self.last_call["outcome"] = "known_invalid"
            raise AppError("AI_INVALID_OUTPUT", "AI 服务返回的 HTTP 响应不是合法 JSON")
        if not isinstance(data, dict):
            self.last_call["outcome"] = "known_invalid"
            raise AppError("AI_INVALID_OUTPUT", "AI 服务返回的响应形状非法")

        request_id = data.get("id") or response.headers.get("x-request-id") or ""
        usage = data.get("usage")
        self.last_call["request_id"] = str(request_id or "")
        self.last_call["usage"] = usage if isinstance(usage, (dict, type(None))) else None

        try:
            choices = data["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.last_call["outcome"] = "known_invalid"
            raise AppError("AI_INVALID_OUTPUT", "AI 响应缺少合法的 choices/message/content")
        if not isinstance(content, str) or not content.strip():
            self.last_call["outcome"] = "known_invalid"
            raise AppError("AI_INVALID_OUTPUT", "AI 返回内容不是非空字符串")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            self.last_call["outcome"] = "known_invalid"
            raise AppError("AI_INVALID_OUTPUT", "AI 返回内容不是合法 JSON")
        if not isinstance(parsed, dict):
            self.last_call["outcome"] = "known_invalid"
            raise AppError("AI_INVALID_OUTPUT", "AI 返回的 JSON 必须是对象")
        self.last_call["outcome"] = "success"
        return parsed, request_id

    def parse_material(self, *, text: str, kind: str, role_id: str, level: str) -> dict:
        from app.ai.prompts import parse_prompt

        system, user = parse_prompt(text=text, kind=kind, role_id=role_id, level=level)
        parsed, _ = self._chat_json(system, user)
        if kind == "resume":
            return _validate(ResumeProfile, parsed).model_dump(mode="json")
        return _validate(JdProfile, parsed).model_dump(mode="json")

    def match_requirements(self, *, resume_snapshot, jd_snapshot) -> list[dict]:
        from app.ai.prompts import match_prompt

        system, user = match_prompt(
            resume_text=resume_snapshot.text,
            resume_profile=resume_snapshot.profile,
            jd_profile=jd_snapshot.profile,
            resume_snapshot_id=str(resume_snapshot.id),
        )
        parsed, _ = self._chat_json(system, user)
        rows = parsed.get("rows") if isinstance(parsed, dict) else None
        if not rows:
            raise AppError("AI_INVALID_OUTPUT", "AI 未返回匹配结果")
        return [_validate(MatchRow, row).model_dump(mode="json") for row in rows]

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
        parsed, _ = self._chat_json(system, user)
        items = parsed.get("questions") if isinstance(parsed, dict) else None
        if not items:
            raise AppError("AI_INVALID_OUTPUT", "AI 未返回生成题")
        return [_validate(GeneratedQuestionOutput, item).model_dump(mode="json") for item in items]

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
        parsed, _ = self._chat_json(system, user)
        return _validate(DuplicateReviewOutput, parsed).model_dump(mode="json")
