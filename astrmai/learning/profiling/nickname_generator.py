from __future__ import annotations

from typing import Any

from ...infrastructure.gateway.json_utils import parse_json_contract


class NicknameGenerator:
    def choose(self, display_name: str, preferred: str = "") -> str:
        return preferred or display_name or "未知用户"

    def build_template_payload(self, profile, persona_summary: str = "") -> dict[str, str]:
        analysis = getattr(profile, "persona_analysis", "") or "暂无画像"
        tags = getattr(profile, "tags", []) or []
        tags_text = ", ".join(str(item) for item in tags) if tags else "暂无"
        return {
            "persona_summary": str(persona_summary or "").strip(),
            "name": str(getattr(profile, "name", "") or "").strip(),
            "analysis": str(analysis or "").strip()[:200],
            "tags_text": str(tags_text or "").strip(),
        }

    def build_prompt(self, profile, persona_summary: str = "") -> str:
        persona_injection = f"[你的人设摘要]: {persona_summary}\n" if persona_summary else ""
        analysis = getattr(profile, "persona_analysis", "") or "暂无画像"
        tags = getattr(profile, "tags", []) or []
        tags_text = ", ".join(str(item) for item in tags) if tags else "暂无"
        return f"""{persona_injection}
你需要给一个你认识的朋友起一个昵称。

关于这个人：
- 原始名字：{getattr(profile, 'name', '')}
- 画像：{analysis[:200]}
- 标签：{tags_text}

请根据他的性格特点和你与他的相处模式，给他起一个符合你人设风格的称呼。
要求：
- 最多 6 个字
- 可以是亲切的简称、调侃或昵称
- 用 JSON 返回：{{"nickname": "你起的昵称", "reason": "起这个名字的理由"}}
"""

    def parse_result(self, result: Any) -> tuple[str, str]:
        self.last_parse_status = "parse_failed"
        text = str(result or "").strip()
        if not text:
            self.last_parse_status = "empty"
            return "", ""
        try:
            parsed = parse_json_contract(
                text,
                required_keys=("nickname", "reason"),
                field_types={"nickname": str, "reason": str},
                allow_extra_keys=False,
                allow_naked_members=True,
            )
            data = parsed.value
            if parsed.schema_valid and isinstance(data, dict):
                self.last_parse_status = parsed.terminal_status
                nickname = str(data.get("nickname", "") or "").strip()
                reason = str(data.get("reason", "") or "").strip()
                return nickname, reason
        except Exception:
            self.last_parse_status = "parse_failed"
            pass
        return "", ""


__all__ = ["NicknameGenerator"]
