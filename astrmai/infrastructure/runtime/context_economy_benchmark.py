from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_SAMPLE_FILENAME = "context_economy_benchmark_samples.jsonl"


@dataclass
class _MetricAccumulator:
    key: str
    call_count: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    provider_session_enabled_count: int = 0
    provider_session_reused_count: int = 0
    primary_hit_count: int = 0
    lane_rotate_count: int = 0
    rotate_reasons: dict[str, int] = field(default_factory=dict)
    stable_prefix_length_total: int = 0
    dynamic_payload_length_total: int = 0
    actual_models: dict[str, int] = field(default_factory=dict)
    workload_families: dict[str, int] = field(default_factory=dict)
    seen_provider_session_ids: set[str] = field(default_factory=set)

    def add(self, sample: dict[str, Any]) -> None:
        self.call_count += 1
        input_tokens = int(sample.get("input_tokens", 0) or 0)
        cached_input_tokens = int(sample.get("cached_input_tokens", 0) or 0)
        output_tokens = int(sample.get("output_tokens", 0) or 0)
        total_tokens = int(sample.get("total_tokens", 0) or (input_tokens + output_tokens))
        self.input_tokens += input_tokens
        self.cached_input_tokens += cached_input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += total_tokens
        session_id = str(sample.get("provider_session_id", "") or "")
        if session_id:
            self.provider_session_enabled_count += 1
            if session_id in self.seen_provider_session_ids:
                self.provider_session_reused_count += 1
            else:
                self.seen_provider_session_ids.add(session_id)
        if bool(sample.get("primary_hit", False)):
            self.primary_hit_count += 1
        if bool(sample.get("lane_rotated", False)):
            self.lane_rotate_count += 1
        for reason in _split_rotate_reasons(sample.get("lane_rotate_reason", "")):
            self.rotate_reasons[reason] = self.rotate_reasons.get(reason, 0) + 1
        self.stable_prefix_length_total += int(sample.get("stable_prefix_length", 0) or 0)
        self.dynamic_payload_length_total += int(sample.get("dynamic_payload_length", 0) or 0)
        model_id = str(sample.get("model_id", "") or "")
        if model_id:
            self.actual_models[model_id] = self.actual_models.get(model_id, 0) + 1
        workload_family = str(sample.get("workload_family", "") or "")
        if workload_family:
            self.workload_families[workload_family] = self.workload_families.get(workload_family, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        calls = max(self.call_count, 1)
        return {
            "call_count": self.call_count,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_ratio": round(self.cached_input_tokens / max(self.input_tokens, 1), 4),
            "provider_session_enabled_rate": round(self.provider_session_enabled_count / calls, 4),
            "provider_session_reuse_rate": round(self.provider_session_reused_count / calls, 4),
            "primary_hit_rate": round(self.primary_hit_count / calls, 4),
            "lane_rotate_rate": round(self.lane_rotate_count / calls, 4),
            "lane_rotate_count": self.lane_rotate_count,
            "avg_stable_prefix_length": round(self.stable_prefix_length_total / calls, 2),
            "avg_dynamic_payload_length": round(self.dynamic_payload_length_total / calls, 2),
            "rotate_reasons": dict(self.rotate_reasons),
            "actual_models": dict(self.actual_models),
            "workload_families": dict(self.workload_families),
        }


def resolve_sample_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_dir():
        return candidate / DEFAULT_SAMPLE_FILENAME
    return candidate


def load_benchmark_samples(
    sample_path: str | Path,
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    workload_family: str = "",
    template_filter: str = "",
) -> list[dict[str, Any]]:
    path = resolve_sample_path(sample_path)
    if not path.exists():
        return []
    workload_family = str(workload_family or "").strip()
    template_filter = str(template_filter or "").strip().lower()
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                sample = json.loads(raw)
            except Exception:
                continue
            if not isinstance(sample, dict):
                continue
            created_at = float(sample.get("created_at", 0.0) or 0.0)
            if start_ts is not None and created_at < float(start_ts):
                continue
            if end_ts is not None and created_at > float(end_ts):
                continue
            if workload_family and str(sample.get("workload_family", "") or "") != workload_family:
                continue
            template_key = str(sample.get("template_key", "") or "")
            template_id = str(sample.get("template_id", "") or "")
            if template_filter and template_filter not in template_key.lower() and template_filter not in template_id.lower():
                continue
            items.append(sample)
    return items


def aggregate_benchmark_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    overview = _MetricAccumulator("overview")
    by_family: dict[str, _MetricAccumulator] = {}
    by_template: dict[str, _MetricAccumulator] = {}

    for sample in samples:
        overview.add(sample)
        family = str(sample.get("workload_family", "") or "unknown")
        template_key = str(
            sample.get("template_key", "")
            or f"{str(sample.get('template_id', '') or 'unknown')}@{str(sample.get('template_version', '') or 'v1')}"
        )
        by_family.setdefault(family, _MetricAccumulator(family)).add(sample)
        by_template.setdefault(template_key, _MetricAccumulator(template_key)).add(sample)

    family_metrics = {key: metric.as_dict() for key, metric in sorted(by_family.items())}
    template_metrics = {key: metric.as_dict() for key, metric in sorted(by_template.items())}
    template_items = [
        {"template_key": key, **value}
        for key, value in template_metrics.items()
    ]
    return {
        "overview": overview.as_dict(),
        "by_workload_family": family_metrics,
        "by_template": template_metrics,
        "high_rotate_templates": _sort_template_items(template_items, sort_by="rotate")[:10],
        "low_reuse_templates": _sort_template_items(template_items, sort_by="session_reuse")[:10],
        "high_traffic_templates": _sort_template_items(template_items, sort_by="calls")[:10],
    }


def build_benchmark_meta(
    *,
    sample_path: str | Path,
    sample_count: int,
    start_ts: float | None = None,
    end_ts: float | None = None,
    workload_family: str = "",
    template_filter: str = "",
) -> dict[str, Any]:
    return {
        "sample_path": str(resolve_sample_path(sample_path)),
        "sample_count": int(sample_count),
        "generated_at": time.time(),
        "filters": {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "workload_family": str(workload_family or ""),
            "template_filter": str(template_filter or ""),
        },
    }


def write_benchmark_artifacts(
    *,
    output_root: str | Path,
    summary: dict[str, Any],
    meta: dict[str, Any],
    label: str = "baseline",
    repo_root: str | Path | None = None,
) -> Path:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    git_sha = _git_short_sha(repo_root)
    safe_label = str(label or "baseline").strip().replace(" ", "_")
    run_id = f"{timestamp}-{git_sha or 'nogit'}-{safe_label}"
    run_dir = Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "samples_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "benchmark_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "benchmark_summary.md").write_text(render_benchmark_markdown(summary, meta, run_id=run_id), encoding="utf-8")
    return run_dir


