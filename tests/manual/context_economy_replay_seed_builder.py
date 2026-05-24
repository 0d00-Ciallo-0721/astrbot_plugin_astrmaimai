from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrmai.infrastructure.context_economy import PromptTemplateId, PromptTemplateRegistry


def _render_version(registry: PromptTemplateRegistry, template_id: PromptTemplateId, payload: dict) -> str:
    return registry.render_template(template_id.value, payload).template_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build simulated Context Economy benchmark samples from replay artifacts.")
    parser.add_argument("--replay-root", default="artifacts/kimi_replay", help="Directory containing replay run folders.")
    parser.add_argument(
        "--output",
        default="artifacts/context_economy_replay_seed_samples_multi_round.jsonl",
        help="Output JSONL path for simulated benchmark samples.",
    )
    return parser


def _dialog_session_id(run_id: str, case_id: str) -> str:
    case_key = str(case_id or "").lower()
    if "private" in case_key:
        return f"chat-private-{run_id}"
    return f"chat-group-{run_id}"


def _persona_session_id() -> str:
    return "persona-global"


def _memory_session_id(case_id: str) -> str:
    case_key = str(case_id or "").lower()
    if "memory" in case_key:
        return "memory-shared"
    return "memory-generic"


def _dream_session_id() -> str:
    return "dream-shared"


def _compaction_session_id() -> str:
    return "compaction-shared"


