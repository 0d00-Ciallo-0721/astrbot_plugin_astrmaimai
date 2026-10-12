import importlib
import os
import sys
import types
from pathlib import Path

import pytest

from tests.helpers.module_cache_isolation import isolated_module_cache


def test_real_host_path_function_uses_isolated_root():
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    root = Path(os.environ["ASTRBOT_ROOT"]).resolve()
    assert root != Path(__file__).resolve().parents[2]
    assert Path(get_astrbot_data_path()).resolve() == root / "data"


@pytest.mark.parametrize("abort", [None, RuntimeError, KeyboardInterrupt])
def test_stub_modules_and_parent_attributes_are_restored(monkeypatch, abort):
    import astrbot.api as api

    original = importlib.import_module("astrbot.api.message_components")
    monkeypatch.delitem(sys.modules, "astrmai._isolation_probe", raising=False)
    before = dict(sys.modules)
    missing = object()
    parent_before = getattr(api, "message_components", missing)

    def run():
        with isolated_module_cache():
            replacement = types.ModuleType("astrbot.api.message_components")
            sys.modules[replacement.__name__] = replacement
            api.message_components = replacement
            sys.modules["astrmai._isolation_probe"] = types.ModuleType("astrmai._isolation_probe")
            sys.modules.pop("astrbot.api.event", None)
            if abort:
                raise abort("fixture aborted")

    if abort:
        with pytest.raises(abort):
            run()
    else:
        run()

    assert sys.modules[original.__name__] is before[original.__name__]
    assert ("astrbot.api.event" in sys.modules) == ("astrbot.api.event" in before)
    if "astrbot.api.event" in before:
        assert sys.modules["astrbot.api.event"] is before["astrbot.api.event"]
    assert "astrmai._isolation_probe" not in sys.modules
    assert getattr(api, "message_components", missing) is parent_before
