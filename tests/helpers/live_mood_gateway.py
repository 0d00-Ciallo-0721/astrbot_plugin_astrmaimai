from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.live_test_config import load_live_llm_config


class LiveMoodGateway:
    def __init__(self, *, api_base: str, api_key: str, task_models: list[str], plugin_config: dict):
        self.api_base = api_base
        self.api_key = api_key
        self.task_models = list(task_models)
        self.lane_manager = object()
        self.config = SimpleNamespace(
            reply=SimpleNamespace(
                emotion_mapping=list(
                    ((plugin_config.get("reply", {}) or {}).get("emotion_mapping", []) or [])
                )
            ),
            provider=SimpleNamespace(task_models=list(task_models)),
        )

    async def chat_in_lane_result(self, **kwargs):
        prompt = str(kwargs.get("prompt", "") or "")
        system_prompt = str(kwargs.get("system_prompt", "") or "")
        model = str((kwargs.get("models", []) or self.task_models or [""])[0] or "")
        if not model:
            raise RuntimeError("no task model available for live mood audit")
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 120,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
        }

        def _post() -> dict:
            req = urllib.request.Request(
                f"{self.api_base}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))

        response = await asyncio.to_thread(_post)
        choices = response.get("choices", []) or []
        message = ((choices[0] if choices else {}).get("message", {}) or {})
        raw_completion = str(message.get("content", "") or "")
        parsed_json = {}
        try:
            parsed_json = json.loads(raw_completion) if raw_completion else {}
        except Exception:
            parsed_json = {}
        return SimpleNamespace(parsed_json=parsed_json, raw_completion=raw_completion)


def build_live_mood_gateway():
    config = load_live_llm_config()
    plugin_config = json.loads(config.plugin_config_path.read_text(encoding="utf-8-sig"))
    task_models = list(config.task_models)
    return LiveMoodGateway(
        api_base=config.api_base,
        api_key=config.api_key,
        task_models=task_models,
        plugin_config=plugin_config,
    )
