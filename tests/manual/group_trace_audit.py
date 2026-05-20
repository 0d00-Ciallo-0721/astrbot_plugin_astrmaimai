from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from astrmai.conversation.attention.context_compaction import ContextCompactionEngine
from astrmai.conversation.attention.group_dialogue_store import DialogueSegment, GroupDialogueStore
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin


DEFAULT_MODEL = "audit-fake-1"
DEFAULT_CHAT_ID_PREFIX = "audit:GroupMessage:"


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def one_line(text: str, limit: int = 180) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)] + "..."


def normalize_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def keyword_tokens(text: str) -> list[str]:
    cleaned = normalize_text(text).lower()
    if not cleaned:
        return []
    separators = ",.!?;:()[]{}<>\"'`~@#%^&*-_=+/\\|\t\r\n"
    for token in separators:
        cleaned = cleaned.replace(token, " ")
    raw_parts = [part for part in cleaned.split(" ") if part]
    kept: list[str] = []
    for part in raw_parts:
        if len(part) >= 2:
            kept.append(part)
    return kept[:24]


def unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = normalize_text(item)
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(normalized)
    return result


def build_keyword_bucket(texts: list[str], *, limit: int = 16) -> list[str]:
    tokens: list[str] = []
    for text in texts:
        tokens.extend(keyword_tokens(text))
    return unique_preserve_order(tokens)[:limit]


def detect_question_type(text: str) -> str:
    lowered = normalize_text(text).lower()
    if any(token in lowered for token in ("why", "how", "what caused", "what makes")):
        return "why_how"
    if any(token in lowered for token in ("should", "can", "could", "do we", "is it okay", "is it safe")):
        return "should_can"
    if any(token in lowered for token in ("summary", "summarize", "recap", "what is the mainline")):
        return "summary"
    return "other"


def normalized_event_to_audit_message(event: dict[str, Any]) -> AuditMessage:
    return AuditMessage(
        speaker_id=str(event.get("speaker_id", "") or ""),
        speaker_name=str(event.get("speaker_name", "") or ""),
        content=str(event.get("content", "") or ""),
        role=str(event.get("role", "user") or "user"),
        message_kind=str(event.get("message_kind", "text") or "text"),
        is_at_bot=bool(event.get("is_at_bot", False)),
        is_reply_to_bot=bool(event.get("is_reply_to_bot", False)),
        has_direct_vision=bool(event.get("has_direct_vision", False)),
        is_image_only=bool(event.get("is_image_only", False)),
        reply_target_sender_id=str(event.get("reply_target_sender_id", "") or ""),
        reply_target_sender_name=str(event.get("reply_target_sender_name", "") or ""),
        expects_reply=bool(event.get("expects_reply", False)),
        mainline_anchors=list(event.get("mainline_anchors", []) or []),
        background_terms=list(event.get("background_terms", []) or []),
        expected_phase=str(event.get("expected_phase", "") or ""),
        note=str(event.get("note", "") or ""),
    )


@dataclass(slots=True)
class AuditMessage:
    speaker_id: str
    speaker_name: str
    content: str
    role: str = "user"
    message_kind: str = "text"
    is_at_bot: bool = False
    is_reply_to_bot: bool = False
    has_direct_vision: bool = False
    is_image_only: bool = False
    reply_target_sender_id: str = ""
    reply_target_sender_name: str = ""
    expects_reply: bool = False
    mainline_anchors: list[str] = field(default_factory=list)
    background_terms: list[str] = field(default_factory=list)
    expected_phase: str = ""
    note: str = ""


@dataclass(slots=True)
class ScenarioSpec:
    scenario_id: str
    title: str
    description: str
    messages: list[AuditMessage]
    tags: list[str] = field(default_factory=list)
    difficulty: str = "base"


class ScenarioBuilder:
    def __init__(
        self,
        *,
        scenario_id: str,
        title: str,
        description: str,
        tags: list[str] | None = None,
        difficulty: str = "base",
    ):
        self.scenario_id = scenario_id
        self.title = title
        self.description = description
        self.tags = list(tags or [])
        self.difficulty = difficulty
        self.messages: list[AuditMessage] = []

    def add(self, message: AuditMessage) -> "ScenarioBuilder":
        self.messages.append(message)
        return self

    def extend(self, messages: list[AuditMessage]) -> "ScenarioBuilder":
        self.messages.extend(messages)
        return self

    def build(self) -> ScenarioSpec:
        return ScenarioSpec(
            scenario_id=self.scenario_id,
            title=self.title,
            description=self.description,
            messages=list(self.messages),
            tags=list(self.tags),
            difficulty=self.difficulty,
        )

    @classmethod
    def from_normalized_events(
        cls,
        *,
        scenario_id: str,
        title: str,
        description: str,
        events: list[dict[str, Any]],
        tags: list[str] | None = None,
        difficulty: str = "base",
    ) -> ScenarioSpec:
        builder = cls(
            scenario_id=scenario_id,
            title=title,
            description=description,
            tags=tags,
            difficulty=difficulty,
        )
        for event in events:
            builder.add(normalized_event_to_audit_message(event))
        return builder.build()


@dataclass(slots=True)
class ReplyResult:
    source: str
    model: str
    text: str
    provider_available: bool = True
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReplyAuditRecord:
    scenario_id: str
    turn_index: int
    input_message: str
    reply_source: str
    reply_model: str
    reply_text: str
    rendered_warm_summary: str
    rendered_warm_quotes: str
    rendered_recent_transcript: str
    recent_transcript_included: bool
    post_compaction_recovery_rounds: int
    rendered_cold_summary: str
    reply_prompt_focus_anchor: str
    warm_summary_preview: str
    warm_quotes_preview: str
    warm_topics_preview: str
    recent_transcript_reason: str
    compaction_state: str
    evaluation_count: int
    current_message_count: int
    pending_eval_nodes: list[int]
    force_execute_on_next_safe_hook: bool
    safe_hook_block_reason: str
    final_score: float
    closure_score: float
    tail_activity_score: float
    topic_density_score: float
    stability_score: float
    benefit_score: float
    cold_summary_preview: str
    self_check_passed: bool
    self_check_score: int
    self_check_fail_reasons: list[str]
    self_check_detail: dict[str, Any]
    gap_classification: str
    scenario_expected_phase: str


@dataclass(slots=True)
class CompareInput:
    left_dir: Path
    right_dir: Path


@dataclass(slots=True)
class SummaryOptions:
    failures_only: bool = False


class ReplyProvider:
    source_name = "unknown"

    async def generate_reply(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        metadata: dict[str, Any],
    ) -> ReplyResult:
        raise NotImplementedError


class DeterministicReplyProvider(ReplyProvider):
    source_name = "fake"

    async def generate_reply(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        metadata: dict[str, Any],
    ) -> ReplyResult:
        last_user = normalize_text(str(metadata.get("last_user_message", "") or ""))
        warm_summary = normalize_text(str(metadata.get("warm_summary", "") or ""))
        warm_topics = normalize_text(str(metadata.get("warm_topics", "") or ""))
        anchors = [normalize_text(item) for item in list(metadata.get("mainline_anchors", []) or []) if normalize_text(item)]
        anchor = anchors[0] if anchors else ""
        if not anchor and warm_topics:
            anchor = warm_topics.split(" | ")[0]
        if not anchor and warm_summary:
            anchor = warm_summary.split(";")[0].split(".")[0]
        if not anchor:
            anchor = one_line(last_user, limit=24)
        if any(token in last_user.lower() for token in ("why", "how", "what does")):
            body = (
                f"I am staying on the mainline: we are still talking about {anchor}. "
                "For this turn, the key reason is that the earlier chain has not fully settled yet."
            )
        elif any(token in last_user.lower() for token in ("should", "can", "do we", "is it okay")):
            body = (
                f"I am staying on the mainline: we are still talking about {anchor}. "
                "For this turn, we can continue, but it is better to confirm against the same chain first."
            )
        elif "summary" in last_user.lower() or "summarize" in last_user.lower():
            body = (
                f"Short summary: the mainline is still {anchor}. "
                "The recent progression is clear, but one detail is still open."
            )
        else:
            body = (
                f"I am staying on the mainline: we are still talking about {anchor}. "
                "For this turn, I will continue from the earlier conclusion and avoid drifting into background chatter."
            )
        return ReplyResult(
            source=self.source_name,
            model=model,
            text=body,
            raw={"provider": self.source_name, "anchor": anchor, "message_count": len(messages)},
        )


