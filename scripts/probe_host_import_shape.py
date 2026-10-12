"""Offline import/call probe using AstrBot's nested plugin package name.

Run with ``python -I -B scripts/probe_host_import_shape.py PLUGIN_ROOT``.
This checks package loading and the speaker block without initializing a plugin,
sending messages, or opening production storage. It is not a live host test.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace


def probe(plugin_root: Path) -> dict[str, object]:
    plugin_root = plugin_root.resolve(strict=True)
    if "astrmai" in sys.modules or importlib.util.find_spec("astrmai") is not None:
        raise RuntimeError("top-level astrmai must be unavailable; run with -I")
    if "data" in sys.modules or "data.plugins" in sys.modules:
        raise RuntimeError("probe requires a fresh process")

    # Only namespace parents are supplied. The real plugin __init__, main,
    # contracts and planning modules execute under their actual host names.
    for name, path in (
        ("data", plugin_root.parent.parent),
        ("data.plugins", plugin_root.parent),
    ):
        module = ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        sys.modules[name] = module
    sys.modules["data"].plugins = sys.modules["data.plugins"]
    prefix = "data.plugins.astrmai"
    spec = importlib.util.spec_from_file_location(
        prefix, plugin_root / "__init__.py",
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin package cannot be loaded")
    package = importlib.util.module_from_spec(spec)
    sys.modules[prefix] = package
    sys.modules["data.plugins"].astrmai = package
    spec.loader.exec_module(package)

    main = importlib.import_module(f"{prefix}.main")
    prompt = importlib.import_module(
        f"{prefix}.astrmai.conversation.planning.planner_prompt_context"
    )
    targets = importlib.import_module(f"{prefix}.astrmai.conversation.contracts.turn_target")
    contexts = importlib.import_module(f"{prefix}.astrmai.conversation.contracts.focus_context")
    event = SimpleNamespace(
        unified_msg_origin="probe:GroupMessage:123",
        message_str="synthetic reply and mention",
        get_sender_id=lambda: "111",
        get_sender_name=lambda: "用户A",
        get_group_id=lambda: "123",
        get_extra=lambda _key, default=None: default,
    )
    context = contexts.FocusThreadContext(
        focus_event=event,
        actor_set=targets.ActorSet(
            current_actor_id="111", quoted_actor_ids=("222",), at_actor_ids=("333",),
        ),
        turn_target=targets.TurnTarget(
            target_kind=targets.TargetKind.MESSAGE,
            target_actor_id="222", target_actor_name="用户B", target_event_id="probe-reply",
        ),
    )
    block = prompt.PlannerPromptContextMixin._build_current_speaker_block(
        event, context, is_lightweight_event=False,
    )
    assert "实际发言人：用户A(QQ:111)" in block, block
    assert "该消息引用了：用户B(QQ:222)" in block, block
    assert "同时@了：QQ:333" in block, block
    assert "实际发言人：用户B" not in block, block
    assert hasattr(main, "AstrMaiPlugin")
    assert "astrmai" not in sys.modules
    assert importlib.util.find_spec("astrmai") is None
    assert Path(prompt.__file__).resolve() == (
        plugin_root / "astrmai/conversation/planning/planner_prompt_context.py"
    ).resolve()
    return {
        "measurement_scope": "offline_host_import_shape",
        "passed": True,
        "python": sys.version,
        "astrbot": importlib.metadata.version("astrbot"),
        "plugin_root": str(plugin_root),
        "module": prompt.__name__,
        "function_called": "PlannerPromptContextMixin._build_current_speaker_block",
        "prompt_module_sha256": hashlib.sha256(Path(prompt.__file__).read_bytes()).hexdigest(),
        "top_level_astrmai_available": False,
        "speaker_block": block,
        "live_host_verified": False,
        "message_sent": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_root", type=Path)
    args = parser.parse_args()
    # AstrBot imports may create default storage; keep it away from live data.
    with tempfile.TemporaryDirectory(prefix="astrmai-import-probe-") as root:
        os.environ["ASTRBOT_ROOT"] = root
        result = probe(args.plugin_root)
    print("ASTRMAI_IMPORT_SHAPE_REPORT=" + json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
