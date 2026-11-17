"""DeepSeek 提示词（m1-v1）。

约束与业务规则一致：只使用材料中真实存在的事实；不推断学校层次、人格、稳定性或造假嫌疑；
输出必须是可被 Pydantic 与证据校验的 JSON。
"""

from __future__ import annotations

import json

SYSTEM_BASE = """你是 MockMate 的面试准备助手，服务对象是校招/实习求职学生。
必须遵守：
1. 只使用用户材料中明确出现的事实；不得编造公司、项目、奖项、年限或生产规模。
2. 不得推断学校层次、跳槽风险、人格特征或“造假嫌疑”。
3. 输出严格 JSON，不要 Markdown、不要解释性前后缀。
4. 所有 quote 必须是材料原文中逐字出现的连续片段。"""


def parse_prompt(*, text: str, kind: str, role_id: str, level: str) -> tuple[str, str]:
    if kind == "resume":
        schema = {
            "kind": "resume",
            "education": [
                {
                    "item_id": "edu-1",
                    "degree": "本科在读",
                    "major": "计算机科学与技术",
                    "evidence": {"quote": "原文逐字片段", "origin": "extracted"},
                }
            ],
            "work_experience": [],
            "projects": [
                {
                    "item_id": "project-1",
                    "name": "项目名",
                    "description": "原文中的项目描述",
                    "skill_codes": ["java.core"],
                    "evidence": {"quote": "原文逐字片段", "origin": "extracted"},
                }
            ],
            "skills": [
                {
                    "item_id": "skill-1",
                    "name": "Java 集合",
                    "code": "java.core",
                    "evidence": {"quote": "原文逐字片段", "origin": "extracted"},
                }
            ],
        }
        instruction = (
            "把简历原文结构化。没有工作经历时 work_experience 输出空数组，这完全合法；"
            "没有项目时 projects 为空数组。education/work/projects/skills 至少保留一项。"
            f"本次目标岗位 {role_id}，级别 {level}；该信息只用于技能码命名，不得据此补充材料中没有的事实。"
        )
    else:
        schema = {
            "kind": "jd",
            "role_id": role_id,
            "level": level,
            "title": "岗位标题",
            "responsibilities": [
                {
                    "item_id": "responsibility-1",
                    "text": "职责正文",
                    "evidence": {"quote": "原文逐字片段", "origin": "extracted"},
                }
            ],
            "requirements": [
                {
                    "item_id": "req-1",
                    "text": "要求正文",
                    "skill_codes": ["java.core"],
                    "importance": "must",
                    "evidence": {"quote": "原文逐字片段", "origin": "extracted"},
                }
            ],
        }
        instruction = (
            "把 JD 原文结构化，逐条提炼职责与显式技能要求。不要补写原文没有的要求；"
            f"role_id/level 的默认设置为 {role_id}/{level}，但只在原文未明确相反信息时沿用。"
            "若原文明确写资深、多年经验、架构师或跨岗位，不得为了匹配而改写成实习/初级；"
            "保留原文事实，系统会给出受控冲突并请用户修改。"
        )
    user = f"""{instruction}

请只输出如下形状的 JSON：
{json.dumps(schema, ensure_ascii=False)}

材料原文：
<<<
{text}
>>>"""
    return SYSTEM_BASE, user


def match_prompt(*, resume_text, resume_profile, jd_profile, resume_snapshot_id) -> tuple[str, str]:
    schema = {
        "rows": [
            {
                "requirement_id": "req-1",
                "status": "supported",
                "resume_evidence": [
                    {
                        "snapshot_id": resume_snapshot_id,
                        "item_id": "project-1",
                        "quote": "简历原文逐字片段",
                    }
                ],
                "explanation": "为什么是这个结论，只依据材料",
                "suggested_focus": "面试准备建议",
            }
        ]
    }
    user = f"""逐条对照 JD 要求与简历材料，给出“要求→支持原文→待验证／未体现”的对应关系。
status 只能是 supported（简历中有可定位原文支持）、to_verify（有相关背景但要求未直接体现）、
not_evidenced（当前材料没有支持）。resume_evidence 可为空数组，但填写时 quote 必须逐字来自简历原文，
item_id 必须来自简历结构。不要输出百分比或录用概率。

简历结构（anchors.item_id 必须取自这里的 item_id）：
{json.dumps(resume_profile, ensure_ascii=False)}

JD 结构：
{json.dumps(jd_profile, ensure_ascii=False)}

只输出如下 JSON：
{json.dumps(schema, ensure_ascii=False)}

简历原文：
<<<
{resume_text}
>>>"""
    return SYSTEM_BASE, user


