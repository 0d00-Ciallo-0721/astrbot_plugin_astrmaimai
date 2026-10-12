"""pytest configuration — ensures the project root is on sys.path so that
``import astrmai`` works regardless of the working directory or PYTHONPATH."""

import sys
from pathlib import Path

import pytest

from tests.helpers.module_cache_isolation import isolated_module_cache

_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture(scope="session")
def after_nonebot_init() -> None:
    """Override nonebug's no-op async fixture, which leaks under pytest 8."""


@pytest.fixture(scope="session", autouse=True)
def isolate_host_data_root(tmp_path_factory):
    """Real cached AstrBot path functions must also resolve into test storage."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("ASTRBOT_ROOT", str(tmp_path_factory.mktemp("host-root")))
        yield


_class_module_isolation = None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Include unittest setUpClass/tearDownClass in the import rollback boundary."""
    global _class_module_isolation
    if item.cls is None:
        with isolated_module_cache():
            return (yield)
    if _class_module_isolation is None:
        _class_module_isolation = isolated_module_cache()
        _class_module_isolation.__enter__()
    try:
        return (yield)
    finally:
        if nextitem is None or nextitem.cls is not item.cls:
            context = _class_module_isolation
            _class_module_isolation = None
            context.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def restore_stub_modules_between_tests():
    """Keep class fixtures alive while rolling back each test's stub/reload changes."""
    with isolated_module_cache():
        yield


@pytest.fixture(autouse=True)
def reset_outbound_gate_between_tests():
    """Keep process-wide lifecycle state from leaking between isolated tests."""
    from astrmai.infrastructure.runtime.outbound_send_guard import OUTBOUND_SEND_GATE

    OUTBOUND_SEND_GATE.close()
    yield
    OUTBOUND_SEND_GATE.close()
