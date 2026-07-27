"""G8/WU-06: 把 legacy 整文件 turn_trace_samples.json 转成 append-only JSONL。

运行时首次写入会自动迁移（`TurnTraceSampleStore._migrate_legacy_sync`），本脚本用于
手工转换归档快照（如 `.agent/runtime-observability-*/turn_trace_samples_server.json`），
方便用新格式工具链统一分析。

用法：
    PYTHONIOENCODING=utf-8 python scripts/convert_turn_trace_to_jsonl.py <input.json> [output.jsonl]

不修改输入文件；输出默认与输入同名换 .jsonl 后缀。同 turn_id 的样本按「后写覆盖先写」合并。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _iter_samples(payload) -> list[dict]:
    samples: list[dict] = []
    if isinstance(payload, list):
        samples.extend(item for item in payload if isinstance(item, dict))
        return samples
    if not isinstance(payload, dict):
        return samples
    by_chat = payload.get("by_chat") or {}
    if isinstance(by_chat, dict):
        for items in by_chat.values():
            samples.extend(item for item in list(items or []) if isinstance(item, dict))
    recent = payload.get("recent") or []
    if isinstance(recent, list):
        samples.extend(item for item in recent if isinstance(item, dict))
    return samples


def _dedupe_by_turn(samples: list[dict]) -> list[dict]:
    by_turn: dict[str, int] = {}
    result: list[dict] = []
    for item in samples:
        turn_id = str(item.get("turn_id", "") or "")
        if not turn_id:
            result.append(item)
            continue
        if turn_id in by_turn:
            result[by_turn[turn_id]] = item
            continue
        by_turn[turn_id] = len(result)
        result.append(item)
    return result


def convert(input_path: Path, output_path: Path) -> int:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    samples = _dedupe_by_turn(_iter_samples(payload))
    with output_path.open("w", encoding="utf-8") as handle:
        for item in samples:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="legacy turn_trace_samples.json path")
    parser.add_argument("output", nargs="?", default="", help="output .jsonl path")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"input not found: {input_path}")
        return 2
    output_path = Path(args.output) if args.output else input_path.with_suffix(".jsonl")
    count = convert(input_path, output_path)
    print(f"converted {count} samples -> {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
