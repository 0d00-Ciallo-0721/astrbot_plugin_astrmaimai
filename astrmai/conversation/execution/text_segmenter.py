# astrmai/Brain/text_segmenter.py
import re
from typing import List
from astrbot.api import logger

class TextSegmenter:
    """
    基于状态机与权重累加的智能文本分段器 (吸收了 Splitter 插件的核心逻辑)。
    解决正则切割太粗暴、中英数字误切、以及首尾幽灵换行符导致 QQ 气泡错位的问题。
    """
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

    def segment(self, text: str) -> List[str]:
        if not text:
            return []

        # 1. 前置清理：清洗多余换行，将 3 个以上的连续换行压缩为 2 个（保留段落感）
        text = re.sub(r'\n{3,}', '\n\n', text.strip())
        
        segments = []
        stack = []
        i = 0
        n = len(text)
        current_chunk = ""
        current_weight = 0

        while i < n:
            # 1. 代码块保护：检测到 ``` 则跳过到结束标识，视为一个整体
            if text.startswith("```", i):
                next_idx = text.find("```", i + 3)
                if next_idx != -1:
                    current_chunk += text[i:next_idx+3]
                    current_weight += (next_idx + 3 - i)
                    i = next_idx + 3
                    continue
                else:
                    current_chunk += text[i:]
                    current_weight += (n - i)
                    break

            char = text[i]
            is_opener = char in self.pair_map
            
            # 2. 引号处理
            if char in self.quote_chars:
                if stack and stack[-1] == char: 
                    stack.pop() # 引号闭合
                else: 
                    stack.append(char) # 引号开启
                current_chunk += char
                if not char.isspace(): current_weight += 1
                i += 1
                continue
            
            # 3. 栈内处理（当前处于成对符号内部）
            if stack:
                expected_closer = self.pair_map.get(stack[-1])
                if char == expected_closer: 
                    stack.pop() # 符号匹配闭合
                elif is_opener and char not in self.quote_chars: 
                    stack.append(char) # 嵌套开启
                
                # 【核心修复】：符号内部的换行符替换为空格，保持块的完整性，防止换行逃逸导致排版爆炸
                if char == '\n': 
                    # 如果是强段落标记则保留，否则一律替换为空格
                    if text.startswith("\n\n", i):
                        current_chunk += "\n\n"
                        i += 2
                        continue
                    else:
                        current_chunk += ' '
                else: 
                    current_chunk += char
                    if not char.isspace(): current_weight += 1
                i += 1
                continue
                
            # 4. 进入新的成对符号
            if is_opener:
                stack.append(char)
                current_chunk += char
                if not char.isspace(): current_weight += 1
                i += 1
                continue

            # 5. 主标点分隔符匹配逻辑
            match = self.split_pattern.match(text, pos=i)
            if match:
                delimiter = match.group()
                prev_char = text[i-1] if i > 0 else ""
                next_char = text[i+len(delimiter)] if i+len(delimiter) < n else ""
                
                # 【防粗暴切割】：英文与数字边界保护逻辑 (照搬 Splitter 的优秀正则)
                if '\n' not in delimiter and bool(re.match(r'^[ \t.?!,;:\-\']+$', delimiter)):
                    # 保护数字中的小数点 (如 3.14)
                    if bool(re.match(r'^[ \t.?!]+$', delimiter)):
                        if '.' in delimiter and bool(re.match(r'^\d$', prev_char)) and bool(re.match(r'^\d$', next_char)):
                            current_chunk += delimiter
                            current_weight += len(delimiter)
                            i += len(delimiter)
                            continue
                            
                    # 保护英文短句或标点不被切开 (如 Hello, world)
                    if bool(re.match(r'^[ \t,;:\-\']+$', delimiter)):
                        prev_is_en = (not prev_char) or bool(re.match(r'^[a-zA-Z0-9 \t.?!,;:\-\']$', prev_char))
                        next_is_en = (not next_char) or bool(re.match(r'^[a-zA-Z0-9 \t.?!,;:\-\']$', next_char))
                        if prev_is_en and next_is_en:
                            current_chunk += delimiter
                            current_weight += len(delimiter)
                            i += len(delimiter)
                            continue

                # 【核心逻辑】：判断是否应该真切分
                clean_len = len(current_chunk.strip())
                should_split = False
                
                # 条件 A：遇到了强段落分割（双换行）
                if '\n\n' in delimiter:
                    should_split = True
                # 条件 B：长度已经达标，且不是单纯为了积攒长度
                elif clean_len >= self.min_length:
                    should_split = True
                
                if should_split:
                    current_chunk += delimiter
                    segments.append(current_chunk)
                    current_chunk = ""
                    current_weight = 0
                    i += len(delimiter)
                else:
                    # 不达标？吞噬标点，继续累加！完美解决 "啊..." 刷屏问题
                    current_chunk += delimiter
                    current_weight += len(delimiter)
                    i += len(delimiter)
                continue
            
            # 6. 次级标点防死锁（如果废话太长，触发兜底切分）
            if current_weight >= self.max_length:
                sec_match = self.secondary_pattern.match(text, pos=i)
                if sec_match:
                    delimiter = sec_match.group()
                    current_chunk += delimiter
                    segments.append(current_chunk)
                    current_chunk = ""
                    current_weight = 0
                    i += len(delimiter)
                    continue

            # 常规字符累加
            current_chunk += char
            if not char.isspace():
                current_weight += 1
            i += 1

        # 7. 尾部收尾与孤儿碎片缝合
        if current_chunk.strip():
            clean_len = len(current_chunk.strip())
            # 如果最后一段极短（比如只有几个字），且前面有已经分好的段落，强行缝合到上一段
            if segments and clean_len < self.min_length:
                segments[-1] += " " + current_chunk.strip()
            else:
                segments.append(current_chunk)

        # 8. 终极净化：消灭首尾的换行符幽灵
        final_segments = []
        for seg in segments:
            cleaned = seg.strip()
            # 严格剥离首尾多余换行，避免引发 QQ 渲染引擎气泡多出一行空白
            cleaned = re.sub(r'^\n+|\n+$', '', cleaned)
            if cleaned:
                final_segments.append(cleaned)

        return final_segments

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