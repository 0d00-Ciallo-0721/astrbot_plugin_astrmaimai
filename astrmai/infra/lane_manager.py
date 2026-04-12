import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from .output_guard import is_safe_visible_text, sanitize_visible_reply_text
from .runtime_contracts import SocialTranscriptTurn, VisibleReplyArtifact


@dataclass(frozen=True)
class LaneKey:
    subsystem: str
    task_family: str
    scope_id: str
    prompt_version: str = "v1"
    scope_kind: str = "chat"

    def as_suffix(self) -> str:
        return f"{self.subsystem}:{self.task_family}:{self.prompt_version}"

    def as_log_key(self) -> str:
        return f"{self.scope_kind}:{self.scope_id}:{self.as_suffix()}"


@dataclass(frozen=True)
class LanePolicy:
    store_mode: str
    max_raw_turns: int
    summarize_threshold_tokens: int = 0
    ttl_seconds: int = 86400


class LaneManager:
    """
    AstrMai lane 会话编排层。
    历史真源始终落在 AstrBot ConversationManager，自身只负责：
    1. lane UMO 映射
    2. 历史裁剪与清洗策略
    3. 运行时 rotation 元信息
    """

    DEFAULT_POLICIES: Dict[tuple[str, str], LanePolicy] = {
        ("sys1", "judge"): LanePolicy(store_mode="structured", max_raw_turns=6),
        ("sys1", "mood"): LanePolicy(store_mode="structured", max_raw_turns=6),
        ("sys1", "vision"): LanePolicy(store_mode="structured", max_raw_turns=2),
        ("sys2", "dialog"): LanePolicy(store_mode="full", max_raw_turns=12),
        ("sys2", "followup"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "goal"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "expression"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "persona"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys2", "retrieval"): LanePolicy(store_mode="structured", max_raw_turns=4),
        ("sys3", "direct"): LanePolicy(store_mode="full", max_raw_turns=8),
        ("bg", "memory"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "dream"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "reflect"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "proactive"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
        ("bg", "profile"): LanePolicy(store_mode="summary_only", max_raw_turns=3),
    }

    def __init__(self, conversation_manager: Any, config: Any = None):
        self.conversation_manager = conversation_manager
        self.config = config
        self._runtime_meta: Dict[str, Dict[str, Any]] = {}
        self._remote_sessions: Dict[str, str] = {}
        self._lane_locks: Dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    def get_policy(self, lane_key: LaneKey) -> LanePolicy:
        return self.DEFAULT_POLICIES.get(
            (lane_key.subsystem, lane_key.task_family),
            LanePolicy(store_mode="structured", max_raw_turns=6),
        )

    def resolve_lane_umo(self, base_origin: Optional[str], lane_key: LaneKey) -> str:
        if base_origin:
            root = base_origin
        else:
            root = f"astrmai_bg:OtherMessage:{lane_key.scope_id}"
        return f"{root}@@astrmai:{lane_key.as_suffix()}"

    async def _get_lane_lock(self, lane_umo: str) -> asyncio.Lock:
        async with self._lock:
            if lane_umo not in self._lane_locks:
                self._lane_locks[lane_umo] = asyncio.Lock()
            return self._lane_locks[lane_umo]

    def _should_rotate(
        self,
        lane_umo: str,
        prompt_version: str,
        prefix_hash: str,
        model_id: str,
        persona_id: str,
    ) -> bool:
        meta = self._runtime_meta.get(lane_umo)
        if not meta:
            return False
        return any(
            [
                meta.get("prompt_version") != prompt_version,
                meta.get("prefix_hash") != prefix_hash,
                meta.get("persona_id") != persona_id,
            ]
        )

    def _build_title(self, lane_key: LaneKey) -> str:
        return f"AstrMai {lane_key.subsystem}/{lane_key.task_family}"

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            fragments: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if text:
                        fragments.append(str(text))
            return " ".join(fragment for fragment in fragments if fragment).strip()
        if isinstance(content, dict):
            return str(content.get("text") or content.get("content") or "").strip()
        return str(content).strip()

    def _build_rolling_summary(self, history: List[dict]) -> str:
        summary_lines: List[str] = []
        for message in history:
            role = str(message.get("role", "assistant")).strip() or "assistant"
            content = self._stringify_content(message.get("content", ""))
            if not content:
                continue
            content = re.sub(r"\s+", " ", content)
            summary_lines.append(f"{role}: {content[:120]}")
            if len(summary_lines) >= 8:
                break
        if not summary_lines:
            return "较早对话摘要：暂无可用内容。"
        return "较早对话摘要：\n" + "\n".join(summary_lines)

    def _extract_dialogue_from_meta_prompt(self, content: str) -> str:
        text = self._stringify_content(content)
        if not text:
            return ""
        patterns = [
            r"这是当前你看到的最新消息[:：]?\s*(.+?)(?:\n\n>>|\)$)",
            r"当前你看到的最新消息[:：]?\s*(.+?)(?:\n\n>>|\)$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                extracted = match.group(1).strip()
                if extracted:
                    return extracted
        if any(marker in text for marker in ("导演旁白", "动作提示", "请仔细阅读设定和前面的剧本")):
            return ""
        return text

    def _sanitize_dialog_message(self, message: dict) -> Optional[dict]:
        role = str(message.get("role", "")).strip()
        content = message.get("content", "")
        if role != "user":
            normalized = sanitize_visible_reply_text(self._stringify_content(content), fallback_text="")
            if not normalized:
                return None
            return {"role": role, "content": normalized}
        cleaned = self._extract_dialogue_from_meta_prompt(content)
        if cleaned and not is_safe_visible_text(cleaned):
            return None
        if not cleaned:
            return None
        return {"role": role, "content": cleaned}

    @staticmethod
    def _looks_like_social_rendered_line(content: str) -> bool:
        normalized = str(content or "").strip()
        if not normalized:
            return False
        return (
            normalized.startswith("[")
            or "说:" in normalized
            or "说：" in normalized
            or "发了一张" in normalized
            or "刚刚" in normalized
            or "戳了戳" in normalized
        )

    def build_history_turn(self, role: str, content: Any) -> Optional[dict]:
        normalized_role = str(role or "").strip()
        if normalized_role == "assistant":
            sanitized = sanitize_visible_reply_text(self._stringify_content(content), fallback_text="")
            if not sanitized:
                return None
            return {"role": normalized_role, "content": sanitized}
        if normalized_role == "user":
            sanitized = self._extract_dialogue_from_meta_prompt(content)
            if not sanitized or not is_safe_visible_text(sanitized):
                return None
            return {"role": normalized_role, "content": sanitized}
        sanitized = self._stringify_content(content)
        if not sanitized:
            return None
        return {"role": normalized_role, "content": sanitized}

    @staticmethod
    def _render_social_transcript_turn(turn: SocialTranscriptTurn, bot_name: str) -> str:
        if turn.turn_type == "assistant":
            speaker = turn.speaker_name or bot_name
            return f"{speaker}: {turn.content[:180]}"
        if turn.content.startswith("["):
            return turn.content[:180]
        speaker = turn.speaker_name or "用户"
        if turn.target_name:
            return f"{speaker}对{turn.target_name}说: {turn.content[:180]}"
        return f"{speaker}: {turn.content[:180]}"

    def _sanitize_dialog_history(self, history: List[dict]) -> tuple[List[dict], bool]:
        sanitized: List[dict] = []
        changed = False
        for message in history:
            if not isinstance(message, dict):
                changed = True
                continue
            normalized = self._sanitize_dialog_message(message)
            if normalized is None:
                changed = True
                continue
            if normalized != message:
                changed = True
            sanitized.append(normalized)
        return sanitized, changed

    def _compact_history(self, normalized: List[dict], lane_key: LaneKey, policy: LanePolicy) -> List[dict]:
        if not normalized:
            return normalized

        if policy.store_mode == "summary_only":
            kept = normalized[-max(policy.max_raw_turns, 1):]
            if len(normalized) > len(kept):
                summary = {"role": "assistant", "content": self._build_rolling_summary(normalized[:-len(kept)])}
                return [summary, *kept][-(policy.max_raw_turns + 1):]
            return kept

        if (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
            max_messages = max(policy.max_raw_turns * 2, 4)
            if len(normalized) <= max_messages:
                return normalized[-max_messages:]
            keep_recent = min(max(policy.max_raw_turns, 4), len(normalized))
            recent_messages = normalized[-keep_recent:]
            older_messages = normalized[:-keep_recent]
            summary = {"role": "assistant", "content": self._build_rolling_summary(older_messages)}
            return [summary, *recent_messages]

        max_messages = max(policy.max_raw_turns, 1)
        if policy.store_mode == "full":
            max_messages *= 2
        return normalized[-max_messages:]

    def _normalize_history(self, history: List[dict], lane_key: LaneKey) -> List[dict]:
        policy = self.get_policy(lane_key)
        normalized: List[dict] = []
        for message in history:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).strip()
            if role == "system":
                continue
            normalized.append(dict(message))
        if (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
            normalized, _ = self._sanitize_dialog_history(normalized)
        return self._compact_history(normalized, lane_key, policy)

    def _load_history(self, conversation: Any) -> List[dict]:
        if not conversation or not getattr(conversation, "history", None):
            return []
        raw_history = conversation.history
        if isinstance(raw_history, str):
            try:
                parsed = json.loads(raw_history)
            except json.JSONDecodeError:
                logger.warning("[LaneManager] Failed to parse lane history JSON; fallback to empty history.")
                return []
        else:
            parsed = raw_history
        if not isinstance(parsed, list):
            return []
        return [dict(item) for item in parsed if isinstance(item, dict)]

    async def ensure_lane(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
    ) -> tuple[str, str, List[dict], LanePolicy]:
        lane_umo = self.resolve_lane_umo(base_origin, lane_key)
        lane_lock = await self._get_lane_lock(lane_umo)
        async with lane_lock:
            conversation_id = await self.conversation_manager.get_curr_conversation_id(lane_umo)
            rotate = False
            old_history: List[dict] = []
            if conversation_id:
                try:
                    old_conversation = await self.conversation_manager.get_conversation(
                        lane_umo,
                        conversation_id,
                        create_if_not_exists=False,
                    )
                    old_history = self._load_history(old_conversation)
                except Exception:
                    old_history = []
                rotate = self._should_rotate(
                    lane_umo=lane_umo,
                    prompt_version=lane_key.prompt_version,
                    prefix_hash=prefix_hash,
                    model_id=model_id,
                    persona_id=persona_id,
                )

            if not conversation_id or rotate:
                conversation_id = await self.conversation_manager.new_conversation(
                    unified_msg_origin=lane_umo,
                    title=self._build_title(lane_key),
                    persona_id=persona_id or None,
                )
                if rotate and old_history and (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
                    await self.save_lane_history(
                        lane_key=lane_key,
                        lane_umo=lane_umo,
                        conversation_id=conversation_id,
                        history=[{"role": "assistant", "content": self._build_rolling_summary(old_history)}],
                        prefix_hash=prefix_hash,
                        model_id=model_id,
                        persona_id=persona_id,
                    )

            conversation = await self.conversation_manager.get_conversation(
                lane_umo,
                conversation_id,
                create_if_not_exists=True,
            )
            loaded_history = self._load_history(conversation)
            history = self._normalize_history(loaded_history, lane_key)
            if loaded_history != history and (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
                await self.conversation_manager.update_conversation(
                    unified_msg_origin=lane_umo,
                    conversation_id=conversation_id,
                    history=history,
                    title=self._build_title(lane_key),
                    persona_id=persona_id or None,
                    token_usage=None,
                )

            self._runtime_meta[lane_umo] = {
                "conversation_id": conversation_id,
                "prompt_version": lane_key.prompt_version,
                "prefix_hash": prefix_hash,
                "model_id": model_id,
                "persona_id": persona_id,
            }
            return lane_umo, conversation_id, history, self.get_policy(lane_key)

    async def save_lane_history(
        self,
        lane_key: LaneKey,
        lane_umo: str,
        conversation_id: str,
        history: List[dict],
        token_usage: Optional[int] = None,
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
    ) -> List[dict]:
        normalized = self._normalize_history(history, lane_key)
        await self.conversation_manager.update_conversation(
            unified_msg_origin=lane_umo,
            conversation_id=conversation_id,
            history=normalized,
            title=self._build_title(lane_key),
            persona_id=persona_id or None,
            token_usage=token_usage,
        )
        self._runtime_meta[lane_umo] = {
            "conversation_id": conversation_id,
            "prompt_version": lane_key.prompt_version,
            "prefix_hash": prefix_hash,
            "model_id": model_id,
            "persona_id": persona_id,
        }
        return normalized

    def get_remote_session_id(self, lane_umo: str, provider_family: str) -> str:
        key = f"{provider_family}:{lane_umo}"
        if key not in self._remote_sessions:
            self._remote_sessions[key] = lane_umo
        return self._remote_sessions[key]

    async def append_exchange(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        user_content: Any,
        assistant_content: Any,
        token_usage: Optional[int] = None,
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
    ) -> List[dict]:
        lane_umo, conversation_id, history, _ = await self.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
            prefix_hash=prefix_hash,
            model_id=model_id,
            persona_id=persona_id,
        )
        user_turn = self.build_history_turn("user", user_content)
        assistant_turn = self.build_history_turn("assistant", assistant_content)
        if user_turn:
            history.append(user_turn)
        if assistant_turn:
            history.append(assistant_turn)
        return await self.save_lane_history(
            lane_key=lane_key,
            lane_umo=lane_umo,
            conversation_id=conversation_id,
            history=history,
            token_usage=token_usage,
            prefix_hash=prefix_hash,
            model_id=model_id,
            persona_id=persona_id,
        )

    async def append_visible_reply_artifact(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        raw_user_text: Any,
        artifact: VisibleReplyArtifact,
        token_usage: Optional[int] = None,
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
    ) -> List[dict]:
        if artifact.blocked or not artifact.persistable_text:
            lane_umo, conversation_id, history, _ = await self.ensure_lane(
                lane_key=lane_key,
                base_origin=base_origin,
                prefix_hash=prefix_hash,
                model_id=model_id,
                persona_id=persona_id,
            )
            return history
        return await self.append_exchange(
            lane_key=lane_key,
            base_origin=base_origin,
            user_content=raw_user_text,
            assistant_content=artifact.persistable_text,
            token_usage=token_usage,
            prefix_hash=prefix_hash,
            model_id=model_id,
            persona_id=persona_id,
        )

    async def get_lane_history(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
    ) -> List[dict]:
        _lane_umo, _conversation_id, history, _ = await self.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
        )
        return list(history)

    async def get_recent_transcript(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        max_turns: int = 4,
    ) -> str:
        lane_umo, _conversation_id, history, _ = await self.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
        )
        if not history:
            return ""

        recent_messages = history[-max(max_turns * 2, 2):]
        bot_name = "Bot"
        if getattr(getattr(self.config, "system1", None), "nicknames", None):
            nicknames = getattr(self.config.system1, "nicknames", [])
            if isinstance(nicknames, list) and nicknames:
                bot_name = str(nicknames[0]).strip() or bot_name

        lines: List[str] = []
        for message in recent_messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).strip()
            content = self._stringify_content(message.get("content", ""))
            if not content:
                continue
            if role == "assistant":
                normalized_assistant = sanitize_visible_reply_text(content, fallback_text="")
                if not normalized_assistant:
                    continue
                lines.append(
                    self._render_social_transcript_turn(
                        SocialTranscriptTurn(speaker_name=bot_name, turn_type="assistant", content=normalized_assistant),
                        bot_name,
                    )
                )
                continue
            if role == "user" and self._looks_like_social_rendered_line(content):
                lines.append(
                    self._render_social_transcript_turn(
                        SocialTranscriptTurn(speaker_name="", turn_type="social_event", content=content),
                        bot_name,
                    )
                )
                continue
            if role == "assistant":
                content = sanitize_visible_reply_text(content, fallback_text="")
                if not content:
                    continue
                lines.append(f"{bot_name}: {content[:180]}")
            elif role == "user":
                if not is_safe_visible_text(content):
                    continue
                if self._looks_like_social_rendered_line(content):
                    lines.append(content[:180])
                    continue
                if content.startswith("[") and "说:" in content:
                    lines.append(content[:180])
                else:
                    lines.append(f"用户: {content[:180]}")

        transcript = "\n".join(lines)
        if getattr(getattr(self.config, "global_settings", None), "debug_mode", False) and transcript:
            logger.debug(f"[LaneManager] recent transcript for {lane_umo}: {transcript[:200]!r}")
        return transcript