def generate_prompt(
    *,
    count,
    resume_text,
    resume_profile,
    resume_snapshot_id,
    jd_text,
    jd_snapshot_id,
    jd_profile,
    rubric,
    avoid_stems,
    context=None,
) -> tuple[str, str]:
    context = context or {}
    slot_type = context.get("question_type", "experience")
    anchors = list(context.get("allowed_anchors") or [])
    sample_anchor = (
        {
            "snapshot_id": anchors[0]["snapshot_id"],
            "item_id": anchors[0]["item_id"],
            "quote": anchors[0]["quote"],
        }
        if anchors
        else {
            "snapshot_id": resume_snapshot_id,
            "item_id": "project-1",
            "quote": "材料原文逐字片段",
        }
    )
    schema = {
        "questions": [
            {
                "question_type": slot_type,
                "canonical_id": "whiteboard-风格稳定标识",
                "stem": "面向该学生的个性化主问题（8–500 字）",
                "focus": "考察目标",
                "topic_key": "技能主题",
                "skill_codes": ["java.core"],
                "anchors": [sample_anchor],
                "evaluation": {
                    "goal": "考察目标",
                    "reference_approach": "参考思路",
                    "key_points": [
                        {"point": "要点一", "weight": 0.6},
                        {"point": "要点二", "weight": 0.4},
                    ],
                    "acceptable_alternatives": ["合理替代解"],
                    "common_mistakes": ["常见错误边界"],
                    "rubric": rubric,
                    "basis": [
                        {
                            "kind": "material",
                            "ref_id": f"{resume_snapshot_id}/project-1",
                            "note": "以简历原文为事实边界",
                        },
                        {
                            "kind": "reasoning",
                            "ref_id": "hypothetical-analysis",
                            "note": "仅评价思路与验证",
                        },
                    ],
                    "answer_status": "model_generated",
                    "trust": "provisional",
                    "uncertainties": ["仍需人工复核的点"],
                },
            }
        ]
    }
    user = f"""本次只为这一个槽位生成 {count} 道个性化主问题（调用方固定 count=1）：
- 本次槽位类型固定为 {slot_type}：experience（经历／推理表达）或 knowledge（技术知识）。
- 只允许从 slot_context.allowed_anchors 选择 anchors；item_id 与 quote 必须原样来自该列表。
- 没有项目／工作经历时，可以引用 JD 明确要求构造“假设场景”，但必须说明是假设，不得编造成做过。
- knowledge 题必须在 evaluation.basis 引用 slot_context.fact_cards 中的已审 fact_card；
  不得把技术断言藏在 reasoning 里绕过。experience 题至少引用一条本场材料依据。
- 不得与题库题干重复或同义改写；不得复用 avoid_stems 中列出的题干。
- 评价依据必须完整：考察目标、参考思路、评分要点（权重和=1）、合理替代解、错误边界、量表。
- 生成题 answer_status 固定 model_generated，trust 固定 provisional，绝不能写 verified/reviewed。
- uncertainties 必须为空数组；仍有未解决事实冲突时不要输出该题，交由调用方失败处理。
- 只能返回 1 道题；多返回或少返回都视为非法输出，不能静默截断。

已有题干（禁止复用）：
{json.dumps(avoid_stems, ensure_ascii=False)}

槽位上下文（类型、允许锚点、依据卡、匹配重点、量表、禁止题干）：
{json.dumps(context, ensure_ascii=False)}

简历结构（anchors.item_id 必须取自这里的 item_id）：
{json.dumps(resume_profile, ensure_ascii=False)}

简历快照 ID：{resume_snapshot_id}
JD 快照 ID：{jd_snapshot_id}

JD 结构：
{json.dumps(jd_profile, ensure_ascii=False)}

只输出如下 JSON：
{json.dumps(schema, ensure_ascii=False)}

简历原文：
<<<
{resume_text}
>>>

JD 原文：
<<<
{jd_text}
>>>"""
    return SYSTEM_BASE, user


def review_prompt(*, questions, material_texts, fact_cards=None) -> tuple[str, str]:
    schema = {
        "duplicate_pairs": [{"left_id": "id", "right_id": "id", "reason": "重复原因"}],
        "grounding_issues": [
            {"question_id": "id", "claim": "题目中的陈述", "reason": "为什么材料无法支持"}
        ],
        "uncertain": False,
    }
    user = f"""检查以下面试主问题：
1. 是否存在相同问法、同义改写或只是替换项目的重复题（duplicate_pairs）。
2. 是否包含材料或已审 fact card 无法支持的陈述、编造经历、与 fact card 冲突的技术断言
   （grounding_issues）。
3. 若无法确定，uncertain 设 true。不要为了通过而强行判为不重复。
4. left_id/right_id/question_id 必须使用题目列表中真实存在的 id，不得编造或重复关系。

题目（含考察目标、参考思路、评分要点、锚点与评价依据）：
{json.dumps(questions, ensure_ascii=False)}

材料原文：
{json.dumps(material_texts, ensure_ascii=False)}

已审依据卡：
{json.dumps(fact_cards or [], ensure_ascii=False)}

只输出如下 JSON：
{json.dumps(schema, ensure_ascii=False)}"""
    return SYSTEM_BASE, user
