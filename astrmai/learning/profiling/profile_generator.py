from __future__ import annotations

import json
import re
from typing import Any


class ProfileGenerator:
    def build_profile_payload(self, profile) -> dict:
        return {
            "user_id": getattr(profile, "user_id", ""),
            "name": getattr(profile, "name", ""),
            "nickname": getattr(profile, "nickname", ""),
            "persona_analysis": getattr(profile, "persona_analysis", ""),
            "memory_points": list(getattr(profile, "memory_points", []) or []),
        }

    def build_template_payload(self, profile, persona_summary: str = "") -> dict[str, str]:
        old_analysis = getattr(profile, "persona_analysis", "") or "鏆傛棤鏃х敾鍍?"
        old_tags = getattr(profile, "tags", []) or []
        old_tags_str = ", ".join(old_tags) if old_tags else "鏆傛棤鏍囩"
        old_memory_points = getattr(profile, "memory_points", []) or []
        old_memory_text = "\n".join(str(item) for item in old_memory_points) if old_memory_points else "鏆傛棤璁板繂鐐?"
        profiling_count = int(getattr(profile, "message_count_for_profiling", 0) or 0)
        recent_interaction_summary = str(getattr(profile, "recent_interaction_summary", "") or "").strip() or "鏆傛棤鏈€杩戜簰鍔ㄦ憳瑕?"
        return {
            "persona_summary": str(persona_summary or "").strip(),
            "name": str(getattr(profile, "name", "") or "").strip(),
            "profiling_count": str(profiling_count),
            "old_analysis": str(old_analysis or "").strip(),
            "old_tags_text": str(old_tags_str or "").strip(),
            "old_memory_text": str(old_memory_text or "").strip(),
            "recent_interaction_summary": recent_interaction_summary,
        }

    def build_prompt(self, profile, persona_summary: str = "") -> str:
        persona_injection = f"\n[你的人设摘要]: {persona_summary}\n" if persona_summary else ""
        old_analysis = getattr(profile, "persona_analysis", "") or "暂无旧画像"
        old_tags = getattr(profile, "tags", []) or []
        old_tags_str = ", ".join(old_tags) if old_tags else "暂无标签"
        old_memory_points = getattr(profile, "memory_points", []) or []
        old_memory_text = "\n".join(str(item) for item in old_memory_points) if old_memory_points else "暂无记忆点"
        profiling_count = int(getattr(profile, "message_count_for_profiling", 0) or 0)
        if profiling_count <= 0:
            return None  # ponytail: skip profiling when no new messages
        recent_interaction_summary = str(getattr(profile, "recent_interaction_summary", "") or "").strip() or "暂无最近互动摘要"
        return f"""{persona_injection}
请基于用户“{getattr(profile, 'name', '')}”与你最近的互动，做一次增量人物画像更新。
本轮新增互动次数：{profiling_count}
【旧画像】{old_analysis}
【旧标签】{old_tags_str}
【旧记忆点】
{old_memory_text}
【最近互动摘要】
{recent_interaction_summary}

请严格输出 JSON：
{{
  "tags": ["标签1", "标签2"],
  "summary": "100字以内的整体印象",
  "memory_points": [
    {{"category": "爱好", "content": "喜欢 xxx", "weight": 0.8}}
  ]
}}
"""

    def parse_result(self, result: Any) -> dict[str, Any]:
        tags: list[str] = []
        analysis = ""
        memory_points: list[str] = []
        text = str(result or "").strip()
        if not text:
            return {"tags": tags, "analysis": analysis, "memory_points": memory_points}

        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                raw_tags = data.get("tags", [])
                if isinstance(raw_tags, list):
                    tags = [str(item).strip() for item in raw_tags if str(item).strip()]
                analysis = str(data.get("summary", data.get("analysis", "")) or "").strip()
                raw_points = data.get("memory_points", [])
                if isinstance(raw_points, list):
                    for item in raw_points:
                        if not isinstance(item, dict):
                            continue
                        content = str(item.get("content", "") or "").strip()
                        if not content:
                            continue
                        category = str(item.get("category", "其他") or "其他").strip()
                        weight = item.get("weight", 0.5)
                        memory_points.append(f"{category}:{content}:{weight}")
        except Exception:
            pass

        if not analysis:
            analysis = text
        return {"tags": tags, "analysis": analysis, "memory_points": memory_points}

    def categorize_memory_points(self, memory_points: Any) -> dict[str, list[str]]:
        buckets = {
            "identity_points": [],
            "preference_points": [],
            "relationship_points": [],
            "speech_style_points": [],
        }
        if not isinstance(memory_points, list):
            return buckets

        for raw_point in memory_points:
            if not isinstance(raw_point, str):
                continue
            parts = raw_point.split(":", 2)
            category = parts[0] if parts else "其他"
            content = parts[1] if len(parts) > 1 else raw_point
            normalized = f"{category}:{content}".strip()
            if not normalized:
                continue
            if category in {"身份", "经历", "技能"}:
                buckets["identity_points"].append(normalized)
            elif category in {"爱好", "偏好"}:
                buckets["preference_points"].append(normalized)
            elif category in {"关系", "互动"}:
                buckets["relationship_points"].append(normalized)
            else:
                buckets["speech_style_points"].append(normalized)

        for key, values in buckets.items():
            buckets[key] = values[:6]
        return buckets


__all__ = ["ProfileGenerator"]
