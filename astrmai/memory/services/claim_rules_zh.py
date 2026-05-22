from __future__ import annotations

import re


ZH_CORRECTION_HINTS = (
    "说错了",
    "改一下",
    "不是",
    "不是那个意思",
    "刚才那个不对",
    "之前",
    "现在",
    "纠正",
)

ZH_SHORT_TERM_HINTS = (
    "今天",
    "最近",
    "这周",
    "刚刚",
    "焦虑",
    "难受",
    "不想社交",
    "心情",
)

ZH_SERVER_COUNT_PATTERN = re.compile(r"(\d+)\s*(?:台|个)?(?:服务器|机器)")

ZH_SERVER_KEYWORDS = ("服务器", "机器")
ZH_ANXIETY_KEYWORDS = ("焦虑", "难受", "心情")


__all__ = [
    "ZH_CORRECTION_HINTS",
    "ZH_SHORT_TERM_HINTS",
    "ZH_SERVER_COUNT_PATTERN",
    "ZH_SERVER_KEYWORDS",
    "ZH_ANXIETY_KEYWORDS",
]
