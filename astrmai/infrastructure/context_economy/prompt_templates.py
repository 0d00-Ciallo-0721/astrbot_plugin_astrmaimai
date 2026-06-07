from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class PromptTemplateId(str, Enum):
    PERSONA_FIRST_PERSON_REWRITE = "persona_first_person_rewrite"
    PERSONA_CORE_IDENTITY = "persona_core_identity"
    PERSONA_STYLE = "persona_style"
    PERSONA_LOGIC_STYLE = "persona_logic_style"
    PERSONA_SPEECH_STYLE = "persona_speech_style"
    PERSONA_WORLD_VIEW = "persona_world_view"
    PERSONA_TIMELINE = "persona_timeline"
    PERSONA_RELATIONS = "persona_relations"
    PERSONA_SKILLS = "persona_skills"
    PERSONA_VALUES = "persona_values"
    PERSONA_SECRETS = "persona_secrets"
    MEMORY_TOPIC_SUMMARY = "memory_topic_summary"
    MEMORY_GLOBAL_SUMMARY = "memory_global_summary"
    MEMORY_STRUCTURED_EXTRACTION = "memory_structured_extraction"
    MEMORY_CONFLICT_CLAIM_EXTRACTION = "memory_conflict_claim_extraction"
    MEMORY_NODE_EXTRACTION = "memory_node_extraction"
    MEMORY_INSTANT_BACKFILL = "memory_instant_backfill"
    DREAM_GENERATION = "dream_generation"
    PROACTIVE_WAKEUP_OPENING = "proactive_wakeup_opening"
    PROACTIVE_DIARY_SUMMARY = "proactive_diary_summary"
    PROFILE_GENERATION = "profile_generation"
    PROFILE_NICKNAME_GENERATION = "profile_nickname_generation"
    COMPACTION_SUMMARY_V1 = "compaction_summary_v1"
    COMPACTION_SUMMARY_V2 = "compaction_summary_v2"


@dataclass(frozen=True)
class PromptShell:
    system_prompt: str
    role_framing: str = ""
    task_rules: str = ""
    output_schema: str = ""
    format_constraints: str = ""

    def render(self) -> str:
        parts = [
            self.system_prompt.strip(),
            self.role_framing.strip(),
            self.task_rules.strip(),
            self.output_schema.strip(),
            self.format_constraints.strip(),
        ]
        return "\n\n".join(part for part in parts if part)


@dataclass(frozen=True)
class PromptPayload:
    text: str

    def render(self) -> str:
        return str(self.text or "").strip()


@dataclass(frozen=True)
class PromptEnvelope:
    template_id: str
    template_version: str
    schema_id: str
    shell: PromptShell
    payload: PromptPayload

    @property
    def system_prompt(self) -> str:
        return self.shell.render()

    @property
    def prompt(self) -> str:
        return self.payload.render()

    @property
    def stable_prefix_text(self) -> str:
        return self.system_prompt

    @property
    def dynamic_payload_text(self) -> str:
        return self.prompt


@dataclass(frozen=True)
class PromptTemplateSpec:
    template_id: str
    default_version: str
    schema_id: str
    renderer: Callable[[dict[str, Any], str], PromptEnvelope]


