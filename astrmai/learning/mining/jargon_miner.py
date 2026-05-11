from __future__ import annotations

from typing import Iterable, List, Sequence


class JargonMiner:
    def __init__(self, expression_miner, min_messages: int = 1):
        self.expression_miner = expression_miner
        self.min_messages = max(int(min_messages or 1), 1)

    def _normalize_messages(self, messages: Iterable | None) -> List:
        if not messages:
            return []
        normalized = []
        for message in messages:
            if message is None:
                continue
            content = getattr(message, 'content', None)
            if content is None:
                normalized.append(message)
                continue
            if str(content).strip():
                normalized.append(message)
        return normalized

    async def mine(self, group_id: str, messages: Sequence | None):
        if not group_id or self.expression_miner is None:
            return []
        normalized = self._normalize_messages(messages)
        if len(normalized) < self.min_messages:
            return []
        return await self.expression_miner.mine_jargons(group_id, normalized)