def build_samples_from_replay(replay_root: Path) -> tuple[list[dict], dict[str, str]]:
    registry = PromptTemplateRegistry()

    persona_version = _render_version(
        registry,
        PromptTemplateId.PERSONA_CORE_IDENTITY,
        {"original_prompt": "persona", "cache_key": "persona-1"},
    )
    compaction_version = _render_version(
        registry,
        PromptTemplateId.COMPACTION_SUMMARY_V2,
        {"lines_text": "- a\n- b"},
    )
    dream_version = _render_version(
        registry,
        PromptTemplateId.DREAM_GENERATION,
        {"persona_name": "Mai", "style": "brief", "dream_log": "A short dream."},
    )
    memory_version = _render_version(
        registry,
        PromptTemplateId.MEMORY_GLOBAL_SUMMARY,
        {"history": "one\ntwo\nthree"},
    )
    memory_extract_version = _render_version(
        registry,
        PromptTemplateId.MEMORY_STRUCTURED_EXTRACTION,
        {"history": "one\ntwo\nthree"},
    )

    samples: list[dict] = []
    clock = 1715997600.0

    def add_sample(**kwargs):
        nonlocal clock
        kwargs.setdefault("provider_family", "moonshot")
        kwargs.setdefault("model_id", "kimi-k2.6")
        kwargs.setdefault("primary_hit", True)
        kwargs.setdefault("fallback_used", False)
        kwargs.setdefault("created_at", clock)
        kwargs.setdefault("source_run_id", "simulated-replay-multi-round")
        kwargs.setdefault("lane_rotate_reason", "")
        kwargs.setdefault("lane_rotated", False)
        kwargs.setdefault("stable_prefix_length", 0)
        kwargs.setdefault("dynamic_payload_length", 0)
        kwargs.setdefault("cached_input_tokens", 0)
        kwargs.setdefault("output_tokens", 0)
        kwargs.setdefault("total_tokens", int(kwargs.get("input_tokens", 0) or 0) + int(kwargs.get("output_tokens", 0) or 0))
        kwargs.setdefault("template_key", str(kwargs.get("template_id", "unknown")) + "@" + str(kwargs.get("template_version", "v1")))
        samples.append(kwargs)
        clock += 17.0

    for source_dir in sorted(replay_root.iterdir()):
        if not source_dir.is_dir():
            continue
        report_path = source_dir / "report.jsonl"
        if not report_path.exists():
            continue
        run_id = source_dir.name
        case_rows: list[dict] = []
        for raw in report_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except Exception:
                continue
            if row.get("kind") == "case":
                case_rows.append(row)

        for idx, row in enumerate(case_rows):
            case_id = str(row.get("case_id", "case"))
            preview = str((row.get("reply_preview") or row.get("reply") or ""))
            preview_len = max(len(preview), 8)
            if "memory" in case_id or "deep_memory" in case_id:
                session = _memory_session_id(case_id)
                add_sample(
                    source_run_id=run_id,
                    workload_family="memory_global_summary",
                    template_id="memory_global_summary",
                    template_version=memory_version,
                    input_tokens=34 + preview_len // 3,
                    cached_input_tokens=12 + idx,
                    output_tokens=16 + preview_len // 8,
                    provider_session_id=session,
                    stable_prefix_length=420,
                    dynamic_payload_length=110 + preview_len,
                )
                add_sample(
                    source_run_id=run_id,
                    workload_family="memory_global_summary",
                    template_id="memory_global_summary",
                    template_version=memory_version,
                    input_tokens=32 + preview_len // 3,
                    cached_input_tokens=13 + idx,
                    output_tokens=15 + preview_len // 8,
                    provider_session_id=session,
                    stable_prefix_length=420,
                    dynamic_payload_length=104 + preview_len,
                )
                add_sample(
                    source_run_id=run_id,
                    workload_family="memory_structured_extraction",
                    template_id="memory_structured_extraction",
                    template_version=memory_extract_version,
                    input_tokens=22 + preview_len // 4,
                    cached_input_tokens=8,
                    output_tokens=10 + preview_len // 10,
                    provider_session_id=session,
                    stable_prefix_length=360,
                    dynamic_payload_length=88 + preview_len,
                )
                add_sample(
                    source_run_id=run_id,
                    workload_family="memory_structured_extraction",
                    template_id="memory_structured_extraction",
                    template_version=memory_extract_version,
                    input_tokens=21 + preview_len // 4,
                    cached_input_tokens=8,
                    output_tokens=11 + preview_len // 10,
                    provider_session_id=session,
                    stable_prefix_length=360,
                    dynamic_payload_length=84 + preview_len,
                )
            elif "tool" in case_id:
                session = _persona_session_id()
                add_sample(
                    source_run_id=run_id,
                    workload_family="persona_summary",
                    template_id="persona_core_identity",
                    template_version=persona_version,
                    input_tokens=28 + preview_len // 4,
                    cached_input_tokens=6,
                    output_tokens=13 + preview_len // 9,
                    provider_session_id=session,
                    stable_prefix_length=452,
                    dynamic_payload_length=95 + preview_len,
                )
                add_sample(
                    source_run_id=run_id,
                    workload_family="persona_summary",
                    template_id="persona_core_identity",
                    template_version=persona_version,
                    input_tokens=27 + preview_len // 5,
                    cached_input_tokens=7,
                    output_tokens=12 + preview_len // 10,
                    provider_session_id=session,
                    stable_prefix_length=452,
                    dynamic_payload_length=91 + preview_len,
                )
            else:
                session = _dialog_session_id(run_id, case_id)
                add_sample(
                    source_run_id=run_id,
                    workload_family="chat_dialog",
                    template_id="chat_dialog",
                    template_version="v1",
                    input_tokens=18 + preview_len // 5,
                    cached_input_tokens=7 if idx % 2 == 1 else 0,
                    output_tokens=11 + preview_len // 10,
                    provider_session_id=session,
                    stable_prefix_length=310,
                    dynamic_payload_length=86 + preview_len,
                )
                if "group_non_direct" not in case_id:
                    add_sample(
                        source_run_id=run_id,
                        workload_family="chat_dialog",
                        template_id="chat_dialog",
                        template_version="v1",
                        input_tokens=17 + preview_len // 6,
                        cached_input_tokens=6,
                        output_tokens=10 + preview_len // 11,
                        provider_session_id=session,
                        stable_prefix_length=310,
                        dynamic_payload_length=80 + preview_len,
                    )

        compact_session = _compaction_session_id()
        add_sample(
            source_run_id=run_id,
            workload_family="compaction_summary",
            template_id="compaction_summary_v2",
            template_version=compaction_version,
            input_tokens=40,
            cached_input_tokens=18,
            output_tokens=12,
            provider_session_id=compact_session,
            stable_prefix_length=390,
            dynamic_payload_length=120,
        )
        add_sample(
            source_run_id=run_id,
            workload_family="compaction_summary",
            template_id="compaction_summary_v2",
            template_version=compaction_version,
            input_tokens=38,
            cached_input_tokens=18,
            output_tokens=11,
            provider_session_id=compact_session,
            stable_prefix_length=390,
            dynamic_payload_length=118,
        )
        add_sample(
            source_run_id=run_id,
            workload_family="dream_generation",
            template_id="dream_generation",
            template_version=dream_version,
            input_tokens=30,
            cached_input_tokens=10,
            output_tokens=20,
            provider_session_id=_dream_session_id(),
            stable_prefix_length=340,
            dynamic_payload_length=140,
        )
        add_sample(
            source_run_id=run_id,
            workload_family="dream_generation",
            template_id="dream_generation",
            template_version=dream_version,
            input_tokens=29,
            cached_input_tokens=10,
            output_tokens=18,
            provider_session_id=_dream_session_id(),
            stable_prefix_length=340,
            dynamic_payload_length=136,
        )

    return samples, {
        "persona_version": persona_version,
        "compaction_version": compaction_version,
        "dream_version": dream_version,
        "memory_version": memory_version,
        "memory_extract_version": memory_extract_version,
    }


def main() -> int:
    args = build_parser().parse_args()
    replay_root = (REPO_ROOT / args.replay_root).resolve()
    output_path = (REPO_ROOT / args.output).resolve()
    samples, versions = build_samples_from_replay(replay_root)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in samples) + "\n", encoding="utf-8")
    print(output_path)
    print(f"samples={len(samples)}")
    print(f"persona_version={versions['persona_version']}")
    print(f"compaction_version={versions['compaction_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
