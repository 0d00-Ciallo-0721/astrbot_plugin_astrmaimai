"""AstrMai Host/Gateway integration probe.

This probe is deliberately separate from ``live_llm_probe``.  The latter calls
an OpenAI-compatible provider directly; this module measures the running
AstrMai host through its admin runtime API and an explicitly configured event
adapter.  It never invents queue, lock, turn, or background values when the
host does not expose them.

Configure a real event injector with ``ASTRMAI_HOST_EVENT_ADAPTER`` using a
``module:function`` import path, or explicitly provide
``ASTRMAI_HOST_EVENT_URL`` for a JSON POST endpoint.  The callable/endpoint
receives one event payload and may return host-specific turn data.  No private
AstrBot transport route is guessed by this module.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import importlib
import inspect
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Iterable, Protocol


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts" / "live_validation"
MEASUREMENT_SCOPE = "astrmai_host"
SCHEMA_VERSION = "astrmai-host-live-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: str) -> str | None:
    value = str(value or "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None


_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "token",
    "secret",
    "password",
    "prompt",
    "message",
    "content",
    "response",
    "cookie",
    "header",
    "user_text",
    "raw_text",
    "error",
}
_EXACT_SENSITIVE_KEYS = {"text"}


def _safe_json(value: Any, *, key: str = "") -> Any:
    normalized_key = str(key or "").lower()
    if normalized_key in _EXACT_SENSITIVE_KEYS or any(marker in normalized_key for marker in _SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _safe_json(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(v, key=key) for v in value]
    if isinstance(value, str):
        return value if len(value) <= 240 else f"{value[:240]}...[truncated]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _public_url(value: str) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    segments = parsed.path.split("/")
    redact_next = False
    for index, segment in enumerate(segments):
        normalized = segment.lower()
        if redact_next or normalized.startswith(("sk-", "token=", "key=")):
            segments[index] = "[redacted]"
            redact_next = False
        elif normalized in {"token", "key", "secret", "auth", "authorization"}:
            redact_next = True
    query = "[redacted]" if parsed.query else ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/".join(segments), query, ""))


@dataclass(frozen=True)
class HostProbeConfig:
    base_url: str = ""
    api_prefix: str = "/astrmai/admin"
    api_key: str = ""
    timeout_sec: float = 10.0
    event_adapter: str = ""
    event_url: str = ""
    sample_interval_sec: float = 1.0
    adapter_timeout_sec: float = 10.0

    @classmethod
    def from_env(cls) -> "HostProbeConfig":
        try:
            timeout_sec = float(os.getenv("ASTRMAI_HOST_TIMEOUT_SEC", "10"))
        except (TypeError, ValueError):
            timeout_sec = 10.0
        try:
            sample_interval_sec = float(os.getenv("ASTRMAI_HOST_SAMPLE_INTERVAL_SEC", "1"))
        except (TypeError, ValueError):
            sample_interval_sec = 1.0
        try:
            adapter_timeout_sec = float(os.getenv("ASTRMAI_HOST_ADAPTER_TIMEOUT_SEC", str(timeout_sec)))
        except (TypeError, ValueError):
            adapter_timeout_sec = timeout_sec
        return cls(
            base_url=os.getenv("ASTRMAI_HOST_BASE_URL", "").strip().rstrip("/"),
            api_prefix=os.getenv("ASTRMAI_HOST_API_PREFIX", "/astrmai/admin").strip().rstrip("/"),
            api_key=os.getenv("ASTRMAI_HOST_API_KEY", ""),
            timeout_sec=max(0.1, timeout_sec),
            event_adapter=os.getenv("ASTRMAI_HOST_EVENT_ADAPTER", "").strip(),
            event_url=os.getenv("ASTRMAI_HOST_EVENT_URL", "").strip(),
            sample_interval_sec=max(0.1, sample_interval_sec),
            adapter_timeout_sec=max(0.1, adapter_timeout_sec),
        )

    def public_summary(self) -> dict[str, Any]:
        return {
            "base_url": _public_url(self.base_url),
            "api_prefix": self.api_prefix,
            "api_key_present": bool(self.api_key),
            "api_key_fingerprint": _fingerprint(self.api_key),
            "timeout_sec": self.timeout_sec,
            "event_adapter_configured": bool(self.event_adapter or self.event_url),
            "event_adapter": self.event_adapter or None,
            "event_url_configured": bool(self.event_url),
            "event_url": _public_url(self.event_url),
            "sample_interval_sec": self.sample_interval_sec,
            "adapter_timeout_sec": self.adapter_timeout_sec,
        }


class EventAdapter(Protocol):
    def __call__(self, payload: dict[str, Any]) -> dict[str, Any] | Awaitable[dict[str, Any]]: ...


def _load_adapter(spec: str) -> EventAdapter | None:
    if not spec:
        return None
    module_name, separator, attr_name = spec.partition(":")
    if not separator or not module_name or not attr_name:
        raise ValueError("ASTRMAI_HOST_EVENT_ADAPTER must use module:function syntax")
    target = getattr(importlib.import_module(module_name), attr_name, None)
    if not callable(target):
        raise TypeError(f"host event adapter is not callable: {spec}")
    return target


class HostApiClient:
    def __init__(self, config: HostProbeConfig):
        self.config = config

    async def request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.config.base_url:
            return {
                "status": "not_configured",
                "measurement_scope": MEASUREMENT_SCOPE,
                "failure_class": "host_not_configured",
                "path": path,
            }

        url = f"{self.config.base_url}{path}"
        headers = {"Accept": "application/json", "User-Agent": "AstrMai-Host-Probe/1.0"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        started = time.perf_counter()

        def _read() -> tuple[int, str]:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=self.config.timeout_sec) as response:
                return int(getattr(response, "status", 200) or 200), response.read().decode("utf-8", errors="replace")

        try:
            status_code, body = await asyncio.to_thread(_read)
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError as exc:
                return {
                    "status": "failed",
                    "measurement_scope": MEASUREMENT_SCOPE,
                    "failure_class": "host_invalid_json",
                    "http_status": status_code,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "parse_error": type(exc).__name__,
                    "path": path,
                }
            return {
                "status": "passed" if 200 <= status_code < 300 else "failed",
                "measurement_scope": MEASUREMENT_SCOPE,
                "failure_class": None if 200 <= status_code < 300 else "host_http_error",
                "http_status": status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "path": path,
                "body": decoded if isinstance(decoded, dict) else {"value": decoded},
            }
        except urllib.error.HTTPError as exc:
            return {
                "status": "failed",
                "measurement_scope": MEASUREMENT_SCOPE,
                "failure_class": "host_auth_error" if exc.code in {401, 403} else "host_http_error",
                "http_status": int(exc.code),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "path": path,
            }
        except TimeoutError:
            return {
                "status": "failed",
                "measurement_scope": MEASUREMENT_SCOPE,
                "failure_class": "host_timeout",
                "http_status": None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "path": path,
            }
        except urllib.error.URLError as exc:
            return {
                "status": "failed",
                "measurement_scope": MEASUREMENT_SCOPE,
                "failure_class": "host_network_error",
                "http_status": None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "path": path,
                "error_type": type(exc.reason).__name__ if getattr(exc, "reason", None) else type(exc).__name__,
            }
        except Exception as exc:  # pragma: no cover - defensive host boundary
            return {
                "status": "failed",
                "measurement_scope": MEASUREMENT_SCOPE,
                "failure_class": "host_client_error",
                "http_status": None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "path": path,
                "error_type": type(exc).__name__,
            }

    async def runtime_status(self) -> dict[str, Any]:
        return await self.request("GET", f"{self.config.api_prefix}/runtime/status")

    async def runtime_history(self) -> dict[str, Any]:
        return await self.request("GET", f"{self.config.api_prefix}/runtime/status/history")


class _HttpEventAdapter:
    def __init__(self, client: HostApiClient, event_url: str):
        self.client = client
        self.event_url = event_url

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.request("POST", self.event_url, json_body=payload)
        body = response.get("body") if isinstance(response, dict) else None
        observed = dict(body) if isinstance(body, dict) else {}
        observed.setdefault("status", response.get("status"))
        if response.get("failure_class"):
            observed.setdefault("failure_class", response["failure_class"])
        return observed


SCENARIOS: dict[str, dict[str, Any]] = {
    "main_reply_private": {"kind": "turn", "channel": "private", "text": "probe private reply"},
    "main_reply_group": {"kind": "turn", "channel": "group", "text": "probe group reply"},
    "main_reply_at": {"kind": "turn", "channel": "group_at", "text": "@bot probe addressed reply"},
    "multi_group_queue_b01": {"kind": "pressure", "channel": "multi_group", "text": "B01 queue probe", "concurrency": 8, "messages": 3},
    "judge_b05": {"kind": "attention", "channel": "annotated", "text": "B05 judge probe", "messages": 6},
    "memory_b07": {"kind": "memory", "channel": "retrieval", "text": "B07 memory probe"},
    "background_b08": {"kind": "background", "channel": "mixed", "text": "B08 background budget probe"},
    "active_dispatch_b15_b16": {"kind": "active", "channel": "proactive", "text": "B15 B16 active dispatch probe"},
    "reread_profile": {"kind": "profile", "channel": "proactive", "text": "reread and profile probe"},
    "parser_coexistence": {"kind": "parser", "channel": "mixed", "text": "parser coexistence probe"},
    "tool_loop": {"kind": "tool_loop", "channel": "private", "text": "tool loop probe"},
}

TERMINAL_STATUSES = {"completed", "failed", "skipped", "timeout", "budget_exhausted", "cancelled", "degraded"}

COMMON_HOST_EVIDENCE = ("host_event_id", "host_chat_id", "host_event_type", "injected_at")
SCENARIO_EVIDENCE: dict[str, tuple[str, ...]] = {
    "multi_group_queue_b01": (
        "gateway_queue_wait_ms",
        "semaphore_wait_ms",
        "lane_wait_ms",
        "sys2_lock_wait_ms",
        "executor_lock_wait_ms",
    ),
    "judge_b05": ("judge_called", "judge_skipped", "filter_reason", "expected_action", "actual_action"),
    "memory_b07": ("vector_status", "index_generation", "faiss_latency_ms", "fallback_source", "outbox_pending_count"),
    "background_b08": ("background_active", "queue_wait_ms", "execution_timeout", "late_completed"),
}


def _scenario_payloads(name: str, definition: dict[str, Any], *, repeat: int) -> list[dict[str, Any]]:
    """Build concrete event intents; the adapter maps them to host events."""
    payloads: list[dict[str, Any]] = []
    for iteration in range(max(1, int(repeat))):
        intents: list[dict[str, Any]]
        if name == "multi_group_queue_b01":
            intents = []
            for group_index in range(int(definition.get("concurrency", 8))):
                for message_index in range(int(definition.get("messages", 3))):
                    intents.append({"group_id": f"probe-group-{group_index + 1}", "message_index": message_index + 1})
        elif name == "judge_b05":
            labels = ("small_talk", "explicit_at", "follow_up", "unrelated", "command", "active")
            intents = [{"label": label} for label in labels]
        elif name == "memory_b07":
            intents = [{"operation": operation} for operation in ("hot_query", "cold_start", "rebuild", "outbox_replay")]
        elif name == "background_b08":
            intents = [{"workload": workload} for workload in ("main_reply", "learning", "profiling", "projection", "compaction", "embedding")]
        elif name == "active_dispatch_b15_b16":
            intents = [{"operation": operation} for operation in ("duplicate", "sensor_filter", "safety_block", "claim_expired", "shutdown_dispatch")]
        elif name == "reread_profile":
            intents = [{"operation": operation} for operation in ("reread", "profile")]
        elif name == "parser_coexistence":
            intents = [{"message_kind": kind} for kind in ("text", "command", "image", "image_text", "link")]
        else:
            intents = [{}]
        for payload in intents:
            payload = dict(payload)
            payload.setdefault("iteration", iteration + 1)
            payloads.append(payload)
    return payloads


def _validate_adapter_result(name: str, observed: dict[str, Any]) -> tuple[str, str, str | None]:
    """Require real host IDs, an explicit terminal state, and scenario evidence."""
    if not isinstance(observed, dict):
        return "measurement_incomplete", "", "adapter_result_not_object"
    host_turn_id = str(observed.get("host_turn_id") or "").strip()
    trace_id = str(observed.get("trace_id") or "").strip()
    final_status = str(observed.get("final_status") or "").strip().lower()
    if not host_turn_id:
        return "measurement_incomplete", final_status, "host_turn_id_missing"
    if not trace_id:
        return "measurement_incomplete", final_status, "trace_id_missing"
    missing_common = [key for key in COMMON_HOST_EVIDENCE if not str(observed.get(key) or "").strip()]
    if missing_common:
        return "measurement_incomplete", final_status, f"host_event_evidence_missing:{','.join(missing_common)}"
    if final_status not in TERMINAL_STATUSES:
        return "measurement_incomplete", final_status, "terminal_status_missing"
    evidence = dict(observed.get("metrics") or {}) if isinstance(observed.get("metrics"), dict) else {}
    evidence.update({key: value for key, value in observed.items() if key not in evidence})
    missing_evidence = [key for key in SCENARIO_EVIDENCE.get(name, ()) if evidence.get(key) is None]
    if missing_evidence:
        return "measurement_incomplete", final_status, f"scenario_evidence_missing:{','.join(missing_evidence)}"
    return ("passed" if final_status == "completed" else final_status), final_status, None


def _empty_metrics() -> dict[str, Any]:
    # These keys are intentionally null until the configured adapter or host
    # trace exposes a measured value.  Zero would falsely imply observation.
    return {
        "gateway_queue_wait_ms": None,
        "semaphore_wait_ms": None,
        "lane_wait_ms": None,
        "sys2_lock_wait_ms": None,
        "executor_lock_wait_ms": None,
        "turn_total_elapsed_ms": None,
        "turn_final_status": None,
        "retry_count": None,
        "fallback": None,
        "judge": None,
        "tool_loop": None,
        "background": None,
        "faiss": None,
        "active_dispatch": None,
        "reread": None,
        "profile": None,
        "parser": None,
    }


def _runtime_projection(result: dict[str, Any]) -> dict[str, Any]:
    body = result.get("body") if isinstance(result, dict) else None
    if not isinstance(body, dict):
        return {"available": False, "measurement_missing": ["runtime_diagnostics"]}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return {
        "available": True,
        "snapshot_at": data.get("snapshot_at"),
        "diagnostics_status": data.get("diagnostics_status"),
        "component_errors": _safe_json(data.get("component_errors", [])),
        "gateway": _safe_json(data.get("infrastructure", {}).get("gateway")) if isinstance(data.get("infrastructure"), dict) else None,
        "background": _safe_json(data.get("infrastructure", {}).get("background_task_budget")) if isinstance(data.get("infrastructure"), dict) else None,
        "attention": _safe_json(data.get("attention")),
        "memory": _safe_json(data.get("memory")),
        "long_turn": _safe_json(data.get("long_turn")),
        "group_reread_observer": _safe_json(data.get("group_reread_observer")),
        "proactive": _safe_json(data.get("proactive")),
        "history": _safe_json(data.get("history", [])),
    }


class AstrMaiHostProbe:
    def __init__(
        self,
        config: HostProbeConfig | None = None,
        *,
        output_root: Path | None = None,
        measurement_scope: str = MEASUREMENT_SCOPE,
    ):
        self.config = config or HostProbeConfig.from_env()
        self.measurement_scope = str(measurement_scope or MEASUREMENT_SCOPE)
        self.client = HostApiClient(self.config)
        self.adapter_error: str | None = None
        try:
            if self.config.event_adapter:
                self.adapter = _load_adapter(self.config.event_adapter)
            elif self.config.event_url:
                self.adapter = _HttpEventAdapter(self.client, self.config.event_url)
            else:
                self.adapter = None
        except Exception as exc:
            self.adapter = None
            self.adapter_error = f"{type(exc).__name__}: {exc}"
        self.output_root = output_root or DEFAULT_OUTPUT_ROOT
        self.run_id = f"astrmai_host_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        self.run_dir = self.output_root / self.run_id
        self.runtime_samples: list[dict[str, Any]] = []
        self.host_requests: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.stages: list[dict[str, Any]] = []

    async def collect_runtime(self, *, reason: str, phase: str = "snapshot") -> dict[str, Any]:
        status = await self.client.runtime_status()
        history = await self.client.runtime_history()
        sample = {
            "sample_id": uuid.uuid4().hex,
            "sampled_at": _utc_now(),
            "reason": reason,
            "phase": phase,
            "measurement_scope": self.measurement_scope,
            "status_request": {k: v for k, v in status.items() if k != "body"},
            "history_request": {k: v for k, v in history.items() if k != "body"},
            "snapshot": _runtime_projection(status),
            "history": _runtime_projection(history).get("history", []),
        }
        self.runtime_samples.append(sample)
        return sample

    async def _sample_during(self, scenario: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(self.config.sample_interval_sec)
            if stop.is_set():
                break
            try:
                await self.collect_runtime(reason=f"{scenario}:during", phase="during")
            except asyncio.CancelledError:
                raise
            except Exception:
                # collect_runtime already records host failures; sampling must
                # never cancel the scenario itself.
                continue

    async def _invoke_adapter(self, payload: dict[str, Any]) -> Any:
        adapter = self.adapter
        if adapter is None:
            raise RuntimeError("event adapter is not configured")
        timeout = self.config.adapter_timeout_sec
        callable_target = getattr(adapter, "__call__", adapter)
        is_async = inspect.iscoroutinefunction(adapter) or inspect.iscoroutinefunction(callable_target)
        if is_async:
            return await asyncio.wait_for(adapter(payload), timeout=timeout)
        observed = await asyncio.wait_for(asyncio.to_thread(adapter, payload), timeout=timeout)
        if inspect.isawaitable(observed):
            return await asyncio.wait_for(observed, timeout=timeout)
        return observed

    async def run_scenario(self, name: str, *, repeat: int = 1) -> dict[str, Any]:
        definition = SCENARIOS.get(name)
        started = _utc_now()
        started_mono = time.perf_counter()
        before = await self.collect_runtime(reason=f"{name}:before", phase="before")
        result: dict[str, Any] = {
            "scenario": name,
            "measurement_scope": self.measurement_scope,
            "scenario_status": "configuration_error" if self.adapter_error else ("not_configured" if self.adapter is None else "pending"),
            "started_at": started,
            "finished_at": None,
            "requests_started": 0,
            "requests_finished": 0,
            "turns_started": 0,
            "turns_finalized": 0,
            "measurement_missing": [],
            "failure_class": None,
            "metrics": _empty_metrics(),
            "terminal_status_counts": {},
            "runtime_sample_before": before["sample_id"],
            "runtime_sample_after": None,
        }
        sampler_stop = asyncio.Event()
        sampler_task: asyncio.Task[None] | None = None
        if definition is None:
            result.update({"scenario_status": "invalid_scenario", "failure_class": "unknown_scenario"})
        elif self.adapter_error:
            result.update({"failure_class": "event_adapter_configuration", "measurement_missing": ["event_adapter"]})
            result["adapter_error"] = self.adapter_error
        elif self.adapter is None:
            result["measurement_missing"] = [
                "event_adapter",
                "gateway_queue_wait_ms",
                "semaphore_wait_ms",
                "lane_wait_ms",
                "sys2_lock_wait_ms",
                "executor_lock_wait_ms",
                "turn_total_elapsed_ms",
                "turn_final_status",
                "retry_count",
                "fallback",
            ]
        else:
            try:
                sampler_task = asyncio.create_task(self._sample_during(name, sampler_stop))
                intents = _scenario_payloads(name, definition, repeat=repeat)
                concurrency = int(definition.get("concurrency", 1)) if definition.get("kind") == "pressure" else 1
                semaphore = asyncio.Semaphore(max(1, concurrency))

                async def invoke(intent: dict[str, Any], sequence: int) -> dict[str, Any]:
                    payload = {
                        "run_id": self.run_id,
                        "scenario": name,
                        "scenario_definition": definition,
                        "event_id": uuid.uuid4().hex,
                        "sequence": sequence,
                        "intent": intent,
                        "measurement_scope": self.measurement_scope,
                        "sent_at": _utc_now(),
                    }
                    result["requests_started"] += 1
                    result["turns_started"] += 1
                    started_turn = time.perf_counter()
                    request_record = {
                        "request_id": payload["event_id"],
                        "event_id": payload["event_id"],
                        "scenario": name,
                        "iteration": intent.get("iteration"),
                        "request_started_at": payload["sent_at"],
                        "measurement_scope": self.measurement_scope,
                    }
                    async with semaphore:
                        try:
                            observed = await self._invoke_adapter(payload)
                        except asyncio.CancelledError:
                            raise
                        except asyncio.TimeoutError:
                            observed = {
                                "status": "timeout",
                                "failure_class": "adapter_timeout",
                                "final_status": "timeout",
                            }
                        except Exception as exc:
                            observed = {
                                "status": "failed",
                                "failure_class": f"adapter_{type(exc).__name__}",
                                "final_status": "failed",
                            }
                    observed = observed if isinstance(observed, dict) else {}
                    observed_metrics = observed.get("metrics") if isinstance(observed.get("metrics"), dict) else {}
                    result["requests_finished"] += 1
                    scenario_status, terminal_status, validation_error = _validate_adapter_result(name, observed)
                    host_turn_id = str(observed.get("host_turn_id") or "").strip() or None
                    trace_id = str(observed.get("trace_id") or "").strip() or None
                    if scenario_status in TERMINAL_STATUSES or scenario_status == "passed":
                        result["turns_finalized"] += 1
                    turn_record = {
                        "host_turn_id": host_turn_id,
                        "trace_id": trace_id,
                        "event_id": payload["event_id"],
                        "scenario": name,
                        "iteration": intent.get("iteration"),
                        "measurement_scope": self.measurement_scope,
                        "started_at": payload["sent_at"],
                        "finished_at": _utc_now(),
                        "adapter_elapsed_ms": round((time.perf_counter() - started_turn) * 1000, 2),
                        "final_status": terminal_status or None,
                        "measurement_status": scenario_status,
                        "measurement_error": validation_error,
                        "observed": _safe_json(observed),
                    }
                    self.turns.append(turn_record)
                    request_record.update({
                        "request_finished_at": turn_record["finished_at"],
                        "adapter_status": observed.get("status"),
                        "final_status": turn_record["final_status"],
                        "host_turn_id": host_turn_id,
                        "trace_id": trace_id,
                        "host_event_id": observed.get("host_event_id"),
                        "host_chat_id": observed.get("host_chat_id"),
                        "host_event_type": observed.get("host_event_type"),
                        "injected_at": observed.get("injected_at"),
                        "response_summary": _safe_json(observed.get("response_summary")),
                        "failure_class": observed.get("failure_class") or validation_error,
                        "measurement_status": scenario_status,
                    })
                    self.host_requests.append(request_record)
                    for key in result["metrics"]:
                        if key in observed_metrics:
                            result["metrics"][key] = _safe_json(observed_metrics[key])
                        elif key in observed:
                            result["metrics"][key] = _safe_json(observed[key])
                    return {
                        "scenario_status": scenario_status,
                        "terminal_status": terminal_status,
                        "failure": validation_error or observed.get("failure_class"),
                    }

                results = await asyncio.gather(*(invoke(intent, index + 1) for index, intent in enumerate(intents)))
                failures = [item for item in results if item["scenario_status"] not in {"passed", "completed"}]
                incomplete = [item for item in results if item["scenario_status"] == "measurement_incomplete"]
                result["terminal_status_counts"] = dict(
                    Counter(item["terminal_status"] for item in results if item.get("terminal_status"))
                )
                if failures:
                    result["scenario_status"] = "measurement_incomplete" if incomplete else "failed"
                    result["failure_class"] = str(failures[0].get("failure") or "host_event_failed")
                else:
                    result["scenario_status"] = "passed"
            except asyncio.CancelledError:
                result.update({"scenario_status": "cancelled", "failure_class": "cancelled"})
            except Exception as exc:
                result.update({"scenario_status": "failed", "failure_class": f"adapter_{type(exc).__name__}"})
            finally:
                if sampler_task is not None:
                    sampler_stop.set()
                    sampler_task.cancel()
                    await asyncio.gather(sampler_task, return_exceptions=True)
        if definition and definition.get("kind") in {"pressure", "background"}:
            await asyncio.sleep(self.config.sample_interval_sec)
            await self.collect_runtime(reason=f"{name}:drain", phase="drain")
        after = await self.collect_runtime(reason=f"{name}:after", phase="after")
        result["runtime_sample_after"] = after["sample_id"]
        result["finished_at"] = _utc_now()
        result["elapsed_ms"] = round((time.perf_counter() - started_mono) * 1000, 2)
        self.stages.append(result)
        return result

    async def run(self, scenarios: Iterable[str] | None = None, *, repeat: int = 1) -> dict[str, Any]:
        selected = list(SCENARIOS.keys()) if scenarios is None else list(scenarios)
        started = _utc_now()
        await self.collect_runtime(reason="run:initial")
        for name in selected:
            await self.run_scenario(name, repeat=repeat)
        finished = _utc_now()
        statuses = [item["scenario_status"] for item in self.stages]
        if not statuses or all(status == "not_configured" for status in statuses):
            overall_status = "not_configured"
        elif all(status == "passed" for status in statuses):
            overall_status = "passed"
        else:
            # Every non-passed scenario, including skipped/timeout/budget or
            # degraded terminal outcomes, blocks a passing run.
            overall_status = "degraded"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "measurement_scope": self.measurement_scope,
            "status": overall_status,
            "started_at": started,
            "finished_at": finished,
            "config": self.config.public_summary(),
            "scenarios_requested": selected,
            "scenarios": self.stages,
            "runtime_sample_count": len(self.runtime_samples),
            "turn_count": len(self.turns),
            "measurement_contract": {
                "unmeasured_values_are_null": True,
                "event_adapter_required_for_turns": True,
                "provider_calls": "not measured by this host probe; use provider_probe separately",
            },
        }
        self._write_artifacts(payload)
        return payload

    def _write_artifacts(self, payload: dict[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "measurement_scope": self.measurement_scope,
            "status": payload["status"],
            "scenario_count": len(self.stages),
            "passed_count": sum(item["scenario_status"] == "passed" for item in self.stages),
            "not_configured_count": sum(item["scenario_status"] == "not_configured" for item in self.stages),
            "failed_count": sum(item["scenario_status"] not in {"passed", "not_configured"} for item in self.stages),
            "measurement_incomplete_count": sum(item["scenario_status"] == "measurement_incomplete" for item in self.stages),
            "host_turn_missing_count": sum(
                1 for turn in self.turns if not turn.get("host_turn_id")
            ),
            "trace_missing_count": sum(1 for turn in self.turns if not turn.get("trace_id")),
            "peak_gateway_queue": self._peak_runtime_value((("gateway", "queue"), ("gateway", "queued"), ("gateway", "queue_depth"))),
            "peak_background_active": self._peak_runtime_value((("background", "active"),)),
            "runtime_sample_count": len(self.runtime_samples),
            "turn_count": len(self.turns),
            "event_adapter_configured": bool(self.adapter),
        }
        (self.run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        with (self.run_dir / "host_requests.jsonl").open("w", encoding="utf-8") as handle:
            for request in self.host_requests:
                handle.write(json.dumps(_safe_json(request), ensure_ascii=False) + "\n")
        with (self.run_dir / "runtime_samples.jsonl").open("w", encoding="utf-8") as handle:
            for sample in self.runtime_samples:
                handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
        with (self.run_dir / "turns.jsonl").open("w", encoding="utf-8") as handle:
            for turn in self.turns:
                handle.write(json.dumps(turn, ensure_ascii=False) + "\n")
        with (self.run_dir / "stages.jsonl").open("w", encoding="utf-8") as handle:
            for stage in self.stages:
                handle.write(json.dumps(stage, ensure_ascii=False) + "\n")
        passed = sum(item["scenario_status"] == "passed" for item in self.stages)
        not_configured = sum(item["scenario_status"] == "not_configured" for item in self.stages)
        failed = len(self.stages) - passed - not_configured
        lines = [
            "# AstrMai Host/Gateway Probe",
            "",
            f"- measurement_scope: `{self.measurement_scope}`",
            f"- run_id: `{self.run_id}`",
            f"- status: `{payload['status']}`",
            f"- scenarios: `{len(self.stages)}` (passed={passed}, not_configured={not_configured}, failed={failed})",
            f"- runtime samples: `{len(self.runtime_samples)}`",
            f"- event adapter configured: `{bool(self.adapter)}`",
            f"- event adapter error: `{self.adapter_error or '-'}`",
            "",
            "Values unavailable from the host or adapter remain `null`; they are not interpreted as zero.",
            "",
            "| Scenario | Status | Requests | Turns | Missing measurements | Failure |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ]
        for item in self.stages:
            lines.append(
                f"| {item['scenario']} | {item['scenario_status']} | {item['requests_finished']}/{item['requests_started']} | "
                f"{item['turns_finalized']}/{item['turns_started']} | {', '.join(item['measurement_missing']) or '-'} | {item['failure_class'] or '-'} |"
            )
        (self.run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    def _peak_runtime_value(self, paths: tuple[tuple[str, ...], ...]) -> float | int | None:
        values: list[float] = []
        for sample in self.runtime_samples:
            for candidate in paths:
                current: Any = sample.get("snapshot", {})
                for path in candidate:
                    if not isinstance(current, dict):
                        current = None
                        break
                    current = current.get(path)
                if isinstance(current, (int, float)):
                    values.append(float(current))
        if not values:
            return None
        peak = max(values)
        return int(peak) if peak.is_integer() else round(peak, 2)


async def run_host_probe(
    *,
    scenarios: Iterable[str] | None = None,
    repeat: int = 1,
    output_root: Path | None = None,
    config: HostProbeConfig | None = None,
    measurement_scope: str = MEASUREMENT_SCOPE,
) -> dict[str, Any]:
    return await AstrMaiHostProbe(
        config,
        output_root=output_root,
        measurement_scope=measurement_scope,
    ).run(scenarios, repeat=repeat)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect AstrMai Host/Gateway runtime diagnostics.")
    parser.add_argument("--scenario", action="append", dest="scenarios", choices=sorted(SCENARIOS), help="Scenario to run; repeatable. Defaults to all.")
    parser.add_argument("--repeat", type=int, default=1, help="Iterations per scenario when an event adapter is configured.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_ROOT), help="Artifact root directory.")
    args = parser.parse_args(argv)
    payload = asyncio.run(run_host_probe(scenarios=args.scenarios, repeat=max(1, args.repeat), output_root=Path(args.output_dir).resolve()))
    print(json.dumps({"run_id": payload["run_id"], "status": payload["status"], "measurement_scope": MEASUREMENT_SCOPE}, ensure_ascii=False))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
