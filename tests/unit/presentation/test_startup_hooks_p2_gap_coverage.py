import asyncio
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class StartupHooksP2GapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_on_program_start_delegates_to_lifecycle_manager(self):
        from astrmai.presentation.events.startup_hooks import on_program_start

        class _Lifecycle:
            def __init__(self):
                self.called = False

            async def on_program_start(self, *, source: str = ""):
                self.called = True

        lifecycle = _Lifecycle()

        asyncio.run(on_program_start(object(), lifecycle))

        self.assertTrue(lifecycle.called)

    def test_on_program_start_propagates_lifecycle_failure(self):
        from astrmai.presentation.events.startup_hooks import on_program_start
        import astrmai.presentation.events.startup_hooks as startup_hooks_mod

        if not hasattr(startup_hooks_mod.logger, "exception"):
            startup_hooks_mod.logger.exception = startup_hooks_mod.logger.error

        class _Lifecycle:
            async def on_program_start(self, *, source: str = ""):
                raise RuntimeError("startup failed")

        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            asyncio.run(on_program_start(object(), _Lifecycle()))


if __name__ == "__main__":
    unittest.main()
