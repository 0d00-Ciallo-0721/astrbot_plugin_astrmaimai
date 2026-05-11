from __future__ import annotations

from ..dto import AdminCommandRequest


async def build_admin_snapshot(facade) -> dict:
    diagnostics = facade.get_runtime_diagnostics()
    capabilities = await facade.get_capability_overview()
    return {
        "diagnostics": diagnostics,
        "capabilities": capabilities,
    }


def build_admin_request(action: str, **payload) -> AdminCommandRequest:
    return AdminCommandRequest(action=action, payload=dict(payload))


__all__ = ["AdminCommandRequest", "build_admin_request", "build_admin_snapshot"]
