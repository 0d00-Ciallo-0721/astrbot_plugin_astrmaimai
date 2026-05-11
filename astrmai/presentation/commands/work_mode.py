from __future__ import annotations

from ..dto import WorkCommandRequest


def parse_work_command(message: str) -> WorkCommandRequest:
    return WorkCommandRequest.from_message(message)


async def handle_work_mode(facade, event):
    async for result in facade.enter_sys3_direct(event):
        yield result


__all__ = ["WorkCommandRequest", "handle_work_mode", "parse_work_command"]
