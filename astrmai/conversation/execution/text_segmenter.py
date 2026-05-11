# astrmai/Brain/text_segmenter.py
import re
from typing import List, Tuple
from astrbot.api import logger

class TextSegmenter:
    """
    基于状态机与权重累加的智能文本分段器 (吸收了 Splitter 插件的核心逻辑)。
    解决正则切割太粗暴、中英数字误切、以及首尾幽灵换行符导致 QQ 气泡错位的问题。
    """
    _URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    _SENTENCE_ENDINGS = frozenset("。？！?!…")
    _SOFT_ENDINGS = frozenset("，,、；;")
    _CONTINUATION_PREFIXES = (
        "但",
        "但是",
        "不过",
        "只是",
        "所以",
        "然后",
        "而且",
        "另外",
        "其实",
        "可能",
        "如果",
        "那",
        "先",
        "也",
        "Still",
        "But",
        "So",
        "And",
    )
    _TAIL_PREFIXES = (
        "那",
        "先",
        "所以",
        "总之",
        "不过",
        "也别",
        "你可以",
        "我们先",
        "我会",
        "别急",
    )

    def __init__(self, min_length: int = 15, max_length: int = 120):
        self.min_length = min_length
        self.max_length = max_length
        
        # 定义成对出现的字符，在智能分段时避免在这些符号内部切断
        self.pair_map = {
            '"': '"', '《': '》', '（': '）', '(': ')', 
            '[': ']', '{': '}', "'": "'", '【': '】', '<': '>'
        }
        self.quote_chars = {'"', "'", "`"}
        
        # 主切分正则（遇到这些符号考虑分段）
        self.split_pattern = re.compile(r'[。？！?!\n…]+')
        # 次级切分正则（当长度超过 max_length 产生死锁时，强制用这些符号切分）
        self.secondary_pattern = re.compile(r'[，,、；;]+')

    def _visible_len(self, text: str) -> int:
        return len(re.sub(r"\s+", "", text or ""))

    def _clean_segment(self, text: str) -> str:
        cleaned = re.sub(r"^\n+|\n+$", "", str(text or "").strip())
        return re.sub(r"[ \t]{2,}", " ", cleaned)

    def _join_units(self, left: str, right: str) -> str:
        if not left:
            return right
        if not right:
            return left
        if left[-1:] in "。？！?!…，,、；;" or right[:1] in "。？！?!…，,、；;":
            return left + right
        return left + " " + right

    def _starts_with_continuation(self, text: str) -> bool:
        stripped = text.lstrip()
        return any(stripped.startswith(prefix) for prefix in self._CONTINUATION_PREFIXES)

    def _is_natural_tail(self, text: str) -> bool:
        stripped = text.lstrip()
        return any(stripped.startswith(prefix) for prefix in self._TAIL_PREFIXES)

    def _should_start_new_segment(self, current: str, unit: str, combined: str) -> bool:
        current_len = self._visible_len(current)
        unit_len = self._visible_len(unit)
        combined_len = self._visible_len(combined)
        if current_len < self.min_length:
            return False
        if combined_len <= self.max_length:
            return bool(
                current_len >= max(self.min_length * 2, int(self.max_length * 0.72))
                and unit_len >= self.min_length
                and self._starts_with_continuation(unit)
            )
        if unit_len < self.min_length:
            return bool(current_len >= self.max_length and self._is_natural_tail(unit))
        return True

    def _scan_units(self, text: str) -> List[Tuple[str, bool]]:
        units: List[Tuple[str, bool]] = []
        stack: List[str] = []
        current = ""
        i = 0
        n = len(text)

        while i < n:
            if text.startswith("```", i):
                next_idx = text.find("```", i + 3)
                end_idx = n if next_idx == -1 else next_idx + 3
                current += text[i:end_idx]
                i = end_idx
                continue

            url_match = self._URL_RE.match(text, i)
            if url_match:
                current += url_match.group()
                i = url_match.end()
                continue

            char = text[i]
            is_opener = char in self.pair_map

            if char in self.quote_chars:
                if stack and stack[-1] == char:
                    stack.pop()
                else:
                    stack.append(char)
                current += char
                i += 1
                continue

            if stack:
                expected_closer = self.pair_map.get(stack[-1])
                if char == expected_closer:
                    stack.pop()
                elif is_opener and char not in self.quote_chars:
                    stack.append(char)
                current += " " if char == "\n" and not text.startswith("\n\n", i) else char
                i += 1
                continue

            if is_opener:
                stack.append(char)
                current += char
                i += 1
                continue

            if text.startswith("\n\n", i):
                cleaned = self._clean_segment(current)
                if cleaned:
                    units.append((cleaned, True))
                elif units:
                    units[-1] = (units[-1][0], True)
                current = ""
                newline_match = re.match(r"\n{2,}", text[i:])
                i += len(newline_match.group()) if newline_match else 2
                continue

            if char == "\n":
                current += " "
                i += 1
                continue

            if char in self._SENTENCE_ENDINGS:
                while i < n and text[i] in self._SENTENCE_ENDINGS:
                    current += text[i]
                    i += 1
                cleaned = self._clean_segment(current)
                if cleaned:
                    units.append((cleaned, False))
                current = ""
                continue

            if self._visible_len(current) >= self.max_length and char in self._SOFT_ENDINGS:
                current += char
                cleaned = self._clean_segment(current)
                if cleaned:
                    units.append((cleaned, False))
                current = ""
                i += 1
                continue

            current += char
            i += 1

        cleaned = self._clean_segment(current)
        if cleaned:
            units.append((cleaned, False))
        return units

    def _pack_units(self, units: List[Tuple[str, bool]]) -> List[str]:
        segments: List[Tuple[str, bool]] = []
        current = ""
        for unit, hard_after in units:
            unit = self._clean_segment(unit)
            if not unit:
                continue
            combined = self._join_units(current, unit)
            if current and self._should_start_new_segment(current, unit, combined):
                segments.append((current, False))
                current = unit
            else:
                current = combined
            if hard_after and current:
                segments.append((current, True))
                current = ""
        if current:
            segments.append((current, False))

        merged: List[str] = []
        previous_hard_after = False
        for segment, hard_after in segments:
            cleaned = self._clean_segment(segment)
            if not cleaned:
                continue
            if (
                merged
                and not previous_hard_after
                and self._visible_len(cleaned) < self.min_length
                and not self._is_natural_tail(cleaned)
            ):
                merged[-1] = self._join_units(merged[-1], cleaned)
            else:
                merged.append(cleaned)
            previous_hard_after = hard_after
        return merged

    def segment(self, text: str) -> List[str]:
        if not text:
            return []
        normalized = re.sub(r"\n{3,}", "\n\n", text.strip())
        return self._pack_units(self._scan_units(normalized))

    @classmethod
    def semantic_chunk(cls, text: str, max_chunk_size: int = 800) -> List[str]:
        """
        [新增] 针对 RAG 原典入库优化的语意切片器。
        严格遵循双换行符 `\\n\\n` 或 Markdown 标题 (`#`) 进行切断，拒绝在句子中间因字数达标而暴力切割。
        如果一段语义真的超长 (> max_chunk_size)，才启动次级句子切分。
        """
        if not text:
            return []
            
        # 1. 预处理：标准化换行与标题分割线
        # 在 markdown 标题前强制加入双换行以触发切割
        text = re.sub(r'\\n(#+\\s+)', r'\\n\\n\1', text)
        
        # 按照强语义边界（双换行）切块
        raw_chunks = [c.strip() for c in re.split(r'\\n{2,}', text) if c.strip()]
        
        final_chunks = []
        current_chunk = ""
        
        for chunk in raw_chunks:
            if len(chunk) > max_chunk_size:
                # 极端情况：这一整段长得离谱，只能退化使用句号强切
                sub_chunks = [s.strip() + "。" for s in re.split(r'[。？！?!]', chunk) if s.strip()]
                for sub in sub_chunks:
                    if len(current_chunk) + len(sub) > max_chunk_size and current_chunk:
                        final_chunks.append(current_chunk.strip())
                        current_chunk = sub
                    else:
                        current_chunk += (" " + sub if current_chunk else sub)
            else:
                if len(current_chunk) + len(chunk) > max_chunk_size and current_chunk:
                    final_chunks.append(current_chunk.strip())
                    current_chunk = chunk
                else:
                     current_chunk += ("\\n\\n" + chunk if current_chunk else chunk)
                     
        if current_chunk.strip():
            final_chunks.append(current_chunk.strip())
            
        return final_chunks
