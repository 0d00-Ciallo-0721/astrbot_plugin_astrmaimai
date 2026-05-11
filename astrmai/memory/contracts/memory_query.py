from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(slots=True)
class MemoryQuery:
    query: str
    session_id: str = ""
    persona_id: str = ""
    top_k: int = 5
    retrieve_keys: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