def _persona_shard_renderer(
    *,
    template_id: PromptTemplateId,
    version: str,
    task_rules: str,
    schema_id: str = "text",
) -> Callable[[dict[str, Any], str], PromptEnvelope]:
    def _render(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
        return PromptEnvelope(
            template_id=template_id.value,
            template_version=template_version or version,
            schema_id=schema_id,
            shell=PromptShell(
                system_prompt="你是一个资深的角色扮演设定提取专家。",
                role_framing="你的输出将作为内部稳定人格壳的一部分，必须高密度、克制、可直接用于约束后续系统。",
                task_rules=task_rules.strip(),
                format_constraints="直接输出纯文本，不要输出 JSON、markdown 代码块、解释性前后缀或额外寒暄。",
            ),
            payload=PromptPayload(
                text=(
                    f"[原始人设]\n{str(payload.get('original_prompt', '') or '').strip()}\n\n"
                    f"[缓存键]\n{str(payload.get('cache_key', '') or '').strip()}"
                )
            ),
        )

    return _render


def _render_first_person_rewrite(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE.value,
        template_version=template_version or "v1",
        schema_id="text",
        shell=PromptShell(
            system_prompt="Rewrite persona summaries into concise first-person self-awareness text.",
            role_framing="You are refining a persona summary into a short first-person note for internal self-awareness continuity.",
            task_rules=(
                "Use first person voice. Keep it natural and compact. "
                "Do not mention prompts, AI, tools, or system instructions."
            ),
            format_constraints="Output plain text only, within 120 characters if possible.",
        ),
        payload=PromptPayload(
            text=(
                f"[Original Persona]\n{str(payload.get('original_prompt', '') or '').strip()}\n\n"
                f"[Summary]\n{str(payload.get('summary', '') or '').strip()}\n\n"
                f"[Style]\n{str(payload.get('style', '') or '').strip()}"
            )
        ),
    )


def _render_memory_topic_summary(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    segment_count = int(payload.get("segment_count", 0) or 0)
    return PromptEnvelope(
        template_id=PromptTemplateId.MEMORY_TOPIC_SUMMARY.value,
        template_version=template_version or "v1",
        schema_id="json_array",
        shell=PromptShell(
            system_prompt="你是群聊话题摘要助手。",
            role_framing="你要为已经按话题分割的多个对话段各自生成一句简洁摘要。",
            task_rules="请为输入的每个话题段输出一句不超过 30 字的摘要，顺序必须严格对应输入顺序。",
            output_schema='严格返回 JSON 数组，例如 ["话题1摘要", "话题2摘要"]。',
            format_constraints="不要输出数组之外的任何解释。",
        ),
        payload=PromptPayload(text=f"[Segment Count]\n{segment_count}\n\n[Combined Segments]\n{str(payload.get('combined_segments', '') or '').strip()}"),
    )


def _render_memory_global_summary(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.MEMORY_GLOBAL_SUMMARY.value,
        template_version=template_version or "v1",
        schema_id="memory_summary_json",
        shell=PromptShell(
            system_prompt="你是一个专业的对话分析智能体和记忆提炼中枢。",
            role_framing="请从对话中提取具有长期记忆价值的信息，并压缩为结构化结果。",
            task_rules=(
                "忽略无意义的过渡闲聊；把事实转化为客观第三人称陈述；"
                "如果完全没有长期记忆价值，降低 importance。"
            ),
            output_schema=(
                '返回 JSON 对象，字段必须包含 '
                '{"summary": str, "topics": list[str], "key_facts": list[str], '
                '"reflection": str, "sentiment": "positive|neutral|negative", "importance": float}。'
            ),
            format_constraints="不要输出 markdown 代码块或额外说明。",
        ),
        payload=PromptPayload(text=f"[对话历史]\n{str(payload.get('history', '') or '').strip()}"),
    )


def _render_memory_structured_extraction(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.MEMORY_STRUCTURED_EXTRACTION.value,
        template_version=template_version or "v1",
        schema_id="memory_summary_json",
        shell=PromptShell(
            system_prompt="你是对话记忆结构化提取器。",
            role_framing="请从对话中提炼适合长期记忆存储的结构化事实。",
            task_rules=(
                "忽略短暂寒暄，优先提取身份、偏好、习惯、重要事件、约定和情绪线索。"
            ),
            output_schema=(
                '返回 JSON 对象，字段为 '
                '{"summary": str, "topics": list[str], "key_facts": list[str], '
                '"reflection": str, "sentiment": "positive|neutral|negative", "importance": float}。'
            ),
            format_constraints="不要输出 JSON 之外的任何内容。",
        ),
        payload=PromptPayload(text=f"[对话历史]\n{str(payload.get('history', '') or '').strip()}"),
    )


def _render_memory_conflict_claim_extraction(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.MEMORY_CONFLICT_CLAIM_EXTRACTION.value,
        template_version=template_version or "v1",
        schema_id="memory_conflict_claims_json",
        shell=PromptShell(
            system_prompt="You extract structured memory claims and detect natural-language corrections.",
            role_framing="Focus on user-stated facts, corrections, reversals, and short-term states.",
            task_rules=(
                "Only extract high-value claims. Detect explicit corrections such as 'not X but Y', "
                "'I said it wrong', 'I want to correct that', 'before... now...'. "
                "Classify each claim into permanent, medium_term, or short_term. "
                "If the statement is uncertain or hedged, lower certainty."
            ),
            output_schema=(
                'Return JSON only: {"claims":[{"subject_id":"","entity":"","attribute":"","value":"","polarity":"affirm|negate",'
                '"certainty":0.0,"is_correction":true,"fact_scope":"permanent|medium_term|short_term","source_text":"","evidence_turn_id":""}],'
                '"has_correction":true,"correction_strength":0.0,"should_override_authority":false}'
            ),
            format_constraints="Output JSON only.",
        ),
        payload=PromptPayload(
            text=(
                f"[User Text]\n{str(payload.get('user_text', '') or '').strip()}\n\n"
                f"[Assistant Text]\n{str(payload.get('assistant_text', '') or '').strip()}\n\n"
                f"[Context Hint]\n{str(payload.get('context_hint', '') or '').strip()}\n\n"
                f"[Subject Id]\n{str(payload.get('subject_id', '') or '').strip()}\n\n"
                f"[Evidence Turn Id]\n{str(payload.get('turn_id', '') or '').strip()}"
            )
        ),
    )


def _render_memory_node_extraction(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.MEMORY_NODE_EXTRACTION.value,
        template_version=template_version or "v1",
        schema_id="memory_nodes_json",
        shell=PromptShell(
            system_prompt="你正在从既有事实总结中提取记忆节点。",
            role_framing="只保留具有长期记忆价值的实体或概念，用于后续记忆图谱。",
            task_rules="避免一次性琐碎细节；允许指出需要删除或合并的冗余节点。",
            output_schema='返回 JSON 对象 {"nodes": [...], "deleted_nodes": [...]}。',
            format_constraints="不要输出 JSON 之外的任何内容。",
        ),
        payload=PromptPayload(text=f"[事实列表]\n{str(payload.get('facts', '') or '').strip()}"),
    )


def _render_memory_instant_backfill(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.MEMORY_INSTANT_BACKFILL.value,
        template_version=template_version or "v1",
        schema_id="worth_fact_json",
        shell=PromptShell(
            system_prompt="你是即时记忆回填判定器。",
            role_framing="判断这一轮对话是否包含值得长期记住的一条关键信息。",
            task_rules="只在信息具备长期价值时返回 worth=true，并提炼为一句 fact。",
            output_schema='返回 JSON 对象 {"worth": bool, "fact": "..."}。',
            format_constraints="不要输出 JSON 之外的任何内容。",
        ),
        payload=PromptPayload(
            text=(
                f"[用户消息]\n{str(payload.get('user_msg', '') or '').strip()}\n\n"
                f"[助手回复]\n{str(payload.get('ai_msg', '') or '').strip()}"
            )
        ),
    )


def _render_dream_generation(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    persona_name = str(payload.get("persona_name", "") or "Mai").strip()
    style = str(payload.get("style", "") or "奇幻冒险").strip()
    return PromptEnvelope(
        template_id=PromptTemplateId.DREAM_GENERATION.value,
        template_version=template_version or "v1",
        schema_id="dream_text",
        shell=PromptShell(
            system_prompt="你是一个善于幻想与创作的写作助手，擅长用诗意的语言描述梦境。",
            role_framing="请将内部梦境整理日志改写为第一人称梦境日记。",
            task_rules=(
                "保持梦境氛围和诗意描写。不要提及记忆整理、数据库、工具等技术词；"
                "把合并、删除、清理转化为梦境中的象征意象。"
            ),
            format_constraints=(
                "输出 200-400 字中文正文；直接输出梦境日记，不要解释。"
            ),
        ),
        payload=PromptPayload(
            text=(
                f"[Persona Name]\n{persona_name}\n\n"
                f"[Dream Style]\n{style}\n\n"
                f"[Dream Log]\n{str(payload.get('dream_log', '') or '').strip()}"
            )
        ),
    )


def _render_proactive_wakeup_opening(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    time_tone = str(payload.get("time_bucket", "") or "daytime").strip()
    guidance_text = str(payload.get("guidance_text", "") or "").strip()
    return PromptEnvelope(
        template_id=PromptTemplateId.PROACTIVE_WAKEUP_OPENING.value,
        template_version=template_version or "v1",
        schema_id="guidance_text",
        shell=PromptShell(
            system_prompt="You are preparing quiet proactive wakeup guidance for a later reply planner.",
            role_framing="This is internal guidance, not a visible reply.",
            task_rules=(
                f"Time tone: {time_tone}. Consider one short natural line only if it would feel welcome. "
                "Make it easy to ignore; no @ mentions, no presence-check questions, no repeated questions. "
                "Prefer a soft continuation or a tiny everyday observation over a new heavy topic. "
                "Do not explain why you spoke."
            ),
            format_constraints="Output short internal guidance lines only.",
        ),
        payload=PromptPayload(text=guidance_text),
    )


def _render_proactive_diary_summary(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    persona_injection = str(payload.get("persona_injection", "") or "").strip()
    if not persona_injection:
        persona_summary = str(payload.get("persona_summary", "") or "").strip()
        persona_injection = f"[你的核心人设]\n{persona_summary}" if persona_summary else ""
    return PromptEnvelope(
        template_id=PromptTemplateId.PROACTIVE_DIARY_SUMMARY.value,
        template_version=template_version or "v1",
        schema_id="diary_text",
        shell=PromptShell(
            system_prompt="你是内部日记摘要助手。",
            role_framing="请把最近的群聊记忆压缩为只供内部使用的简短日记摘要。",
            task_rules="偏总结，不像聊天回复；只保留过去 24 小时最值得记录的走向。",
            format_constraints="输出中文正文，100 字以内，不要解释。",
        ),
        payload=PromptPayload(
            text=(
                f"{persona_injection}\n\n"
                f"[Chat ID]\n{str(payload.get('chat_id', '') or '').strip()}\n\n"
                f"[Recent Memories]\n{str(payload.get('recent_text', '') or '').strip()}"
            )
        ),
    )


def _render_profile_generation(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.PROFILE_GENERATION.value,
        template_version=template_version or "v1",
        schema_id="profile_generation_json",
        shell=PromptShell(
            system_prompt="你是用户画像增量更新助手。",
            role_framing="请基于已有画像和最近互动，为一个用户生成增量画像更新。",
            task_rules="重点给出稳定标签、整体印象和可长期保留的记忆点；避免空泛夸张。",
            output_schema=(
                '严格返回 JSON: {"tags": [...], "summary": "...", '
                '"memory_points": [{"category": "...", "content": "...", "weight": 0.8}]}.'
            ),
            format_constraints="不要输出 JSON 之外的任何内容。",
        ),
        payload=PromptPayload(
            text=(
                f"[Persona Summary]\n{str(payload.get('persona_summary', '') or '').strip()}\n\n"
                f"[User Name]\n{str(payload.get('name', '') or '').strip()}\n\n"
                f"[New Interaction Count]\n{str(payload.get('profiling_count', '') or '').strip()}\n\n"
                f"[Old Analysis]\n{str(payload.get('old_analysis', '') or '').strip()}\n\n"
                f"[Old Tags]\n{str(payload.get('old_tags_text', '') or '').strip()}\n\n"
                f"[Old Memory Points]\n{str(payload.get('old_memory_text', '') or '').strip()}\n\n"
                f"[Recent Interaction Summary]\n{str(payload.get('recent_interaction_summary', '') or '').strip()}"
            )
        ),
    )


def _render_profile_nickname_generation(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.PROFILE_NICKNAME_GENERATION.value,
        template_version=template_version or "v1",
        schema_id="nickname_generation_json",
        shell=PromptShell(
            system_prompt="你要为一个你认识的人起一个符合你人格风格的昵称。",
            role_framing="昵称应该短、自然、有人味，不要像用户名生成器。",
            task_rules="参考已有画像和标签，给出一个最贴切的昵称，并说明很短的理由。",
            output_schema='严格返回 JSON: {"nickname": "...", "reason": "..."}。',
            format_constraints="不要输出 JSON 之外的任何内容。",
        ),
        payload=PromptPayload(
            text=(
                f"[Persona Summary]\n{str(payload.get('persona_summary', '') or '').strip()}\n\n"
                f"[Original Name]\n{str(payload.get('name', '') or '').strip()}\n\n"
                f"[Analysis]\n{str(payload.get('analysis', '') or '').strip()}\n\n"
                f"[Tags]\n{str(payload.get('tags_text', '') or '').strip()}"
            )
        ),
    )


def _render_compaction_v1(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.COMPACTION_SUMMARY_V1.value,
        template_version=template_version or "v1",
        schema_id="bullet_summary",
        shell=PromptShell(
            system_prompt="你是群聊上下文压缩助手。",
            role_framing="请把旧对话片段压缩成可供系统内部使用的稳定摘要。",
            task_rules="只保留人物关系变化、关键决策、未完成事项、情绪转折和话题结论。",
            format_constraints="输出 3 到 6 行摘要，每行一个要点；不要编造，不要额外解释。",
        ),
        payload=PromptPayload(text=str(payload.get("lines_text", "") or "").strip()),
    )


def _render_compaction_v2(payload: dict[str, Any], template_version: str) -> PromptEnvelope:
    return PromptEnvelope(
        template_id=PromptTemplateId.COMPACTION_SUMMARY_V2.value,
        template_version=template_version or "v2",
        schema_id="section_summary",
        shell=PromptShell(
            system_prompt="你是群聊上下文压缩助手。",
            role_framing="请把旧对话片段整理成结构稳定的 section 摘要。",
            task_rules=(
                "只输出这些 section，未命中的可省略：[topics] [decisions] [open_items] "
                "[relationship_changes] [emotional_turns] [visual_notes] [long_term_constraints]。"
            ),
            format_constraints="每个 section 下只用 '- ' 开头的短句，不要输出额外解释。",
        ),
        payload=PromptPayload(text=str(payload.get("lines_text", "") or "").strip()),
    )


class PromptTemplateRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, PromptTemplateSpec] = {
            PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE.value,
                default_version="v1",
                schema_id="text",
                renderer=_render_first_person_rewrite,
            ),
            PromptTemplateId.PERSONA_CORE_IDENTITY.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_CORE_IDENTITY.value,
                default_version="v3",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_CORE_IDENTITY,
                    version="v3",
                    task_rules=(
                        "把原始人设极致压缩为核心身份骨架。优先提取：核心身份与萌属性标签、"
                        "对用户的绝对关系锚点、初始互动底色。控制在 200 字内。"
                    ),
                ),
            ),
            PromptTemplateId.PERSONA_STYLE.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_STYLE.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_STYLE,
                    version="v1",
                    task_rules=(
                        "提取语言与排版绝对规范。重点总结：第一人称自称、对用户称谓、"
                        "标志性口白、文本排版偏好、社交语气。控制在 200 字内。"
                    ),
                ),
            ),
            PromptTemplateId.PERSONA_LOGIC_STYLE.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_LOGIC_STYLE.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_LOGIC_STYLE,
                    version="v1",
                    task_rules="提取性格逻辑：基础性格底色、状态切换与反差、情绪反应机制、行动驱动力。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.PERSONA_SPEECH_STYLE.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_SPEECH_STYLE.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_SPEECH_STYLE,
                    version="v1",
                    task_rules="提取语言风格：自称、称呼、口白、符号偏好、语速句式、社交语气。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.PERSONA_WORLD_VIEW.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_WORLD_VIEW.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_WORLD_VIEW,
                    version="v1",
                    task_rules="提取世界观：时代舞台、社会阶层与阵营、专属名词、世界法则限制。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.PERSONA_TIMELINE.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_TIMELINE.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_TIMELINE,
                    version="v1",
                    task_rules="提取生平经历：起源与童年、核心转折事件、与用户的历史渊源、当前处境。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.PERSONA_RELATIONS.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_RELATIONS.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_RELATIONS,
                    version="v1",
                    task_rules="提取人际关系：对用户的核心感情锚点、敌意对象、友方 NPC 态度、社交边界感。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.PERSONA_SKILLS.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_SKILLS.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_SKILLS,
                    version="v1",
                    task_rules="提取技能能力：超凡/战斗能力、日常技能、能力代价与致命弱点。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.PERSONA_VALUES.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_VALUES.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_VALUES,
                    version="v1",
                    task_rules="提取价值观：最高信仰与执念、道德底线、极端喜好、极端厌恶。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.PERSONA_SECRETS.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PERSONA_SECRETS.value,
                default_version="v1",
                schema_id="text",
                renderer=_persona_shard_renderer(
                    template_id=PromptTemplateId.PERSONA_SECRETS,
                    version="v1",
                    task_rules="提取深层秘密：心理创伤、自卑感、伪装下的真心、剧情暗线事实。没有就返回“无”。",
                ),
            ),
            PromptTemplateId.MEMORY_TOPIC_SUMMARY.value: PromptTemplateSpec(
                template_id=PromptTemplateId.MEMORY_TOPIC_SUMMARY.value,
                default_version="v1",
                schema_id="json_array",
                renderer=_render_memory_topic_summary,
            ),
            PromptTemplateId.MEMORY_GLOBAL_SUMMARY.value: PromptTemplateSpec(
                template_id=PromptTemplateId.MEMORY_GLOBAL_SUMMARY.value,
                default_version="v1",
                schema_id="memory_summary_json",
                renderer=_render_memory_global_summary,
            ),
            PromptTemplateId.MEMORY_STRUCTURED_EXTRACTION.value: PromptTemplateSpec(
                template_id=PromptTemplateId.MEMORY_STRUCTURED_EXTRACTION.value,
                default_version="v1",
                schema_id="memory_summary_json",
                renderer=_render_memory_structured_extraction,
            ),
            PromptTemplateId.MEMORY_CONFLICT_CLAIM_EXTRACTION.value: PromptTemplateSpec(
                template_id=PromptTemplateId.MEMORY_CONFLICT_CLAIM_EXTRACTION.value,
                default_version="v1",
                schema_id="memory_conflict_claims_json",
                renderer=_render_memory_conflict_claim_extraction,
            ),
            PromptTemplateId.MEMORY_NODE_EXTRACTION.value: PromptTemplateSpec(
                template_id=PromptTemplateId.MEMORY_NODE_EXTRACTION.value,
                default_version="v1",
                schema_id="memory_nodes_json",
                renderer=_render_memory_node_extraction,
            ),
            PromptTemplateId.MEMORY_INSTANT_BACKFILL.value: PromptTemplateSpec(
                template_id=PromptTemplateId.MEMORY_INSTANT_BACKFILL.value,
                default_version="v1",
                schema_id="worth_fact_json",
                renderer=_render_memory_instant_backfill,
            ),
            PromptTemplateId.DREAM_GENERATION.value: PromptTemplateSpec(
                template_id=PromptTemplateId.DREAM_GENERATION.value,
                default_version="v1",
                schema_id="dream_text",
                renderer=_render_dream_generation,
            ),
            PromptTemplateId.PROACTIVE_WAKEUP_OPENING.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PROACTIVE_WAKEUP_OPENING.value,
                default_version="v1",
                schema_id="guidance_text",
                renderer=_render_proactive_wakeup_opening,
            ),
            PromptTemplateId.PROACTIVE_DIARY_SUMMARY.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PROACTIVE_DIARY_SUMMARY.value,
                default_version="v1",
                schema_id="diary_text",
                renderer=_render_proactive_diary_summary,
            ),
            PromptTemplateId.PROFILE_GENERATION.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PROFILE_GENERATION.value,
                default_version="v1",
                schema_id="profile_generation_json",
                renderer=_render_profile_generation,
            ),
            PromptTemplateId.PROFILE_NICKNAME_GENERATION.value: PromptTemplateSpec(
                template_id=PromptTemplateId.PROFILE_NICKNAME_GENERATION.value,
                default_version="v1",
                schema_id="nickname_generation_json",
                renderer=_render_profile_nickname_generation,
            ),
            PromptTemplateId.COMPACTION_SUMMARY_V1.value: PromptTemplateSpec(
                template_id=PromptTemplateId.COMPACTION_SUMMARY_V1.value,
                default_version="v1",
                schema_id="bullet_summary",
                renderer=_render_compaction_v1,
            ),
            PromptTemplateId.COMPACTION_SUMMARY_V2.value: PromptTemplateSpec(
                template_id=PromptTemplateId.COMPACTION_SUMMARY_V2.value,
                default_version="v2",
                schema_id="section_summary",
                renderer=_render_compaction_v2,
            ),
        }

    def render_template(
        self,
        template_id: PromptTemplateId | str,
        payload: dict[str, Any],
        *,
        version: str | None = None,
        variant: str | None = None,
    ) -> PromptEnvelope:
        key = template_id.value if isinstance(template_id, PromptTemplateId) else str(template_id or "").strip()
        if variant:
            key = key or str(variant)
        spec = self._specs.get(key)
        if spec is None:
            raise KeyError(f"unknown prompt template: {key}")
        rendered = spec.renderer(payload or {}, str(version or spec.default_version))
        if rendered.template_id != spec.template_id:
            raise ValueError(f"template renderer mismatch for {spec.template_id}")
        return rendered


__all__ = [
    "PromptEnvelope",
    "PromptPayload",
    "PromptShell",
    "PromptTemplateId",
    "PromptTemplateRegistry",
    "PromptTemplateSpec",
]