def render_benchmark_markdown(summary: dict[str, Any], meta: dict[str, Any], *, run_id: str) -> str:
    overview = dict(summary.get("overview", {}) or {})
    lines: list[str] = [
        f"# Context Economy Benchmark Baseline",
        "",
        f"- Run ID: `{run_id}`",
        f"- Sample Count: `{int(meta.get('sample_count', 0) or 0)}`",
        f"- Sample Path: `{meta.get('sample_path', '')}`",
        "",
        "## Overview",
        "",
        f"- Total Calls: `{overview.get('call_count', 0)}`",
        f"- Input Tokens: `{overview.get('input_tokens', 0)}`",
        f"- Cached Input Tokens: `{overview.get('cached_input_tokens', 0)}`",
        f"- Output Tokens: `{overview.get('output_tokens', 0)}`",
        f"- Total Tokens: `{overview.get('total_tokens', 0)}`",
        f"- Cached Input Ratio: `{_fmt_pct(overview.get('cached_input_ratio', 0.0))}`",
        f"- Session Reuse Rate: `{_fmt_pct(overview.get('provider_session_reuse_rate', 0.0))}`",
        f"- Primary Hit Rate: `{_fmt_pct(overview.get('primary_hit_rate', 0.0))}`",
        f"- Lane Rotate Rate: `{_fmt_pct(overview.get('lane_rotate_rate', 0.0))}`",
        "",
        "## By Workload Family",
        "",
    ]
    family_metrics = dict(summary.get("by_workload_family", {}) or {})
    if family_metrics:
        for family, item in family_metrics.items():
            lines.extend(
                [
                    f"### {family}",
                    f"- Calls: `{item.get('call_count', 0)}`",
                    f"- Total Tokens: `{item.get('total_tokens', 0)}`",
                    f"- Cached Input Ratio: `{_fmt_pct(item.get('cached_input_ratio', 0.0))}`",
                    f"- Session Reuse Rate: `{_fmt_pct(item.get('provider_session_reuse_rate', 0.0))}`",
                    f"- Primary Hit Rate: `{_fmt_pct(item.get('primary_hit_rate', 0.0))}`",
                    f"- Lane Rotate Rate: `{_fmt_pct(item.get('lane_rotate_rate', 0.0))}`",
                    "",
                ]
            )
    else:
        lines.extend(["_No workload family data._", ""])

    lines.extend(["## By Template", ""])
    template_metrics = dict(summary.get("by_template", {}) or {})
    if template_metrics:
        for template_key, item in template_metrics.items():
            lines.extend(
                [
                    f"### {template_key}",
                    f"- Calls: `{item.get('call_count', 0)}`",
                    f"- Total Tokens: `{item.get('total_tokens', 0)}`",
                    f"- Cached Input Ratio: `{_fmt_pct(item.get('cached_input_ratio', 0.0))}`",
                    f"- Session Reuse Rate: `{_fmt_pct(item.get('provider_session_reuse_rate', 0.0))}`",
                    f"- Primary Hit Rate: `{_fmt_pct(item.get('primary_hit_rate', 0.0))}`",
                    f"- Lane Rotate Count: `{item.get('lane_rotate_count', 0)}`",
                    "",
                ]
            )
    else:
        lines.extend(["_No template data._", ""])

    for title, key in (
        ("High Rotate Templates", "high_rotate_templates"),
        ("Low Reuse Templates", "low_reuse_templates"),
        ("High Traffic Templates", "high_traffic_templates"),
    ):
        lines.extend([f"## {title}", ""])
        items = list(summary.get(key, []) or [])
        if not items:
            lines.extend(["_No template samples._", ""])
            continue
        for item in items:
            lines.append(
                f"- `{item.get('template_key', '')}` | calls `{item.get('call_count', 0)}` | "
                f"rotate `{item.get('lane_rotate_count', 0)}` | reuse `{_fmt_pct(item.get('provider_session_reuse_rate', 0.0))}`"
            )
        lines.append("")
    return "\n".join(lines)


