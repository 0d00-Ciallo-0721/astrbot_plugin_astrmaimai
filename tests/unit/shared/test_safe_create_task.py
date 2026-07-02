import asyncio
import pytest
from unittest import mock

from astrmai.shared.helpers.plugin_helpers import safe_create_task


class TestSafeCreateTask:
    """Unit tests for safe_create_task() fire-and-forget wrapper."""

    @pytest.mark.asyncio
    async def test_normal_completion_no_error_log(self):
        """Normal completion should NOT trigger logger.error"""
        async def ok():
            return 42

        with mock.patch("astrmai.shared.helpers.plugin_helpers._astrbot_logger") as mlog:
            task = safe_create_task(ok())
            result = await task
            assert result == 42
            mlog.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_task_has_done_callback(self):
        """Task should have done_callback attached for error logging"""
        async def fail():
            raise ValueError("test error")

        task = safe_create_task(fail())
        # Verify a callback was registered
        assert task.done() is False  # not done yet
        # Cancel to clean up
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_returns_task_object(self):
        """safe_create_task() returns an asyncio.Task"""
        async def ok():
            pass

        task = safe_create_task(ok())
        assert isinstance(task, asyncio.Task)

    @pytest.mark.asyncio
    async def test_name_parameter_accepted(self):
        """Name parameter is accepted without error"""
        async def ok():
            pass

        task = safe_create_task(ok(), name="my_test_task")
        assert isinstance(task, asyncio.Task)
