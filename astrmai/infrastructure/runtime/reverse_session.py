from __future__ import annotations

import re
from typing import Any, Dict


REVERSE_SESSION_TAG = "astrbot_reverse_session"
REVERSE_SESSION_PATTERN = re.compile(
    rf"<{REVERSE_SESSION_TAG}>\s*(.*?)\s*</{REVERSE_SESSION_TAG}>",
    re.DOTALL | re.IGNORECASE,
)


def render_reverse_session_block(
    session_id: str,
    *,
    session_scope: str = "",
    parent_session_id: str = "",
    session_kind: str = "",
    source: str = "",
) -> str:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return ""
    payload = {
        "session_id": normalized_session_id,
        "session_scope": str(session_scope or "").strip(),
        "parent_session_id": str(parent_session_id or "").strip(),
        "session_kind": str(session_kind or "").strip(),
        "source": str(source or "").strip(),
    }
    body = "\n".join(f"{key}={value}" for key, value in payload.items())
    return f"<{REVERSE_SESSION_TAG}>\n{body}\n</{REVERSE_SESSION_TAG}>"


def parse_reverse_session_block(text: str) -> Dict[str, str]:
    raw_text = str(text or "")
    match = REVERSE_SESSION_PATTERN.search(raw_text)
    if not match:
        return {}
    parsed: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        normalized = str(line or "").strip()
        if not normalized or "=" not in normalized:
            continue
        key, value = normalized.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def strip_reverse_session_block(text: str) -> str:
    raw_text = str(text or "")
    if not raw_text:
        return ""
    stripped = REVERSE_SESSION_PATTERN.sub("", raw_text)
    return stripped.strip()


def append_reverse_session_block(
    system_prompt: str,
    session_id: str,
    *,
    session_scope: str = "",
    parent_session_id: str = "",
    session_kind: str = "",
    source: str = "",
) -> str:
    block = render_reverse_session_block(
        session_id,
        session_scope=session_scope,
        parent_session_id=parent_session_id,
        session_kind=session_kind,
        source=source,
    )
    base_prompt = strip_reverse_session_block(system_prompt)
    if not block:
        return base_prompt
    if not base_prompt:
        return block
    return f"{base_prompt}\n\n{block}"


def provider_is_gemini_reverse(provider: Any) -> bool:
    if provider is None:
        return False

    provider_type = ""
    try:
        meta = provider.meta() if callable(getattr(provider, "meta", None)) else None
        provider_type = str(getattr(meta, "type", "") or "").strip().lower()
    except Exception:
        provider_type = ""

    provider_config = getattr(provider, "provider_config", None) or {}
    if provider_type != "openai_chat_completion":
        return False

    reverse_provider = str(provider_config.get("reverse_provider", "") or "").strip().lower()
    reverse_plugin = str(provider_config.get("reverse_plugin", "") or "").strip().lower()
    reverse_kind = str(provider_config.get("reverse_kind", "") or "").strip().lower()
    reverse_session_via = str(provider_config.get("reverse_session_via", "") or "").strip().lower()
    supports_reverse_session = provider_config.get("supports_reverse_session", None)
    gemini_reverse_flag = provider_config.get("gemini_reverse", None)

    normalized_truthy = {"1", "true", "yes", "on"}
    supports_reverse_session = (
        supports_reverse_session is True
        or str(supports_reverse_session or "").strip().lower() in normalized_truthy
    )
    gemini_reverse_flag = (
        gemini_reverse_flag is True
        or str(gemini_reverse_flag or "").strip().lower() in normalized_truthy
    )

    reverse_provider_matches = reverse_provider in {"gemini_web", "gemini_reverse", "gemini-reverse"}
    reverse_plugin_matches = reverse_plugin in {
        "astrbot_plugin_gemini_reverse",
        "gemini_reverse",
    }
    reverse_kind_matches = reverse_kind in {"gemini_web", "gemini_reverse", "gemini-reverse"}
    reverse_transport_matches = reverse_session_via in {"system_prompt", "prompt_sentinel"}

    if reverse_provider_matches or reverse_plugin_matches or reverse_kind_matches:
        return True
    if gemini_reverse_flag:
        return True
    if supports_reverse_session and reverse_transport_matches:
        return True
    return False


def maybe_attach_reverse_session_block(
    system_prompt: str,
    provider: Any,
    *,
    session_id: str,
    session_scope: str = "",
    parent_session_id: str = "",
    session_kind: str = "",
    source: str = "",
) -> str:
    if REVERSE_SESSION_PATTERN.search(str(system_prompt or "")):
        return str(system_prompt or "")
    if not provider_is_gemini_reverse(provider):
        return str(system_prompt or "")
    return append_reverse_session_block(
        system_prompt,
        session_id,
        session_scope=session_scope,
        parent_session_id=parent_session_id,
        session_kind=session_kind,
        source=source,
    )