def _sort_template_items(items: list[dict[str, Any]], *, sort_by: str) -> list[dict[str, Any]]:
    def _key(item: dict[str, Any]) -> tuple:
        rotates = int(item.get("lane_rotate_count", 0) or 0)
        calls = int(item.get("call_count", 0) or 0)
        reuse = float(item.get("provider_session_reuse_rate", 0.0) or 0.0)
        template_key = str(item.get("template_key", "") or "")
        if sort_by == "session_reuse":
            return (reuse, -rotates, -calls, template_key)
        if sort_by == "calls":
            return (-calls, -rotates, reuse, template_key)
        return (-rotates, reuse, -calls, template_key)

    return sorted(items, key=_key)


def _split_rotate_reasons(raw: str) -> list[str]:
    parts: list[str] = []
    for part in str(raw or "").split(","):
        clean = part.strip()
        if clean and clean not in parts:
            parts.append(clean)
    return parts


def _fmt_pct(value: float) -> str:
    return f"{float(value or 0.0) * 100:.1f}%"


def _git_short_sha(repo_root: str | Path | None) -> str:
    if repo_root is None:
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


__all__ = [
    "DEFAULT_SAMPLE_FILENAME",
    "aggregate_benchmark_samples",
    "build_benchmark_meta",
    "load_benchmark_samples",
    "render_benchmark_markdown",
    "resolve_sample_path",
    "write_benchmark_artifacts",
]
