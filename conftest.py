"""pytest configuration — ensures the project root is on sys.path so that
``import astrmai`` works regardless of the working directory or PYTHONPATH."""

import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture(scope="session")
def after_nonebot_init() -> None:
    """Override nonebug's no-op async fixture, which leaks under pytest 8."""


@pytest.fixture(autouse=True)
def reset_outbound_gate_between_tests():
    """Keep process-wide lifecycle state from leaking between isolated tests."""
    from astrmai.infrastructure.runtime.outbound_send_guard import OUTBOUND_SEND_GATE

    OUTBOUND_SEND_GATE.close()
    yield
    OUTBOUND_SEND_GATE.close()
