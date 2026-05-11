from __future__ import annotations

from ..dto import HelpCommandView


def build_help_view(facade) -> HelpCommandView:
    return HelpCommandView(
        title="AstrMai",
        body=facade.build_help_text(),
    )


async def handle_mai_help(facade, event):
    view = build_help_view(facade)
    yield event.plain_result(view.body)


__all__ = ["HelpCommandView", "build_help_view", "handle_mai_help"]
