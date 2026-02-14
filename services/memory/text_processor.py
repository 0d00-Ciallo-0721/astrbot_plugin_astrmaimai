### 📄 services/memory/text_processor.py
import re
import string
from pathlib import Path
try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False

class TextProcessor:
    """文本处理器：分词与清洗"""
    DEFAULT_STOPWORDS = {
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "的", "了", "着", "是", "在"
    }

    def __init__(self, stopwords_path: str = None):
        self.stopwords = self.DEFAULT_STOPWORDS.copy()
        if stopwords_path:
            self._load_stopwords(stopwords_path)

    def _load_stopwords(self, path: str):
        try:
            p = Path(path)
            if p.exists():
                with open(p, 'r', encoding='utf-8') as f:
                    for line in f:
                        word = line.strip()
                        if word: self.stopwords.add(word)
        except Exception:
            pass

    def tokenize(self, text: str, remove_stopwords: bool = True) -> list:
        if not text: return []
        # 清洗
        text = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', text)
        
        if JIEBA_AVAILABLE:
            tokens = jieba.lcut(text)
        else:
            tokens = text.split() # 降级处理

        if remove_stopwords:
            tokens = [t for t in tokens if t.strip() and t not in self.stopwords]
        
        return tokens