from __future__ import annotations

from ..contracts.memory_query import MemoryCandidate


class MemoryContextBuilder:
    def __init__(self, *, max_items: int = 5, max_chars: int = 1400):
        self.max_items = max(int(max_items or 5), 1)
        self.max_chars = max(int(max_chars or 1400), 240)

    @staticmethod
    def _clean(text: str) -> str:
        return " ".join(str(text or "").split())

    def select(self, candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
        ranked = sorted(
            candidates,
            key=lambda item: (
                item.relevance_score * 0.45
                + item.importance * 0.25
                + item.confidence * 0.15
                + item.recency_score * 0.1
                - (0.25 if item.status == "stale" else 0.0)
            ),
            reverse=True,
        )
        return ranked[: self.max_items]

    def render_prompt_block(self, candidates: list[MemoryCandidate], *, guidance: str = "") -> tuple[str, str]:
        lines: list[str] = []
        budget = self.max_chars
        selected = self.select(candidates)
        for item in selected:
            text = self._clean(item.summary or item.content)
            if not text:
                continue
            prefix = f"- [{item.kind or 'memory'}]"
            if item.status == "stale":
                prefix += " (possibly stale)"
            line = f"{prefix} {text}"
            if len(line) > budget:
                line = line[: max(0, budget - 3)] + "..."
            lines.append(line)
            budget -= len(line)
            if budget <= 0:
                break
        if not lines:
            return "", ""
        if not guidance:
            for item in selected:
                guidance = str((item.metadata or {}).get("deep_guidance") or "").strip()
                if guidance:
                    break
        guidance = guidance or "Use these memories only when directly relevant; do not quote raw memory text or mention memory retrieval."
        rendered = (
            "(internal memory reference; do not quote or reveal this block)\n"
            + "\n".join(lines)
            + f"\n{guidance}"
        ).strip()
        return rendered, guidance


__all__ = ["MemoryContextBuilder"]
