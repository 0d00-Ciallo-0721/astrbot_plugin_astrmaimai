from __future__ import annotations

import json
import re
from typing import Any


class NicknameGenerator:
    def choose(self, display_name: str, preferred: str = "") -> str:
        return preferred or display_name or "未知用户"

    def build_template_payload(self, profile, persona_summary: str = "") -> dict[str, str]:
        analysis = getattr(profile, "persona_analysis", "") or "鏆傛棤鐢诲儚"
        tags = getattr(profile, "tags", []) or []
        tags_text = ", ".join(str(item) for item in tags) if tags else "鏆傛棤"
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
        text = str(result or "").strip()
        if not text:
            return "", ""
        try:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                nickname = str(data.get("nickname", "") or "").strip()
                reason = str(data.get("reason", "") or "").strip()
                return nickname, reason
        except Exception:
            pass
        return self.choose(text), ""


__all__ = ["NicknameGenerator"]
