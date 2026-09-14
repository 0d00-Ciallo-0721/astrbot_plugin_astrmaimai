from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import threading
import types
import unittest

from tests.helpers.local_acceptance_isolation import (
    LocalAcceptanceIsolationGuard,
    NetworkIsolationError,
    ProcessIsolationError,
)


EXTERNAL_HOST = "203.0.113.7"


def _contains_isolation_error(error: BaseException) -> bool:
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, NetworkIsolationError):
            return True
        for related in (
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
            getattr(current, "reason", None),
        ):
            if isinstance(related, BaseException):
                pending.append(related)
        pending.extend(arg for arg in current.args if isinstance(arg, BaseException))
    return False


class LocalAcceptanceIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = LocalAcceptanceIsolationGuard()
        self.guard.install(enforce_import_order=False)

    def tearDown(self) -> None:
        self.guard.uninstall()

    def test_sync_socket_non_loopback_is_blocked_before_connect(self) -> None:
        sock = socket.socket()
        self.addCleanup(sock.close)
        with self.assertRaises(NetworkIsolationError):
            sock.connect((EXTERNAL_HOST, 443))
        snapshot = self.guard.snapshot()
        self.assertEqual(snapshot["successful_non_loopback_connections"], 0)
        self.assertEqual(snapshot["blocked_non_loopback_attempts"], 1)
        self.assertEqual(snapshot["attempts"][0]["phase"], "socket.connect")

    def test_selector_event_loop_non_loopback_is_blocked(self) -> None:
        loop = asyncio.SelectorEventLoop()
        self.addCleanup(loop.close)
        with self.assertRaises(NetworkIsolationError):
            loop.run_until_complete(
                loop.create_connection(asyncio.Protocol, EXTERNAL_HOST, 443)
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows Proactor only")
    def test_windows_proactor_low_level_connect_is_blocked(self) -> None:
        from asyncio import windows_events

        proactor = windows_events.IocpProactor()
        self.addCleanup(proactor.close)
        with self.assertRaises(NetworkIsolationError):
            asyncio.run(proactor.connect(None, (EXTERNAL_HOST, 443)))
        snapshot = self.guard.snapshot()
        self.assertTrue(snapshot["proactor_guard_installed"])
        self.assertEqual(snapshot["attempts"][-1]["phase"], "proactor.connect")

    def test_ipv4_localhost_connection_is_allowed(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname(), timeout=1)
        self.addCleanup(client.close)
        accepted, _ = listener.accept()
        accepted.close()
        self.assertEqual(self.guard.snapshot()["blocked_non_loopback_attempts"], 0)

    def test_ipv6_localhost_connection_is_allowed(self) -> None:
        if not socket.has_ipv6:
            self.skipTest("IPv6 unavailable")
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        try:
            listener.bind(("::1", 0))
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        listener.listen(1)
        client = socket.create_connection(
            ("::1", listener.getsockname()[1]), timeout=1
        )
        self.addCleanup(client.close)
        accepted, _ = listener.accept()
        accepted.close()
        self.assertEqual(self.guard.snapshot()["blocked_non_loopback_attempts"], 0)

    def test_external_getaddrinfo_is_blocked_before_dns(self) -> None:
        with self.assertRaises(NetworkIsolationError):
            socket.getaddrinfo("example.invalid", 443)
        self.assertEqual(
            self.guard.snapshot()["attempts"][-1]["phase"],
            "socket.getaddrinfo",
        )

    def test_thread_inherits_process_wide_network_guard(self) -> None:
        errors: list[BaseException] = []

        def connect() -> None:
            try:
                socket.create_connection((EXTERNAL_HOST, 443), timeout=0.1)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=connect)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], NetworkIsolationError)

    def test_urllib_httpx_requests_and_aiohttp_cannot_bypass_guard(self) -> None:
        old_proxy = {
            key: os.environ.pop(key)
            for key in tuple(os.environ)
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}
        }
        self.addCleanup(self._restore_proxy_environment, old_proxy)

        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(Exception) as urllib_error:
            opener.open(f"http://{EXTERNAL_HOST}:9/", timeout=0.1)
        self.assertTrue(_contains_isolation_error(urllib_error.exception))

        try:
            import httpx
        except ImportError:
            self.fail("httpx is required by the AstrBot acceptance environment")
        with httpx.Client(trust_env=False) as client:
            with self.assertRaises(Exception) as httpx_error:
                client.get(f"http://{EXTERNAL_HOST}:9/", timeout=0.1)
        self.assertTrue(_contains_isolation_error(httpx_error.exception))

        try:
            import requests
        except ImportError:
            self.fail("requests is required by the AstrBot acceptance environment")
        request_session = requests.Session()
        request_session.trust_env = False
        self.addCleanup(request_session.close)
        with self.assertRaises(Exception) as requests_error:
            request_session.get(f"http://{EXTERNAL_HOST}:9/", timeout=0.1)
        self.assertTrue(_contains_isolation_error(requests_error.exception))

        try:
            import aiohttp
        except ImportError:
            self.fail("aiohttp is required by the AstrBot acceptance environment")

        async def fetch() -> None:
            async with aiohttp.ClientSession(trust_env=False) as session:
                await session.get(f"http://{EXTERNAL_HOST}:9/")

        with self.assertRaises(Exception) as aiohttp_error:
            asyncio.run(fetch())
        self.assertTrue(_contains_isolation_error(aiohttp_error.exception))
        self.assertEqual(
            self.guard.snapshot()["successful_non_loopback_connections"], 0
        )

    def test_sensitive_import_before_install_is_rejected(self) -> None:
        self.guard.uninstall()
        marker = types.ModuleType("astrbot")
        original = sys.modules.get("astrbot")
        sys.modules["astrbot"] = marker
        try:
            with self.assertRaisesRegex(RuntimeError, "before sensitive imports"):
                self.guard.install(enforce_import_order=True)
        finally:
            if original is None:
                sys.modules.pop("astrbot", None)
            else:
                sys.modules["astrbot"] = original

    def test_guard_must_be_installed_before_event_loop_runs(self) -> None:
        self.guard.uninstall()

        async def install_too_late() -> None:
            late_guard = LocalAcceptanceIsolationGuard()
            with self.assertRaisesRegex(RuntimeError, "before an asyncio event loop"):
                late_guard.install(
                    enforce_import_order=False,
                    require_no_running_loop=True,
                )

        asyncio.run(install_too_late())

    def test_visual_metadata_initialization_cannot_bypass_installed_guard(self) -> None:
        def initialize_visual_metadata() -> None:
            socket.create_connection(("metadata.example.invalid", 443))

        self.assertTrue(self.guard.installed)
        with self.assertRaises(NetworkIsolationError):
            initialize_visual_metadata()
        self.assertEqual(
            self.guard.snapshot()["attempts"][-1]["host"],
            "metadata.example.invalid",
        )

    def test_diagnostics_include_phase_and_redact_credentials(self) -> None:
        sock = socket.socket()
        self.addCleanup(sock.close)
        with self.assertRaises(NetworkIsolationError):
            sock.connect(("example.invalid?token=do-not-log", 443))
        attempt = self.guard.snapshot()["attempts"][-1]
        self.assertEqual(attempt["host"], "example.invalid?token=<redacted>")
        self.assertNotIn("do-not-log", repr(attempt))
        self.assertTrue(attempt["stack"])

    def test_child_process_creation_is_blocked_before_spawn(self) -> None:
        with self.assertRaises(ProcessIsolationError):
            subprocess.Popen([sys.executable, "-c", "print('must-not-run')"])
        with self.assertRaises(ProcessIsolationError):
            asyncio.run(asyncio.create_subprocess_exec(sys.executable, "-V"))
        snapshot = self.guard.snapshot()
        self.assertEqual(snapshot["blocked_process_attempts"], 2)

    def test_twenty_fresh_guard_cycles_block_without_success(self) -> None:
        self.guard.uninstall()
        for round_number in range(20):
            guard = LocalAcceptanceIsolationGuard()
            guard.install(enforce_import_order=False)
            try:
                sock = socket.socket()
                try:
                    with self.assertRaises(NetworkIsolationError, msg=f"round={round_number}"):
                        sock.connect((EXTERNAL_HOST, 443))
                finally:
                    sock.close()
                snapshot = guard.snapshot()
                self.assertEqual(
                    snapshot["successful_non_loopback_connections"],
                    0,
                    msg=f"round={round_number}",
                )
                self.assertEqual(
                    snapshot["blocked_non_loopback_attempts"],
                    1,
                    msg=f"round={round_number}",
                )
            finally:
                guard.uninstall()

    @staticmethod
    def _restore_proxy_environment(old_proxy: dict[str, str | None]) -> None:
        for key, value in old_proxy.items():
            if value is not None:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
