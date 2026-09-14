from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from typing import Any, Callable


class NetworkIsolationError(PermissionError):
    """Raised before a non-loopback network connection can be attempted."""


class ProcessIsolationError(PermissionError):
    """Raised before the acceptance process can create a child process."""


@dataclass(frozen=True)
class IsolationAttempt:
    host: str
    port: str
    phase: str
    stack: tuple[str, ...]


def _redact(value: Any) -> str:
    text = str(value)
    text = re.sub(
        r"(?i)(token|api[_-]?key|secret|password)=([^&\s]+)",
        r"\1=<redacted>",
        text,
    )
    return re.sub(r"(?i)bearer\s+[^\s]+", "Bearer <redacted>", text)


def is_loopback_host(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    text = str(host).strip().strip("[]").rstrip(".").lower()
    if text == "localhost":
        return True
    if "%" in text:
        text = text.split("%", 1)[0]
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


class LocalAcceptanceIsolationGuard:
    """Process-wide localhost-only guard for the local acceptance harness."""

    SENSITIVE_IMPORT_PREFIXES = (
        "astrbot",
        "data.plugins.astrmai",
        "aiohttp",
        "httpx",
        "requests",
        "urllib.request",
    )

    def __init__(self) -> None:
        self._installed = False
        self._lock = threading.Lock()
        self._attempts: list[IsolationAttempt] = []
        self._process_attempts: list[dict[str, str]] = []
        self._successful_non_loopback_connections = 0
        self._installed_at_monotonic = 0.0
        self._proactor_guard_installed = False
        self._originals: dict[str, Any] = {}

    @property
    def installed(self) -> bool:
        return self._installed

    def install(
        self,
        *,
        enforce_import_order: bool = True,
        require_no_running_loop: bool = True,
    ) -> "LocalAcceptanceIsolationGuard":
        if self._installed:
            return self
        if enforce_import_order:
            loaded = sorted(
                name
                for name in sys.modules
                if any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in self.SENSITIVE_IMPORT_PREFIXES
                )
            )
            if loaded:
                raise RuntimeError(
                    "network isolation must be installed before sensitive imports: "
                    + ", ".join(loaded[:8])
                )
        if require_no_running_loop:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError(
                    "network isolation must be installed before an asyncio event loop runs"
                )

        self._capture_originals()
        try:
            self._install_socket_guards()
            self._install_asyncio_guards()
            self._install_process_guards()
        except BaseException:
            self._restore_originals()
            raise
        self._installed_at_monotonic = time.monotonic()
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed and not self._originals:
            return
        self._restore_originals()
        self._installed = False

    def __enter__(self) -> "LocalAcceptanceIsolationGuard":
        return self.install(enforce_import_order=False)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.uninstall()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            attempts = [asdict(attempt) for attempt in self._attempts]
            process_attempts = list(self._process_attempts)
        return {
            "installed": self._installed,
            "installed_at_monotonic": self._installed_at_monotonic,
            "successful_non_loopback_connections": (
                self._successful_non_loopback_connections
            ),
            "blocked_non_loopback_attempts": len(attempts),
            "blocked_process_attempts": len(process_attempts),
            "proactor_guard_installed": self._proactor_guard_installed,
            "attempts": attempts,
            "process_attempts": process_attempts,
        }

    def _capture_originals(self) -> None:
        self._originals = {
            "socket.connect": socket.socket.connect,
            "socket.connect_ex": socket.socket.connect_ex,
            "socket.create_connection": socket.create_connection,
            "socket.getaddrinfo": socket.getaddrinfo,
            "loop.create_connection": asyncio.BaseEventLoop.create_connection,
            "loop.create_datagram_endpoint": (
                asyncio.BaseEventLoop.create_datagram_endpoint
            ),
            "loop.subprocess_exec": asyncio.BaseEventLoop.subprocess_exec,
            "loop.subprocess_shell": asyncio.BaseEventLoop.subprocess_shell,
            "asyncio.create_subprocess_exec": asyncio.create_subprocess_exec,
            "asyncio.create_subprocess_shell": asyncio.create_subprocess_shell,
            "subprocess.Popen": subprocess.Popen,
            "os.system": os.system,
            "os.popen": os.popen,
        }
        try:
            from asyncio import windows_events
        except ImportError:
            return
        self._originals["proactor.connect"] = windows_events.IocpProactor.connect

    def _restore_originals(self) -> None:
        originals = self._originals
        if not originals:
            return
        socket.socket.connect = originals["socket.connect"]
        socket.socket.connect_ex = originals["socket.connect_ex"]
        socket.create_connection = originals["socket.create_connection"]
        socket.getaddrinfo = originals["socket.getaddrinfo"]
        asyncio.BaseEventLoop.create_connection = originals["loop.create_connection"]
        asyncio.BaseEventLoop.create_datagram_endpoint = originals[
            "loop.create_datagram_endpoint"
        ]
        asyncio.BaseEventLoop.subprocess_exec = originals["loop.subprocess_exec"]
        asyncio.BaseEventLoop.subprocess_shell = originals["loop.subprocess_shell"]
        asyncio.create_subprocess_exec = originals["asyncio.create_subprocess_exec"]
        asyncio.create_subprocess_shell = originals["asyncio.create_subprocess_shell"]
        subprocess.Popen = originals["subprocess.Popen"]
        os.system = originals["os.system"]
        os.popen = originals["os.popen"]
        if "proactor.connect" in originals:
            from asyncio import windows_events

            windows_events.IocpProactor.connect = originals["proactor.connect"]
        self._originals = {}
        self._proactor_guard_installed = False

    def _stack_summary(self) -> tuple[str, ...]:
        frames = traceback.extract_stack(limit=10)[:-2]
        return tuple(
            f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
            for frame in frames[-6:]
        )

    def _guard_host(self, host: Any, port: Any, phase: str) -> None:
        if is_loopback_host(host):
            return
        attempt = IsolationAttempt(
            host=_redact(host),
            port=_redact(port),
            phase=phase,
            stack=self._stack_summary(),
        )
        with self._lock:
            self._attempts.append(attempt)
        raise NetworkIsolationError(
            f"localhost-only network guard rejected host={attempt.host} "
            f"port={attempt.port} phase={phase}"
        )

    def _guard_address(self, address: Any, phase: str) -> None:
        if isinstance(address, tuple) and address:
            port = address[1] if len(address) > 1 else ""
            self._guard_host(address[0], port, phase)

    def _record_process_attempt(self, phase: str, command: Any) -> None:
        with self._lock:
            self._process_attempts.append(
                {
                    "phase": phase,
                    "command": _redact(command),
                    "stack": " | ".join(self._stack_summary()),
                }
            )
        raise ProcessIsolationError(
            f"local acceptance forbids child process creation phase={phase}"
        )

    def _install_socket_guards(self) -> None:
        original_connect = self._originals["socket.connect"]
        original_connect_ex = self._originals["socket.connect_ex"]
        original_create_connection = self._originals["socket.create_connection"]
        original_getaddrinfo = self._originals["socket.getaddrinfo"]
        guard = self

        def guarded_connect(sock: socket.socket, address: Any) -> Any:
            guard._guard_address(address, "socket.connect")
            return original_connect(sock, address)

        def guarded_connect_ex(sock: socket.socket, address: Any) -> Any:
            guard._guard_address(address, "socket.connect_ex")
            return original_connect_ex(sock, address)

        def guarded_create_connection(
            address: Any, *args: Any, **kwargs: Any
        ) -> Any:
            guard._guard_address(address, "socket.create_connection")
            return original_create_connection(address, *args, **kwargs)

        def guarded_getaddrinfo(
            host: Any,
            port: Any,
            family: int = 0,
            type: int = 0,
            proto: int = 0,
            flags: int = 0,
        ) -> Any:
            guard._guard_host(host, port, "socket.getaddrinfo")
            return original_getaddrinfo(host, port, family, type, proto, flags)

        socket.socket.connect = guarded_connect
        socket.socket.connect_ex = guarded_connect_ex
        socket.create_connection = guarded_create_connection
        socket.getaddrinfo = guarded_getaddrinfo

    def _install_asyncio_guards(self) -> None:
        original_connection = self._originals["loop.create_connection"]
        original_datagram = self._originals["loop.create_datagram_endpoint"]
        original_subprocess_exec = self._originals["loop.subprocess_exec"]
        original_subprocess_shell = self._originals["loop.subprocess_shell"]
        guard = self

        async def guarded_loop_connection(
            loop: asyncio.BaseEventLoop,
            protocol_factory: Callable[..., Any],
            host: Any = None,
            port: Any = None,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if host is not None:
                guard._guard_host(host, port, "asyncio.create_connection")
            return await original_connection(
                loop, protocol_factory, host, port, *args, **kwargs
            )

        async def guarded_loop_datagram(
            loop: asyncio.BaseEventLoop,
            protocol_factory: Callable[..., Any],
            local_addr: Any = None,
            remote_addr: Any = None,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if remote_addr is not None:
                guard._guard_address(remote_addr, "asyncio.create_datagram_endpoint")
            return await original_datagram(
                loop,
                protocol_factory,
                local_addr=local_addr,
                remote_addr=remote_addr,
                *args,
                **kwargs,
            )

        async def guarded_loop_subprocess_exec(
            loop: asyncio.BaseEventLoop, protocol_factory: Any, *args: Any, **kwargs: Any
        ) -> Any:
            guard._record_process_attempt("asyncio.loop.subprocess_exec", args)

        async def guarded_loop_subprocess_shell(
            loop: asyncio.BaseEventLoop,
            protocol_factory: Any,
            command: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            guard._record_process_attempt("asyncio.loop.subprocess_shell", command)

        async def guarded_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
            guard._record_process_attempt("asyncio.create_subprocess_exec", args)

        async def guarded_subprocess_shell(command: Any, *args: Any, **kwargs: Any) -> Any:
            guard._record_process_attempt("asyncio.create_subprocess_shell", command)

        asyncio.BaseEventLoop.create_connection = guarded_loop_connection
        asyncio.BaseEventLoop.create_datagram_endpoint = guarded_loop_datagram
        asyncio.BaseEventLoop.subprocess_exec = guarded_loop_subprocess_exec
        asyncio.BaseEventLoop.subprocess_shell = guarded_loop_subprocess_shell
        asyncio.create_subprocess_exec = guarded_subprocess_exec
        asyncio.create_subprocess_shell = guarded_subprocess_shell

        if "proactor.connect" in self._originals:
            from asyncio import windows_events

            original_proactor_connect = self._originals["proactor.connect"]

            async def guarded_proactor_connect(
                proactor: Any, sock: socket.socket, address: Any
            ) -> Any:
                guard._guard_address(address, "proactor.connect")
                return await original_proactor_connect(proactor, sock, address)

            windows_events.IocpProactor.connect = guarded_proactor_connect
            self._proactor_guard_installed = True

        self._originals["loop.subprocess_exec"] = original_subprocess_exec
        self._originals["loop.subprocess_shell"] = original_subprocess_shell

    def _install_process_guards(self) -> None:
        guard = self

        class GuardedPopen:
            def __new__(cls, *args: Any, **kwargs: Any) -> Any:
                command = args[0] if args else kwargs.get("args", "")
                guard._record_process_attempt("subprocess.Popen", command)

            def __class_getitem__(cls, item: Any) -> Any:
                return cls

        def guarded_system(command: Any) -> Any:
            guard._record_process_attempt("os.system", command)

        def guarded_os_popen(command: Any, *args: Any, **kwargs: Any) -> Any:
            guard._record_process_attempt("os.popen", command)

        subprocess.Popen = GuardedPopen
        os.system = guarded_system
        os.popen = guarded_os_popen
