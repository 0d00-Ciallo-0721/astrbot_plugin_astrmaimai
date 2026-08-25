"""Run every Host scenario against a deterministic, non-network Mock adapter.

This produces ``measurement_scope=offline_mock`` artifacts. It validates the
probe's expansion, terminal-state and scenario-evidence aggregation without
claiming that AstrBot or an LLM Provider was contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.manual.astrmai_host_probe import (
    HostProbeConfig,
    run_host_probe,
)


def _mock_result(payload: dict) -> dict:
    scenario = str(payload.get("scenario", ""))
    result = {
        "status": "completed",
        "final_status": "completed",
        "host_turn_id": f"mock-turn-{payload['event_id']}",
        "trace_id": f"mock-trace-{payload['event_id']}",
        "host_event_id": payload["event_id"],
        "host_chat_id": str(payload.get("intent", {}).get("group_id", "mock-chat")),
        "host_event_type": "group_message" if "group" in scenario else "private_message",
        "injected_at": payload["sent_at"],
        "metrics": {},
    }
    if scenario == "multi_group_queue_b01":
        result["metrics"].update({
            "gateway_queue_wait_ms": 1.0,
            "semaphore_wait_ms": 1.0,
            "lane_wait_ms": 1.0,
            "sys2_lock_wait_ms": 1.0,
            "executor_lock_wait_ms": 1.0,
        })
    elif scenario == "judge_b05":
        result["metrics"].update({
            "judge_called": True,
            "judge_skipped": False,
            "filter_reason": "engaged",
            "expected_action": "reply",
            "actual_action": "reply",
        })
    elif scenario == "memory_b07":
        result["metrics"].update({
            "vector_status": "ready",
            "index_generation": 1,
            "faiss_latency_ms": 2.0,
            "fallback_source": "faiss",
            "outbox_pending_count": 0,
        })
    elif scenario == "background_b08":
        result["metrics"].update({
            "background_active": 1,
            "queue_wait_ms": 1.0,
            "execution_timeout": False,
            "late_completed": 0,
        })
    return result


async def _run(output_root: Path) -> dict:
    config = HostProbeConfig(event_adapter="offline_mock_host_probe:adapter")
    with patch("tests.manual.astrmai_host_probe._load_adapter", return_value=adapter):
        return await run_host_probe(
            output_root=output_root,
            config=config,
            measurement_scope="offline_mock",
        )


async def adapter(payload: dict) -> dict:
    return _mock_result(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all Host probe scenarios with offline Mock data.")
    parser.add_argument("--output-dir", default="artifacts/live_validation")
    args = parser.parse_args()
    payload = asyncio.run(_run(Path(args.output_dir).resolve()))
    print({"run_id": payload["run_id"], "status": payload["status"], "measurement_scope": payload["measurement_scope"]})
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