class OpenAICompatibleReplyProvider(ReplyProvider):
    source_name = "openai-compatible"

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 30.0):
        self.base_url = self._normalize_base_url(base_url)
        self.api_key = str(api_key or "")
        self.timeout_seconds = float(timeout_seconds or 30.0)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        cleaned = str(base_url or "").rstrip("/")
        if not cleaned:
            return ""
        if cleaned.endswith("/v1"):
            return cleaned
        return cleaned + "/v1"

    async def generate_reply(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        metadata: dict[str, Any],
    ) -> ReplyResult:
        if not self.base_url:
            raise RuntimeError("OpenAI-compatible provider requires base_url")
        if not self.api_key:
            raise RuntimeError("OpenAI-compatible provider requires api_key")
        return await asyncio.to_thread(self._generate_reply_sync, messages, model, metadata)

    async def list_models(self) -> list[str]:
        if not self.base_url:
            raise RuntimeError("OpenAI-compatible provider requires base_url")
        if not self.api_key:
            raise RuntimeError("OpenAI-compatible provider requires api_key")
        return await asyncio.to_thread(self._list_models_sync)

    def _generate_reply_sync(
        self,
        messages: list[dict[str, str]],
        model: str,
        metadata: dict[str, Any],
    ) -> ReplyResult:
        import requests

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 180,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            raise RuntimeError(f"relay_unreachable::{exc}") from exc
        raw = response.json()
        if response.status_code >= 400:
            body_preview = one_line(raw, 240)
            if response.status_code == 401:
                raise RuntimeError(f"invalid_api_key::{body_preview}")
            if response.status_code == 404 or "model" in body_preview.lower():
                raise RuntimeError(f"model_not_found::{body_preview}")
            raise RuntimeError(f"provider_error status={response.status_code} body={body_preview}")
        choice = (((raw or {}).get("choices") or [{}])[0] or {}).get("message") or {}
        text = str(choice.get("content", "") or "").strip()
        if not text:
            return ReplyResult(
                source="empty_completion",
                model=model,
                text="",
                provider_available=False,
                error="completion returned empty content",
                raw={"status": response.status_code, "body": raw, "metadata": metadata},
            )
        return ReplyResult(
            source=self.source_name,
            model=model,
            text=text,
            provider_available=True,
            raw={"status": response.status_code, "body": raw, "metadata": metadata},
        )

    def _list_models_sync(self) -> list[str]:
        import requests

        url = f"{self.base_url}/models"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept-Encoding": "identity",
        }
        try:
            response = requests.get(url, headers=headers, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            raise RuntimeError(f"relay_unreachable::{exc}") from exc
        raw = response.json()
        if response.status_code >= 400:
            body_preview = one_line(raw, 240)
            if response.status_code == 401:
                raise RuntimeError(f"invalid_api_key::{body_preview}")
            raise RuntimeError(f"provider_error status={response.status_code} body={body_preview}")
        models: list[str] = []
        for item in list((raw or {}).get("data", []) or []):
            model_id = normalize_text(str((item or {}).get("id", "") or ""))
            if model_id:
                models.append(model_id)
        return models


class _RecentDecisionHelper(PlannerPromptContextMixin):
    pass


class GroupTraceAuditRunner:
    def __init__(
        self,
        *,
        provider: ReplyProvider,
        model: str,
        output_dir: Path,
        scenario_filter: set[str] | None = None,
        tag_filter: set[str] | None = None,
        difficulty_filter: set[str] | None = None,
        max_scenarios: int | None = None,
        summary_only: bool = False,
        failures_only: bool = False,
        seed: int | None = None,
    ):
        self.provider = provider
        self.model = model
        self.output_dir = output_dir
        self.scenario_filter = scenario_filter or set()
        self.tag_filter = tag_filter or set()
        self.difficulty_filter = difficulty_filter or set()
        self.max_scenarios = max_scenarios
        self.summary_only = summary_only
        self.failures_only = failures_only
        self.seed = seed
        self.store = GroupDialogueStore()
        self.compaction = ContextCompactionEngine(self.store)
        self.recent_helper = _RecentDecisionHelper()
        self._timestamp = 1_800_000_000.0
        self._event_counter = 0
        if seed is not None:
            random.seed(seed)

    async def run(self) -> dict[str, Any]:
        scenarios = select_scenarios(
            build_scenarios(),
            scenario_filter=self.scenario_filter,
            tag_filter=self.tag_filter,
            difficulty_filter=self.difficulty_filter,
            max_scenarios=self.max_scenarios,
        )
        trace_records: list[dict[str, Any]] = []
        reply_records: list[dict[str, Any]] = []
        scenario_summaries: list[dict[str, Any]] = []
        for scenario in scenarios:
            records = await self._run_scenario(scenario)
            trace_records.extend(record["trace_status"] for record in records)
            reply_records.extend(record["audit_record"] for record in records)
            scenario_summaries.append(self._build_scenario_summary(scenario, records))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        metrics = build_metrics(scenario_summaries, reply_records)
        if not self.summary_only:
            (self.output_dir / "trace_samples.json").write_text(json_dumps(trace_records), encoding="utf-8")
            with (self.output_dir / "reply_audit.jsonl").open("w", encoding="utf-8") as handle:
                for record in reply_records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        (self.output_dir / "metrics.json").write_text(json_dumps(metrics), encoding="utf-8")
        (self.output_dir / "summary.md").write_text(
            render_summary_markdown(scenario_summaries, SummaryOptions(failures_only=self.failures_only), metrics),
            encoding="utf-8",
        )
        return {
            "output_dir": str(self.output_dir),
            "scenarios": len(scenarios),
            "records": len(reply_records),
            "summary_only": self.summary_only,
        }

    async def _run_scenario(self, scenario: ScenarioSpec) -> list[dict[str, Any]]:
        chat_id = f"{DEFAULT_CHAT_ID_PREFIX}{scenario.scenario_id}"
        records: list[dict[str, Any]] = []
        for turn_index, message in enumerate(scenario.messages, start=1):
            await self._append_message(chat_id, message)
            await self.compaction.schedule_compaction_evaluation(
                chat_id,
                focus_context=None,
                message_source="user" if message.role == "user" else "assistant",
            )
            trace_status = await self.compaction.get_trace_status(chat_id, focus_context=None)
            warm_bundle = await self.store.get_warm_context_bundle(chat_id)
            cold_summary = await self.store.get_cold_summary(chat_id)
            recent_transcript = await self._render_recent_transcript(chat_id)
            include_recent, recent_reason = self.recent_helper._should_include_recent_transcript(
                message.content,
                warm_bundle,
                recent_transcript,
                post_compaction_recovery_rounds=int(trace_status.get("post_compaction_recovery_rounds", 0) or 0),
            )
            rendered_recent_transcript = recent_transcript if include_recent else ""
            focus_anchor = self._choose_focus_anchor(message, warm_bundle)
            reply_result = ReplyResult(source="none", model=self.model, text="", raw={})
            if message.role == "user" and message.expects_reply:
                reply_result = await self._generate_reply(
                    chat_id,
                    scenario,
                    message,
                    warm_bundle=warm_bundle,
                    rendered_recent_transcript=rendered_recent_transcript,
                    focus_anchor=focus_anchor,
                )
                if normalize_text(reply_result.text):
                    assistant_message = AuditMessage(
                        speaker_id="astrmai-bot",
                        speaker_name="AstrMai",
                        content=reply_result.text,
                        role="assistant",
                        message_kind="text",
                    )
                    await self._append_message(chat_id, assistant_message)
                    await self.compaction.schedule_compaction_evaluation(
                        chat_id,
                        focus_context=None,
                        message_source="assistant",
                    )
            trace_status = await self.compaction.get_trace_status(chat_id, focus_context=None)
            warm_bundle = await self.store.get_warm_context_bundle(chat_id)
            cold_summary = await self.store.get_cold_summary(chat_id)
            recent_transcript = await self._render_recent_transcript(chat_id)
            include_recent, recent_reason = self.recent_helper._should_include_recent_transcript(
                message.content,
                warm_bundle,
                recent_transcript,
                post_compaction_recovery_rounds=int(trace_status.get("post_compaction_recovery_rounds", 0) or 0),
            )
            rendered_recent_transcript = recent_transcript if include_recent else ""
            focus_anchor = self._choose_focus_anchor(message, warm_bundle)
            self_check = self._self_check(
                message=message,
                reply_text=reply_result.text,
                warm_summary=warm_bundle.summary_text,
                warm_topics_preview=warm_bundle.topic_preview,
                warm_quotes=warm_bundle.quote_text,
                recent_reason=recent_reason if include_recent else "warm_sufficient",
                trace_status=trace_status,
                rendered_recent_transcript=rendered_recent_transcript,
                focus_anchor=focus_anchor,
            )
            audit_record = ReplyAuditRecord(
                scenario_id=scenario.scenario_id,
                turn_index=turn_index,
                input_message=message.content,
                reply_source=reply_result.source,
                reply_model=reply_result.model,
                reply_text=reply_result.text,
                rendered_warm_summary=warm_bundle.summary_text,
                rendered_warm_quotes=warm_bundle.quote_text,
                rendered_recent_transcript=rendered_recent_transcript,
                recent_transcript_included=include_recent,
                post_compaction_recovery_rounds=int(trace_status.get("post_compaction_recovery_rounds", 0) or 0),
                rendered_cold_summary=cold_summary,
                reply_prompt_focus_anchor=focus_anchor,
                warm_summary_preview=one_line(warm_bundle.summary_text),
                warm_quotes_preview=one_line(warm_bundle.quote_text),
                warm_topics_preview=one_line(warm_bundle.topic_preview),
                recent_transcript_reason=recent_reason if include_recent else "warm_sufficient",
                compaction_state=str(trace_status.get("state", "") or ""),
                evaluation_count=int(trace_status.get("evaluation_count", 0) or 0),
                current_message_count=int(trace_status.get("current_message_count", 0) or 0),
                pending_eval_nodes=list(trace_status.get("pending_eval_nodes", []) or []),
                force_execute_on_next_safe_hook=bool(trace_status.get("force_execute_on_next_safe_hook", False)),
                safe_hook_block_reason=str(trace_status.get("safe_hook_block_reason", "") or ""),
                final_score=float(trace_status.get("final_score", 0.0) or 0.0),
                closure_score=float(trace_status.get("closure_score", 0.0) or 0.0),
                tail_activity_score=float(trace_status.get("tail_activity_score", 0.0) or 0.0),
                topic_density_score=float(trace_status.get("topic_density_score", 0.0) or 0.0),
                stability_score=float(trace_status.get("stability_score", 0.0) or 0.0),
                benefit_score=float(trace_status.get("benefit_score", 0.0) or 0.0),
                cold_summary_preview=one_line(cold_summary),
                self_check_passed=bool(self_check["passed"]),
                self_check_score=int(self_check["score"]),
                self_check_fail_reasons=list(self_check["fail_reasons"]),
                self_check_detail=dict(self_check["detail"]),
                gap_classification=str(self_check.get("gap_classification", "") or ""),
                scenario_expected_phase=message.expected_phase or "",
            )
            records.append(
                {
                    "scenario": asdict(scenario),
                    "input_message": asdict(message),
                    "trace_status": {
                        "scenario_id": scenario.scenario_id,
                        "turn_index": turn_index,
                        "scenario_expected_phase": message.expected_phase or "",
                        "warm_summary_preview": audit_record.warm_summary_preview,
                        "warm_quotes_preview": audit_record.warm_quotes_preview,
                        "warm_topics_preview": audit_record.warm_topics_preview,
                        "rendered_warm_summary": audit_record.rendered_warm_summary,
                        "rendered_warm_quotes": audit_record.rendered_warm_quotes,
                        "rendered_recent_transcript": audit_record.rendered_recent_transcript,
                        "recent_transcript_included": audit_record.recent_transcript_included,
                        "post_compaction_recovery_rounds": audit_record.post_compaction_recovery_rounds,
                        "rendered_cold_summary": audit_record.rendered_cold_summary,
                        "reply_prompt_focus_anchor": audit_record.reply_prompt_focus_anchor,
                        "recent_transcript_reason": audit_record.recent_transcript_reason,
                        "cold_summary_preview": audit_record.cold_summary_preview,
                        "gap_classification": audit_record.gap_classification,
                        "self_check_detail": audit_record.self_check_detail,
                        **trace_status,
                    },
                    "audit_record": asdict(audit_record),
                }
            )
        return records

    async def _append_message(self, chat_id: str, message: AuditMessage) -> DialogueSegment:
        self._event_counter += 1
        self._timestamp += 3.0
        return await self.store.append_segment(
            chat_id,
            event_id=f"{chat_id}-evt-{self._event_counter}",
            speaker_id=message.speaker_id,
            speaker_name=message.speaker_name,
            content=message.content,
            role=message.role,
            message_kind=message.message_kind,
            is_bot=message.role == "assistant",
            reply_target_sender_id=message.reply_target_sender_id,
            reply_target_sender_name=message.reply_target_sender_name,
            is_at_bot=message.is_at_bot,
            is_reply_to_bot=message.is_reply_to_bot,
            has_direct_vision=message.has_direct_vision,
            is_image_only=message.is_image_only,
            timestamp=self._timestamp,
        )

    @staticmethod
    def _choose_focus_anchor(message: AuditMessage, warm_bundle) -> str:
        anchors = [normalize_text(item) for item in list(message.mainline_anchors or []) if normalize_text(item)]
        if anchors:
            return anchors[0]
        warm_topics = str(getattr(warm_bundle, "topic_preview", "") or "").strip()
        if warm_topics:
            return warm_topics.split(" | ", 1)[0]
        warm_summary = str(getattr(warm_bundle, "summary_text", "") or "").strip()
        if warm_summary:
            return warm_summary.splitlines()[0]
        return normalize_text(message.content)

    async def _generate_reply(
        self,
        chat_id: str,
        scenario: ScenarioSpec,
        message: AuditMessage,
        *,
        warm_bundle,
        rendered_recent_transcript: str,
        focus_anchor: str,
    ) -> ReplyResult:
        prompt_messages = [
            {
                "role": "system",
                "content": "You are AstrMai in a group chat. Stay on the active mainline and do not get distracted by background chatter.",
            },
            {
                "role": "user",
                "content": "\n".join(
                    [
                        f"Scenario: {scenario.title}",
                        f"Current input: {message.content}",
                        f"Warm summary: {warm_bundle.summary_text or '(empty)'}",
                        f"Warm quotes: {warm_bundle.quote_text or '(empty)'}",
                        f"Recent transcript: {rendered_recent_transcript or '(omitted)'}",
                        f"Focus anchor: {focus_anchor or '(empty)'}",
                        "Please produce one short reply that stays on the mainline.",
                    ]
                ),
            },
        ]
        metadata = {
            "scenario_id": scenario.scenario_id,
            "last_user_message": message.content,
            "warm_summary": warm_bundle.summary_text,
            "warm_quotes": warm_bundle.quote_text,
            "warm_topics": warm_bundle.topic_preview,
            "recent_transcript": rendered_recent_transcript,
            "focus_anchor": focus_anchor,
            "mainline_anchors": list(message.mainline_anchors or []),
            "background_terms": list(message.background_terms or []),
        }
        try:
            result = await self.provider.generate_reply(prompt_messages, model=self.model, metadata=metadata)
            return result
        except Exception as exc:
            error_text = str(exc)
            error_kind = "reply_source_unavailable"
            if "::" in error_text:
                error_kind = error_text.split("::", 1)[0]
            return ReplyResult(
                source=error_kind,
                model=self.model,
                text="",
                provider_available=False,
                error=error_text,
                raw={"provider": getattr(self.provider, "source_name", "unknown"), "error": error_text, "metadata": metadata},
            )

    async def _render_recent_transcript(self, chat_id: str) -> str:
        snapshot = await self.store.snapshot_compaction_candidates(
            chat_id,
            keep_recent_segments=self.compaction.compaction_keep_recent_segments,
        )
        formatter = getattr(self.store, "_format_segment_line", None)
        lines: list[str] = []
        for segment in list(snapshot.get("recent_segments", []) or []):
            if callable(formatter):
                line = str(formatter(segment) or "").strip()
            else:
                line = self._fallback_render_segment(segment)
            if line:
                lines.append(line)
        return "\n".join(lines[-8:])

    @staticmethod
    def _fallback_render_segment(segment: DialogueSegment) -> str:
        speaker = str(getattr(segment, "speaker_name", "") or getattr(segment, "speaker_id", "") or "unknown")
        content = normalize_text(getattr(segment, "content", ""))
        if not content:
            return ""
        return f"{speaker}: {content}"

    def _self_check(
        self,
        *,
        message: AuditMessage,
        reply_text: str,
        warm_summary: str,
        warm_topics_preview: str,
        warm_quotes: str,
        recent_reason: str,
        trace_status: dict[str, Any],
        rendered_recent_transcript: str,
        focus_anchor: str,
    ) -> dict[str, Any]:
        fail_reasons: list[str] = []
        score = 100
        normalized_reply = normalize_text(reply_text).lower()
        normalized_warm = normalize_text(warm_summary).lower()
        warm_quote_tokens = build_keyword_bucket([warm_quotes], limit=8)
        warm_topic_tokens = build_keyword_bucket([warm_topics_preview, warm_summary], limit=10)
        anchor_tokens = build_keyword_bucket(list(message.mainline_anchors or []), limit=10)
        background_tokens = build_keyword_bucket(list(message.background_terms or []), limit=10)
        mainline_bucket = unique_preserve_order(anchor_tokens + warm_topic_tokens)
        question_type = detect_question_type(message.content)
        detail = {
            "question_type": question_type,
            "matched_mainline_anchors": [],
            "matched_mainline_tokens": [],
            "matched_question_tokens": [],
            "matched_background_terms": [],
            "warm_quote_tokens": warm_quote_tokens,
            "recent_transcript_included": bool(normalize_text(rendered_recent_transcript)),
            "focus_anchor": focus_anchor,
        }
        if message.expects_reply and not normalized_reply:
            fail_reasons.append("missing_reply")
            score -= 40
        anchor_hits = [anchor for anchor in list(message.mainline_anchors or []) if normalize_text(anchor).lower() and normalize_text(anchor).lower() in normalized_reply]
        mainline_token_hits = [token for token in mainline_bucket if token.lower() in normalized_reply]
        detail["matched_mainline_anchors"] = anchor_hits
        detail["matched_mainline_tokens"] = mainline_token_hits
        if message.expects_reply and message.mainline_anchors and not (anchor_hits or mainline_token_hits):
            fail_reasons.append("mainline_anchor_missed")
            score -= 25
        background_hits = [term for term in list(message.background_terms or []) if normalize_text(term).lower() and normalize_text(term).lower() in normalized_reply]
        background_token_hits = [token for token in background_tokens if token.lower() in normalized_reply]
        detail["matched_background_terms"] = unique_preserve_order(background_hits + background_token_hits)
        if message.background_terms:
            if detail["matched_background_terms"] and not (anchor_hits or mainline_token_hits):
                fail_reasons.append("background_distraction")
                score -= 15
        if normalized_reply and normalized_warm:
            warm_tokens = build_keyword_bucket([warm_summary, warm_topics_preview], limit=10)
            if warm_tokens and not any(token in normalized_reply for token in warm_tokens) and not (anchor_hits or mainline_token_hits):
                fail_reasons.append("mainline_context_not_reflected")
                score -= 10
        if int(trace_status.get("post_compaction_recovery_rounds", 0) or 0) > 0 and recent_reason == "warm_sufficient" and message.expects_reply:
            fail_reasons.append("recovery_recent_not_relaxed")
            score -= 10
        question_markers: dict[str, list[str]] = {
            "why_how": ["reason", "because", "due", "caused", "still active", "not settled", "settled", "overlap"],
            "should_can": ["can", "should", "wait", "confirm", "continue", "safe", "defer", "deferred", "until", "pause", "paused", "safe hook"],
            "summary": ["summary", "mainline", "still about", "key point", "conclusion", "focus", "takeaway", "recap", "concluded", "overall", "result", "trade-off", "prioritizes"],
            "other": ["mainline", "continue", "point", "reason"],
        }
        matched_question_tokens = [token for token in question_markers.get(question_type, question_markers["other"]) if token in normalized_reply]
        detail["matched_question_tokens"] = matched_question_tokens
        if message.expects_reply and question_type in {"why_how", "should_can", "summary"}:
            summary_style_answer = (
                question_type == "summary"
                and (anchor_hits or mainline_token_hits)
                and len(normalized_reply.split()) >= 8
            )
            if not matched_question_tokens and not summary_style_answer:
                fail_reasons.append("did_not_address_last_question")
                score -= 20
        prompt_mainline_bucket = build_keyword_bucket(
            [focus_anchor, warm_summary, warm_topics_preview, warm_quotes, rendered_recent_transcript],
            limit=16,
        )
        prompt_context_hits = [token for token in unique_preserve_order(anchor_tokens + mainline_bucket) if token in prompt_mainline_bucket]
        prompt_has_mainline_context = bool(prompt_context_hits)
        gap_classification = ""
        if fail_reasons:
            gap_classification = "model_response_gap" if prompt_has_mainline_context else "context_assembly_gap"
        detail["prompt_mainline_bucket"] = prompt_mainline_bucket
        detail["prompt_context_hits"] = prompt_context_hits
        detail["gap_classification"] = gap_classification
        score = max(0, score)
        return {
            "passed": not fail_reasons,
            "score": score,
            "fail_reasons": fail_reasons,
            "warm_quotes_preview": one_line(warm_quotes),
            "detail": detail,
            "gap_classification": gap_classification,
        }

    @staticmethod
    def _build_scenario_summary(scenario: ScenarioSpec, records: list[dict[str, Any]]) -> dict[str, Any]:
        audit_records = [record["audit_record"] for record in records]
        passed = sum(1 for record in audit_records if bool(record.get("self_check_passed", False)))
        compact_states = [str(record.get("compaction_state", "") or "") for record in audit_records]
        first_state_turns: dict[str, int] = {}
        for record in audit_records:
            state = str(record.get("compaction_state", "") or "")
            if state and state not in first_state_turns:
                first_state_turns[state] = int(record.get("turn_index", 0) or 0)
        failure_cards: list[dict[str, Any]] = []
        for record in audit_records:
            if not bool(record.get("self_check_passed", False)):
                failure_cards.append(
                    {
                        "turn_index": int(record.get("turn_index", 0) or 0),
                        "input_message": str(record.get("input_message", "") or ""),
                        "reply_preview": one_line(str(record.get("reply_text", "") or "")),
                        "warm_summary_preview": str(record.get("warm_summary_preview", "") or ""),
                        "rendered_recent_transcript": str(record.get("rendered_recent_transcript", "") or ""),
                        "reply_prompt_focus_anchor": str(record.get("reply_prompt_focus_anchor", "") or ""),
                        "fail_reasons": list(record.get("self_check_fail_reasons", []) or []),
                        "gap_classification": str(record.get("gap_classification", "") or ""),
                        "self_check_detail": dict(record.get("self_check_detail", {}) or {}),
                    }
                )
        forced_records = [record for record in audit_records if "forced" in [tag.lower() for tag in list(scenario.tags or [])]]
        recovery_records = [record for record in audit_records if "recovery" in [tag.lower() for tag in list(scenario.tags or [])]]
        block_reason_counts: dict[str, int] = {}
        for record in audit_records:
            reason = normalize_text(str(record.get("safe_hook_block_reason", "") or ""))
            if reason:
                block_reason_counts[reason] = int(block_reason_counts.get(reason, 0) or 0) + 1
        return {
            "scenario_id": scenario.scenario_id,
            "title": scenario.title,
            "description": scenario.description,
            "tags": list(scenario.tags or []),
            "difficulty": scenario.difficulty,
            "total_turns": len(records),
            "self_check_passed_turns": passed,
            "self_check_failed_turns": len(records) - passed,
            "states_seen": sorted({state for state in compact_states if state}),
            "state_counts": {state: compact_states.count(state) for state in sorted({state for state in compact_states if state})},
            "first_state_turns": first_state_turns,
            "block_reason_counts": block_reason_counts,
            "last_reply_preview": one_line(str(audit_records[-1].get("reply_text", "") or "")) if audit_records else "",
            "last_fail_reasons": list(audit_records[-1].get("self_check_fail_reasons", []) or []) if audit_records else [],
            "failure_cards": failure_cards[:10],
            "forced_trajectory": [
                {
                    "turn_index": int(record.get("turn_index", 0) or 0),
                    "phase": str(record.get("scenario_expected_phase", "") or ""),
                    "state": str(record.get("compaction_state", "") or ""),
                    "safe_hook_block_reason": str(record.get("safe_hook_block_reason", "") or ""),
                    "force_execute_on_next_safe_hook": bool(record.get("force_execute_on_next_safe_hook", False)),
                }
                for record in forced_records
                if str(record.get("compaction_state", "") or "") in {"FORCED_PENDING", "COOLDOWN"} or bool(record.get("force_execute_on_next_safe_hook", False))
            ],
            "recovery_checks": [
                {
                    "turn_index": int(record.get("turn_index", 0) or 0),
                    "state": str(record.get("compaction_state", "") or ""),
                    "recent_reason": str(record.get("recent_transcript_reason", "") or ""),
                    "reply_preview": one_line(str(record.get("reply_text", "") or "")),
                }
                for record in recovery_records[:4]
            ],
        }


def render_summary_markdown(summaries: list[dict[str, Any]], options: SummaryOptions, metrics: dict[str, Any]) -> str:
    lines = ["# Group Trace Audit Summary", ""]
    lines.extend(
        [
            "## Aggregate",
            "",
            f"- Total scenarios: {metrics.get('total_scenarios', 0)}",
            f"- Total turns: {metrics.get('total_turns', 0)}",
            f"- Passed turns: {metrics.get('passed_turns', 0)}",
            f"- Failed turns: {metrics.get('failed_turns', 0)}",
            f"- Worst WAIT_NEXT_NODE scenario: {metrics.get('worst_wait_next_node_scenario', '(none)')}",
            f"- Earliest COOLDOWN scenario: {metrics.get('earliest_cooldown_scenario', '(none)')}",
            f"- Most common block reason: {metrics.get('most_common_block_reason', '(none)')}",
            f"- Most unstable score bucket: {metrics.get('most_unstable_score_signal', '(none)')}",
            "",
        ]
    )
    for item in summaries:
        if options.failures_only and not item["self_check_failed_turns"] and "FORCED_PENDING" not in item["states_seen"]:
            continue
        lines.extend(
            [
                f"## {item['scenario_id']} - {item['title']}",
                "",
                item["description"],
                "",
                f"- Tags: {', '.join(item['tags']) or '(none)'}",
                f"- Difficulty: {item['difficulty']}",
                f"- Total turns: {item['total_turns']}",
                f"- Self-check passed turns: {item['self_check_passed_turns']}",
                f"- Self-check failed turns: {item['self_check_failed_turns']}",
                f"- States seen: {', '.join(item['states_seen']) or '(none)'}",
                f"- State counts: {json.dumps(item['state_counts'], ensure_ascii=False)}",
                f"- Last reply preview: {item['last_reply_preview'] or '(empty)'}",
                f"- Last fail reasons: {', '.join(item['last_fail_reasons']) or '(none)'}",
                "",
            ]
        )
        if item["first_state_turns"]:
            lines.append("### State Timeline")
            lines.append("")
            for state_name, turn_index in item["first_state_turns"].items():
                lines.append(f"- First `{state_name}`: turn {turn_index}")
            lines.append("")
        if item["failure_cards"]:
            lines.append("### Failure Cards")
            lines.append("")
            for card in item["failure_cards"]:
                lines.append(f"- Turn {card['turn_index']}: {', '.join(card['fail_reasons']) or '(none)'}")
                lines.append(f"  - Input: {one_line(card['input_message'])}")
                lines.append(f"  - Warm: {one_line(card['warm_summary_preview'])}")
                lines.append(f"  - Recent: {one_line(card['rendered_recent_transcript']) or '(omitted)'}")
                lines.append(f"  - Focus anchor: {one_line(card['reply_prompt_focus_anchor']) or '(empty)'}")
                lines.append(f"  - Reply: {card['reply_preview'] or '(empty)'}")
                lines.append(f"  - Gap: {card['gap_classification'] or '(none)'}")
                lines.append(f"  - Detail: {json.dumps(card['self_check_detail'], ensure_ascii=False)}")
            lines.append("")
        if item["forced_trajectory"]:
            lines.append("### Compression Trajectory")
            lines.append("")
            for step in item["forced_trajectory"]:
                lines.append(
                    f"- Turn {step['turn_index']} [{step['phase']}]: state={step['state']}, "
                    f"force_next_safe_hook={step['force_execute_on_next_safe_hook']}, "
                    f"block={step['safe_hook_block_reason'] or '(none)'}"
                )
            lines.append("")
        if item["recovery_checks"]:
            lines.append("### Recovery Snapshot")
            lines.append("")
            for step in item["recovery_checks"]:
                lines.append(
                    f"- Turn {step['turn_index']}: state={step['state']}, recent_reason={step['recent_reason']}, "
                    f"reply={step['reply_preview'] or '(empty)'}"
                )
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_scenarios() -> list[ScenarioSpec]:
    scenarios = [
        ScenarioSpec(
            scenario_id="followup_chain",
            title="Mainline Follow-up Chain",
            description="The user keeps following up on the same mainline after a bot reply.",
            messages=[
                AuditMessage("u1", "Alice", "AstrMai, what does focus tail overlap mean in the compaction timing rules?", is_at_bot=True, expects_reply=True, mainline_anchors=["focus tail overlap", "compaction timing"], expected_phase="active_tail"),
                AuditMessage("u1", "Alice", "Why does focus tail overlap delay compaction timing?", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["focus tail overlap", "compaction timing"], expected_phase="active_tail"),
                AuditMessage("u2", "Bob", "I think the reply chain is still active, so the tail has not settled.", expected_phase="active_tail"),
                AuditMessage("u1", "Alice", "Should the rule wait until the reply chain cools down before compaction?", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["reply chain", "cool down"], expected_phase="active_tail"),
            ],
            tags=["base", "tail-heavy"],
            difficulty="base",
        ),
        ScenarioSpec(
            scenario_id="mainline_with_smalltalk",
            title="Mainline Washed by Smalltalk",
            description="A bot-directed mainline is followed by casual side chatter.",
            messages=[
                AuditMessage("u1", "Alice", "AstrMai, summarize the compaction state machine conclusion from just now.", is_at_bot=True, expects_reply=True, mainline_anchors=["compaction state machine", "conclusion"], expected_phase="active_tail"),
                AuditMessage("u2", "Bob", "What should we eat for lunch?", background_terms=["lunch"], expected_phase="background_fill"),
                AuditMessage("u3", "Carol", "Milk tea sounds good.", background_terms=["milk tea"], expected_phase="background_fill"),
                AuditMessage("u2", "Bob", "Fried chicken also works.", background_terms=["fried chicken"], expected_phase="background_fill"),
                AuditMessage("u1", "Alice", "I do not mean lunch. I mean the compaction state machine point from just now.", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["compaction state machine", "compaction"], background_terms=["lunch", "milk tea", "fried chicken"], expected_phase="active_tail"),
            ],
            tags=["base", "smalltalk"],
            difficulty="base",
        ),
        ScenarioSpec(
            scenario_id="parallel_topics",
            title="Parallel Topics",
            description="Several small threads run in parallel across the recent window.",
            messages=[
                AuditMessage("u1", "Alice", "AstrMai, explain why warm summary should be topic driven.", is_at_bot=True, expects_reply=True, mainline_anchors=["warm summary", "topic driven"], expected_phase="active_tail"),
                AuditMessage("u2", "Bob", "I also want to talk about the cold summary merge path.", background_terms=["cold summary merge"], expected_phase="background_fill"),
                AuditMessage("u3", "Carol", "I care more about recent fallback for another thread.", background_terms=["recent fallback"], expected_phase="background_fill"),
                AuditMessage("u2", "Bob", "Pending eval nodes might also matter for a separate branch.", background_terms=["pending eval nodes"], expected_phase="background_fill"),
                AuditMessage("u1", "Alice", "Please stay with my earlier question: why should warm summary avoid stitching rule lines?", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["warm summary", "rule lines"], background_terms=["cold summary merge", "recent fallback", "pending eval nodes"], expected_phase="active_tail"),
            ],
            tags=["base", "parallel"],
            difficulty="base",
        ),
        build_forced_compaction_scenario(),
        build_post_compaction_recovery_scenario(),
        build_long_tail_drag_scenario(),
        build_unsettled_topic_shift_scenario(),
        build_parallel_multi_user_bot_scenario(),
        build_vision_mixed_context_scenario(),
        build_post_compaction_fast_followup_scenario(),
    ]
    return scenarios


def build_scenario_catalog() -> dict[str, ScenarioSpec]:
    return {scenario.scenario_id: scenario for scenario in build_scenarios()}


def build_forced_compaction_scenario() -> ScenarioSpec:
    messages: list[AuditMessage] = []
    for index in range(1, 61):
        messages.append(
            AuditMessage(
                speaker_id=f"u{(index % 3) + 1}",
                speaker_name=["Alice", "Bob", "Carol"][index % 3],
                content=f"Background progression {index}: routine audit chatter with no bot-directed tail yet.",
                expected_phase="background_fill",
            )
        )
    for index in range(1, 31):
        speaker_id = f"u{((index + 1) % 3) + 1}"
        speaker_name = ["Alice", "Bob", "Carol"][(index + 1) % 3]
        messages.append(
            AuditMessage(
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                content=(
                    f"Active tail {index}: @AstrMai the forced pending chain is still live. "
                    "Should compaction timing wait because the reply chain has not settled yet?"
                ),
                is_at_bot=True,
                is_reply_to_bot=index > 1,
                reply_target_sender_id="astrmai-bot",
                reply_target_sender_name="AstrMai",
                expects_reply=True,
                mainline_anchors=["forced pending", "compaction timing"],
                expected_phase="active_tail",
            )
        )
    for index in range(1, 13):
        messages.append(
            AuditMessage(
                speaker_id="u2" if index % 2 else "u3",
                speaker_name="Bob" if index % 2 else "Carol",
                content=(
                    f"Forced pending extension {index}: @AstrMai still no natural pause. "
                    "Can the same forced pending thread stay deferred until the earliest safe hook?"
                ),
                is_at_bot=True,
                is_reply_to_bot=True,
                reply_target_sender_id="astrmai-bot",
                reply_target_sender_name="AstrMai",
                expects_reply=True,
                mainline_anchors=["forced pending", "natural pause"],
                expected_phase="forced_pending_window",
            )
        )
    messages.extend(
        [
            AuditMessage(
                "u1",
                "Alice",
                "No new question from me. We can pause here and let the pending compaction finish after the thread settles.",
                mainline_anchors=["pause here", "pending compaction"],
                expected_phase="natural_pause",
            ),
            AuditMessage(
                "u2",
                "Bob",
                "Yes, let the thread settle now and allow the earliest safe hook to compact.",
                mainline_anchors=["earliest safe hook", "allow compact"],
                expected_phase="natural_pause",
            ),
        ]
    )
    return ScenarioSpec(
        scenario_id="forced_compaction",
        title="Forced Compaction at 120",
        description="A long chat reaches forced compaction and then waits for the earliest safe hook.",
        messages=messages,
        tags=["base", "forced", "tail-heavy"],
        difficulty="advanced",
    )


def build_post_compaction_recovery_scenario() -> ScenarioSpec:
    messages: list[AuditMessage] = []
    for index in range(1, 122):
        messages.append(
            AuditMessage(
                speaker_id=f"u{(index % 2) + 1}",
                speaker_name="Alice" if index % 2 else "Bob",
                content=f"Recovery setup message {index}: continue the audit conversation and keep the mainline alive.",
                expected_phase="background_fill",
            )
        )
    messages.extend(
        [
            AuditMessage("u1", "Alice", "AstrMai, is the mainline you just compressed still available?", is_at_bot=True, expects_reply=True, mainline_anchors=["compressed", "mainline"], expected_phase="post_compaction_recovery"),
            AuditMessage("u1", "Alice", "I mean the point from just now, not some other topic.", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["just now", "mainline"], expected_phase="post_compaction_recovery"),
        ]
    )
    return ScenarioSpec(
        scenario_id="post_compaction_recovery",
        title="Post-compaction Recovery",
        description="The first two turns after a compaction should still keep the recent mainline clear.",
        messages=messages,
        tags=["base", "recovery"],
        difficulty="base",
    )


def build_long_tail_drag_scenario() -> ScenarioSpec:
    builder = ScenarioBuilder(
        scenario_id="long_tail_drag",
        title="Long Tail Drag",
        description="A long bot-directed tail keeps the active chain alive for many rounds.",
        tags=["tail-heavy", "forced"],
        difficulty="advanced",
    )
    for index in range(1, 36):
        builder.add(
            AuditMessage(
                speaker_id="u1" if index % 2 else "u2",
                speaker_name="Alice" if index % 2 else "Bob",
                content=f"Tail drag {index}: @AstrMai should we still delay compaction while this same chain is live?",
                is_at_bot=True,
                is_reply_to_bot=index > 1,
                reply_target_sender_id="astrmai-bot",
                reply_target_sender_name="AstrMai",
                expects_reply=True,
                mainline_anchors=["delay compaction", "same chain"],
                expected_phase="active_tail",
            )
        )
    return builder.build()


def build_unsettled_topic_shift_scenario() -> ScenarioSpec:
    builder = ScenarioBuilder(
        scenario_id="unsettled_topic_shift",
        title="Unsettled Topic Shift",
        description="A new topic begins before the old mainline has fully closed.",
        tags=["closure", "safe-window"],
        difficulty="advanced",
    )
    builder.extend(
        [
            AuditMessage("u1", "Alice", "AstrMai, why is closure score still low here?", is_at_bot=True, expects_reply=True, mainline_anchors=["closure score"], expected_phase="active_tail"),
            AuditMessage("u1", "Alice", "Should we treat the old reply chain as still open?", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["reply chain", "still open"], expected_phase="active_tail"),
            AuditMessage("u2", "Bob", "Also, I want to switch to safe window timing now.", background_terms=["safe window timing"], expected_phase="background_fill"),
            AuditMessage("u1", "Alice", "Wait, before switching topics, is the previous chain actually settled?", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["previous chain", "settled"], background_terms=["safe window timing"], expected_phase="active_tail"),
        ]
    )
    return builder.build()


def build_parallel_multi_user_bot_scenario() -> ScenarioSpec:
    builder = ScenarioBuilder(
        scenario_id="parallel_multi_user_bot",
        title="Parallel Multi-user Bot Threads",
        description="Several users ask AstrMai different questions in overlapping reply branches.",
        tags=["parallel", "tail-heavy"],
        difficulty="advanced",
    )
    builder.extend(
        [
            AuditMessage("u1", "Alice", "AstrMai, explain topic density score for my branch.", is_at_bot=True, expects_reply=True, mainline_anchors=["topic density score"], expected_phase="active_tail"),
            AuditMessage("u2", "Bob", "AstrMai, can focus tail overlap block compaction in my branch?", is_at_bot=True, expects_reply=True, mainline_anchors=["focus tail overlap"], expected_phase="active_tail"),
            AuditMessage("u3", "Carol", "AstrMai, should pending eval nodes be consumed in order?", is_at_bot=True, expects_reply=True, mainline_anchors=["pending eval nodes"], expected_phase="active_tail"),
            AuditMessage("u1", "Alice", "Please stay with my topic density branch, not Bob's overlap branch.", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["topic density", "my branch"], background_terms=["focus tail overlap", "pending eval nodes"], expected_phase="active_tail"),
        ]
    )
    return builder.build()


def build_vision_mixed_context_scenario() -> ScenarioSpec:
    builder = ScenarioBuilder(
        scenario_id="vision_mixed_context",
        title="Vision Mixed Context",
        description="An image-heavy message sequence still needs warm summary to preserve the mainline.",
        tags=["vision", "mixed"],
        difficulty="advanced",
    )
    builder.extend(
        [
            AuditMessage("u1", "Alice", "AstrMai, can you keep the compaction mainline while I send a screenshot?", is_at_bot=True, expects_reply=True, mainline_anchors=["compaction mainline", "screenshot"], expected_phase="active_tail"),
            AuditMessage("u1", "Alice", "[image only]", message_kind="image", has_direct_vision=True, is_image_only=True, expected_phase="background_fill"),
            AuditMessage("u2", "Bob", "The screenshot is about the same compaction state chart.", has_direct_vision=True, background_terms=["screenshot"], expected_phase="background_fill"),
            AuditMessage("u1", "Alice", "Please answer the earlier compaction mainline question, not just the image note.", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["compaction mainline", "earlier question"], background_terms=["image", "screenshot"], expected_phase="active_tail"),
        ]
    )
    return builder.build()


def build_post_compaction_fast_followup_scenario() -> ScenarioSpec:
    builder = ScenarioBuilder(
        scenario_id="post_compaction_fast_followup",
        title="Post-compaction Fast Follow-up",
        description="A new bot-directed follow-up arrives immediately after compaction recovery starts.",
        tags=["recovery", "tail-heavy"],
        difficulty="advanced",
    )
    for index in range(1, 122):
        builder.add(
            AuditMessage(
                speaker_id="u1" if index % 2 else "u2",
                speaker_name="Alice" if index % 2 else "Bob",
                content=f"Fast follow-up setup {index}: keep the audit chain warm and continuous.",
                expected_phase="background_fill",
            )
        )
    builder.extend(
        [
            AuditMessage("u1", "Alice", "AstrMai, right after compaction, can you still follow the exact mainline?", is_at_bot=True, expects_reply=True, mainline_anchors=["right after compaction", "exact mainline"], expected_phase="post_compaction_recovery"),
            AuditMessage("u1", "Alice", "And can you continue immediately without drifting?", is_reply_to_bot=True, reply_target_sender_id="astrmai-bot", reply_target_sender_name="AstrMai", expects_reply=True, mainline_anchors=["continue immediately", "without drifting"], expected_phase="post_compaction_recovery"),
        ]
    )
    return builder.build()


def build_provider(args: argparse.Namespace) -> ReplyProvider:
    mode = str(args.provider_mode or "auto").strip().lower()
    if mode == "fake":
        return DeterministicReplyProvider()
    if mode == "openai":
        return OpenAICompatibleReplyProvider(
            base_url=str(args.base_url or ""),
            api_key=str(args.api_key or ""),
        )
    if mode == "auto":
        if str(args.base_url or "").strip() and str(args.api_key or "").strip():
            return OpenAICompatibleReplyProvider(
                base_url=str(args.base_url or ""),
                api_key=str(args.api_key or ""),
            )
        return DeterministicReplyProvider()
    raise SystemExit(f"Unsupported provider mode: {args.provider_mode}")


def select_scenarios(
    scenarios: list[ScenarioSpec],
    *,
    scenario_filter: set[str],
    tag_filter: set[str],
    difficulty_filter: set[str],
    max_scenarios: int | None,
) -> list[ScenarioSpec]:
    normalized_tags = {normalize_text(tag).lower() for tag in tag_filter if normalize_text(tag)}
    normalized_difficulties = {normalize_text(item).lower() for item in difficulty_filter if normalize_text(item)}
    selected: list[ScenarioSpec] = []
    for scenario in scenarios:
        if scenario_filter and scenario.scenario_id not in scenario_filter:
            continue
        if normalized_tags and not any(normalize_text(tag).lower() in normalized_tags for tag in list(scenario.tags or [])):
            continue
        if normalized_difficulties and normalize_text(scenario.difficulty).lower() not in normalized_difficulties:
            continue
        selected.append(scenario)
    if max_scenarios is not None and max_scenarios >= 0:
        return selected[:max_scenarios]
    return selected


def build_metrics(summaries: list[dict[str, Any]], reply_records: list[dict[str, Any]]) -> dict[str, Any]:
    total_turns = sum(int(item.get("total_turns", 0) or 0) for item in summaries)
    passed_turns = sum(int(item.get("self_check_passed_turns", 0) or 0) for item in summaries)
    failed_turns = total_turns - passed_turns
    scenario_pass_rates = {
        str(item.get("scenario_id", "") or ""): round(
            (int(item.get("self_check_passed_turns", 0) or 0) / int(item.get("total_turns", 0) or 1)),
            4,
        )
        for item in summaries
        if str(item.get("scenario_id", "") or "")
    }
    scenario_state_counts = {
        str(item.get("scenario_id", "") or ""): dict(item.get("state_counts", {}) or {})
        for item in summaries
        if str(item.get("scenario_id", "") or "")
    }
    scenario_first_state_turns = {
        str(item.get("scenario_id", "") or ""): dict(item.get("first_state_turns", {}) or {})
        for item in summaries
        if str(item.get("scenario_id", "") or "")
    }
    scenario_block_reason_counts = {
        str(item.get("scenario_id", "") or ""): dict(item.get("block_reason_counts", {}) or {})
        for item in summaries
        if str(item.get("scenario_id", "") or "")
    }
    wait_candidates = [item for item in summaries if int(item.get("state_counts", {}).get("WAIT_NEXT_NODE", 0) or 0) > 0]
    wait_scenario = max(wait_candidates, key=lambda item: int(item.get("state_counts", {}).get("WAIT_NEXT_NODE", 0) or 0), default={})
    cooldown_candidates = [item for item in summaries if "COOLDOWN" in item.get("first_state_turns", {})]
    earliest_cooldown = min(cooldown_candidates, key=lambda item: int(item.get("first_state_turns", {}).get("COOLDOWN", 10**9) or 10**9), default={})
    block_reason_counts: dict[str, int] = {}
    signal_names = ["closure_score", "tail_activity_score", "topic_density_score", "stability_score", "benefit_score"]
    evaluated_states = {"WAIT_NEXT_NODE", "FORCED_PENDING", "COOLDOWN", "COMPACT_NOW", "DEFERRED_FOR_STABILITY"}
    evaluated_records = [
        record
        for record in reply_records
        if int(record.get("evaluation_count", 0) or 0) >= 80 or str(record.get("compaction_state", "") or "") in evaluated_states
    ]
    signal_samples: dict[str, list[float]] = {name: [] for name in signal_names}
    for record in reply_records:
        reason = normalize_text(str(record.get("safe_hook_block_reason", "") or ""))
        if reason:
            block_reason_counts[reason] = int(block_reason_counts.get(reason, 0) or 0) + 1
    for record in evaluated_records:
        for key in signal_names:
            signal_samples[key].append(float(record.get(key, 0.0) or 0.0))
    most_common_block_reason = max(block_reason_counts.items(), key=lambda item: item[1], default=("(none)", 0))[0]
    averages = {
        key: (sum(values) / len(values) if values else 0.0)
        for key, values in signal_samples.items()
    }
    ranges = {
        key: ((max(values) - min(values)) if values else 0.0)
        for key, values in signal_samples.items()
    }
    stddevs = {
        key: (statistics.pstdev(values) if len(values) >= 2 else 0.0)
        for key, values in signal_samples.items()
    }
    most_unstable_score_signal = max(stddevs.items(), key=lambda item: item[1], default=("(none)", 0.0))[0] if evaluated_records else "(none)"
    return {
        "total_scenarios": len(summaries),
        "total_turns": total_turns,
        "passed_turns": passed_turns,
        "failed_turns": failed_turns,
        "pass_rate": round((passed_turns / total_turns), 4) if total_turns else 0.0,
        "worst_wait_next_node_scenario": wait_scenario.get("scenario_id", "(none)") if wait_candidates else "(none)",
        "earliest_cooldown_scenario": earliest_cooldown.get("scenario_id", "(none)"),
        "most_common_block_reason": most_common_block_reason,
        "most_unstable_score_signal": most_unstable_score_signal,
        "scenario_pass_rates": scenario_pass_rates,
        "scenario_state_counts": scenario_state_counts,
        "scenario_first_state_turns": scenario_first_state_turns,
        "scenario_block_reason_counts": scenario_block_reason_counts,
        "evaluated_turn_count": len(evaluated_records),
        "block_reason_counts": block_reason_counts,
        "score_averages": averages,
        "score_ranges": ranges,
        "score_stddevs": stddevs,
    }


def load_artifact_metrics(artifact_dir: Path) -> dict[str, Any]:
    metrics_path = artifact_dir / "metrics.json"
    if metrics_path.exists():
        loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
        if "scenario_pass_rates" in loaded and "score_stddevs" in loaded:
            return loaded
    reply_path = artifact_dir / "reply_audit.jsonl"
    if not reply_path.exists():
        raise FileNotFoundError(f"No metrics.json or reply_audit.jsonl found in {artifact_dir}")
    reply_records = [json.loads(line) for line in reply_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    scenario_map: dict[str, dict[str, Any]] = {}
    for record in reply_records:
        scenario_id = str(record.get("scenario_id", "") or "")
        item = scenario_map.setdefault(
            scenario_id,
            {
                "scenario_id": scenario_id,
                "total_turns": 0,
                "self_check_passed_turns": 0,
                "state_counts": {},
                "first_state_turns": {},
            },
        )
        item["total_turns"] += 1
        if record.get("self_check_passed", False):
            item["self_check_passed_turns"] += 1
        state = str(record.get("compaction_state", "") or "")
        if state:
            item["state_counts"][state] = int(item["state_counts"].get(state, 0) or 0) + 1
            item["first_state_turns"].setdefault(state, int(record.get("turn_index", 0) or 0))
    return build_metrics(list(scenario_map.values()), reply_records)


def render_compare_markdown(compare_input: CompareInput) -> str:
    left_metrics = load_artifact_metrics(compare_input.left_dir)
    right_metrics = load_artifact_metrics(compare_input.right_dir)
    left_pass = dict(left_metrics.get("scenario_pass_rates", {}) or {})
    right_pass = dict(right_metrics.get("scenario_pass_rates", {}) or {})
    left_states = dict(left_metrics.get("scenario_state_counts", {}) or {})
    right_states = dict(right_metrics.get("scenario_state_counts", {}) or {})
    left_first = dict(left_metrics.get("scenario_first_state_turns", {}) or {})
    right_first = dict(right_metrics.get("scenario_first_state_turns", {}) or {})
    scenario_ids = sorted(set(left_pass) | set(right_pass) | set(left_states) | set(right_states))
    degraded: list[str] = []
    wait_increase: list[str] = []
    forced_shift: list[str] = []
    lines = [
        "# Group Trace Audit Compare",
        "",
        f"- Left: {compare_input.left_dir}",
        f"- Right: {compare_input.right_dir}",
        "",
        f"- Left pass rate: {left_metrics.get('pass_rate', 0.0)}",
        f"- Right pass rate: {right_metrics.get('pass_rate', 0.0)}",
        f"- Left worst WAIT_NEXT_NODE scenario: {left_metrics.get('worst_wait_next_node_scenario', '(none)')}",
        f"- Right worst WAIT_NEXT_NODE scenario: {right_metrics.get('worst_wait_next_node_scenario', '(none)')}",
        f"- Left most common block reason: {left_metrics.get('most_common_block_reason', '(none)')}",
        f"- Right most common block reason: {right_metrics.get('most_common_block_reason', '(none)')}",
        "",
    ]
    lines.extend(["## Scenario Comparison", ""])
    for scenario_id in scenario_ids:
        left_rate = float(left_pass.get(scenario_id, 0.0) or 0.0)
        right_rate = float(right_pass.get(scenario_id, 0.0) or 0.0)
        left_wait = int((left_states.get(scenario_id, {}) or {}).get("WAIT_NEXT_NODE", 0) or 0)
        right_wait = int((right_states.get(scenario_id, {}) or {}).get("WAIT_NEXT_NODE", 0) or 0)
        left_forced = int((left_states.get(scenario_id, {}) or {}).get("FORCED_PENDING", 0) or 0)
        right_forced = int((right_states.get(scenario_id, {}) or {}).get("FORCED_PENDING", 0) or 0)
        left_cool = int((left_states.get(scenario_id, {}) or {}).get("COOLDOWN", 0) or 0)
        right_cool = int((right_states.get(scenario_id, {}) or {}).get("COOLDOWN", 0) or 0)
        left_forced_first = (left_first.get(scenario_id, {}) or {}).get("FORCED_PENDING")
        right_forced_first = (right_first.get(scenario_id, {}) or {}).get("FORCED_PENDING")
        if right_rate < left_rate:
            degraded.append(f"{scenario_id}: {left_rate} -> {right_rate}")
        if right_wait > left_wait:
            wait_increase.append(f"{scenario_id}: {left_wait} -> {right_wait}")
        if left_forced_first != right_forced_first and (left_forced_first is not None or right_forced_first is not None):
            forced_shift.append(f"{scenario_id}: {left_forced_first} -> {right_forced_first}")
        lines.extend(
            [
                f"### {scenario_id}",
                f"- Pass rate: {left_rate} -> {right_rate}",
                f"- States: {json.dumps(left_states.get(scenario_id, {}), ensure_ascii=False)} -> {json.dumps(right_states.get(scenario_id, {}), ensure_ascii=False)}",
                f"- WAIT/FORCED/COOLDOWN: {left_wait}/{left_forced}/{left_cool} -> {right_wait}/{right_forced}/{right_cool}",
                f"- First FORCED_PENDING: {left_forced_first if left_forced_first is not None else '(none)'} -> {right_forced_first if right_forced_first is not None else '(none)'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Regressions",
            "",
            f"- Lower pass rate: {', '.join(degraded) if degraded else '(none)'}",
            f"- Higher WAIT_NEXT_NODE: {', '.join(wait_increase) if wait_increase else '(none)'}",
            f"- First FORCED_PENDING shifted: {', '.join(forced_shift) if forced_shift else '(none)'}",
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def choose_preferred_model(models: list[str]) -> str:
    cleaned = [normalize_text(item) for item in models if normalize_text(item)]
    if not cleaned:
        return DEFAULT_MODEL
    if "LongCat-Flash-Chat" in cleaned:
        return "LongCat-Flash-Chat"

    def is_non_thinking(name: str) -> bool:
        lowered = name.lower()
        return "thinking" not in lowered and "reason" not in lowered

    flash_or_chat = [name for name in cleaned if is_non_thinking(name) and ("flash" in name.lower() or "chat" in name.lower())]
    if flash_or_chat:
        return flash_or_chat[0]
    instruct = [name for name in cleaned if is_non_thinking(name) and "instruct" in name.lower()]
    if instruct:
        return instruct[0]
    return cleaned[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run manual group trace audit scenarios.")
    parser.add_argument("--provider-mode", default="fake", choices=["fake", "openai", "auto"])
    parser.add_argument("--base-url", default=os.getenv("GROUP_TRACE_AUDIT_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("GROUP_TRACE_AUDIT_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("GROUP_TRACE_AUDIT_MODEL", ""))
    parser.add_argument("--scenario", action="append", default=[], help="Scenario id to run. Can be specified multiple times.")
    parser.add_argument("--tag", action="append", default=[], help="Scenario tag to run. Can be specified multiple times.")
    parser.add_argument("--difficulty", action="append", default=[], help="Scenario difficulty to run. Can be specified multiple times.")
    parser.add_argument("--list-scenarios", action="store_true", help="List available scenarios and exit.")
    parser.add_argument("--summary-only", action="store_true", help="Only write summary and metrics artifacts.")
    parser.add_argument("--failures-only", action="store_true", help="Only show failures and key states in summary.md.")
    parser.add_argument("--max-scenarios", type=int, default=None, help="Limit the number of scenarios to execute.")
    parser.add_argument("--seed", type=int, default=None, help="Reserved seed for future scenario variants.")
    parser.add_argument("--compare-dir", nargs=2, default=[], metavar=("OLD_DIR", "NEW_DIR"), help="Compare two artifact directories and exit.")
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    if args.list_scenarios:
        catalog = build_scenario_catalog()
        print(
            json_dumps(
                [
                    {
                        "scenario_id": item.scenario_id,
                        "title": item.title,
                        "difficulty": item.difficulty,
                        "tags": item.tags,
                        "turns": len(item.messages),
                    }
                    for item in catalog.values()
                ]
            )
        )
        return 0
    if args.compare_dir:
        print(render_compare_markdown(CompareInput(Path(args.compare_dir[0]), Path(args.compare_dir[1]))))
        return 0
    provider = build_provider(args)
    selected_model = str(args.model or "").strip()
    if not selected_model:
        if isinstance(provider, OpenAICompatibleReplyProvider):
            try:
                selected_model = choose_preferred_model(await provider.list_models())
            except Exception:
                selected_model = DEFAULT_MODEL
        else:
            selected_model = DEFAULT_MODEL
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "artifacts" / "group_trace_audit" / timestamp
    runner = GroupTraceAuditRunner(
        provider=provider,
        model=selected_model,
        output_dir=output_dir,
        scenario_filter={item for item in list(args.scenario or []) if item},
        tag_filter={item for item in list(args.tag or []) if item},
        difficulty_filter={item for item in list(args.difficulty or []) if item},
        max_scenarios=args.max_scenarios,
        summary_only=bool(args.summary_only),
        failures_only=bool(args.failures_only),
        seed=args.seed,
    )
    result = await runner.run()
    print(json_dumps(result))
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
