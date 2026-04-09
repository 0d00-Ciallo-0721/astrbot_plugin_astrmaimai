import time
from typing import List, Dict, Any, Optional
import re          # [新增] 导入正则表达式库
import json        # [新增] 导入 JSON 解析库
import asyncio     # [新增] 导入异步库
import time
from astrbot.api import logger
import asyncio
import hashlib
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from ..infra.database import DatabaseService
from ..infra.datamodels import ChatState, UserProfile, VisualMemory
from .persona_summarizer import PersonaSummarizer

class ContextEngine:
    """
    上下文引擎 (System 2: Cognition Core)
    职责: 动态编织 Prompt，集成人设压缩、状态注入、记忆回溯与黑话植入。
    设计原则: Anti-Bloat (结构化标签), Dynamic Injection (动态注入), Native Chinese (中文原声), 纯剧本模式 (Script Mode)
    (注：用户画像已从被动注入改为主动 Tool Call 召回)
    """
    def __init__(self, db: DatabaseService, persona_summarizer: PersonaSummarizer, config=None, context=None):
        self.db = db
        self.summarizer = persona_summarizer
        self.config = config if config else self.summarizer.gateway.config
        self.context = context if context else self.summarizer.gateway.context
        self._prefix_hash_by_chat: Dict[str, str] = {}

    def get_last_prefix_hash(self, chat_id: str) -> str:
        return self._prefix_hash_by_chat.get(chat_id, "")
    
    async def build_prompt(self, 
                           chat_id: str, 
                           event_messages: List[AstrMessageEvent],
                           retrieve_keys: List[str] = None,
                           slang_patterns: str = "",
                           sys1_thought: str = "",
                           goals_context: str = "",
                           expression_habits: str = "",
                           planner_reasoning: str = "",
                           jargon_explanation: str = "",
                           near_context_priority: bool = False) -> str:
        
        if retrieve_keys is None:
            retrieve_keys = []
        tool_descs = ""
            
        is_fast_mode = "CORE_ONLY" in retrieve_keys
            
        valid_keys = []
        if hasattr(self, "filter_retrieve_keys"):
            valid_keys = self.filter_retrieve_keys(retrieve_keys)
        else:
            valid_keys = retrieve_keys

        # 1. 获取基础状态数据 (情绪、精力)
        state = self.db.get_chat_state(chat_id)
        
        # 2. 调用 Summarizer 获取人格切片数据
        target_persona_id = getattr(self.config.persona, 'persona_id', "")
        raw_prompt = getattr(self.config.persona, 'prompt', "")

        if target_persona_id and not raw_prompt:
            try:
                _persona_text = ""
                _config = getattr(self.context, "config", None)
                if not _config and hasattr(self.context, "get_config"):
                    _config = self.context.get_config()
                    
                if isinstance(_config, dict) and "personas" in _config:
                    for p in _config.get("personas", []):
                        if str(p.get("persona_id")) == target_persona_id or str(p.get("id")) == target_persona_id or str(p.get("name")) == target_persona_id:
                            _persona_text = p.get("prompt", "") or p.get("system_prompt", "")
                            break
                            
                if not _persona_text and hasattr(self.context, "persona_manager"):
                    p_mgr = self.context.persona_manager
                    if hasattr(p_mgr, "personas"):
                        p_list = p_mgr.personas
                        if isinstance(p_list, dict):
                            p_obj = p_list.get(target_persona_id)
                            if p_obj:
                                _persona_text = getattr(p_obj, "prompt", getattr(p_obj, "system_prompt", ""))
                        elif isinstance(p_list, list):
                            for p in p_list:
                                if str(getattr(p, "persona_id", getattr(p, "id", ""))) == target_persona_id or str(getattr(p, "name", "")) == target_persona_id:
                                    _persona_text = getattr(p, "prompt", getattr(p, "system_prompt", ""))
                                    break

                if _persona_text:
                    raw_prompt = _persona_text
                    if getattr(self, "_last_logged_persona", "") != _persona_text:
                        logger.info(f"[ContextEngine] 🧬 成功从 AstrBot 核心框架自动提取了 ID 为 [{target_persona_id}] 的原生人格内容 (长度: {len(raw_prompt)}字)。")
                        self._last_logged_persona = _persona_text
                else:
                    logger.warning(f"[ContextEngine] ⚠️ 未能在 AstrBot 原生框架中找到 ID 为 [{target_persona_id}] 的设定，请确保在 AstrBot 的「人格设置」中该 ID 存在且含有文本。")
                    
            except Exception as e:
                logger.error(f"[ContextEngine] 自动提取 AstrBot 原生人格时发生异常: {e}")

        persona_data = await self.summarizer.get_summary(
            original_prompt=raw_prompt,
            persona_id=target_persona_id,
            session_id=chat_id
        )
        
        if isinstance(persona_data, dict):
            persona_summary = persona_data.get("summary", "")
            style_guide = persona_data.get("style", "保持自然对话风格，偶尔使用网络颜文字，被夸奖时会嘴硬")
            shards = persona_data.get("shards", {})
            raw_persona = persona_data.get("raw", "")
            is_full_ready = persona_data.get("is_full_ready", True)
        else:
            persona_summary = persona_data[0] if isinstance(persona_data, tuple) else str(persona_data)
            style_guide = persona_data[1] if isinstance(persona_data, tuple) and len(persona_data) > 1 else "保持自然对话风格，偶尔使用网络颜文字，被夸奖时会嘴硬"
            shards = {}
            raw_persona = persona_summary
            is_full_ready = True
            
        if not is_full_ready:
            valid_keys = []
            if retrieve_keys and not is_fast_mode:
                persona_summary += "\n(你正在努力回忆更深层的记忆细节，但暂时只能想起个大概。)"

        # 3. 按需组装区块
        role_block = ""
        style_block = ""
        
        if "ALL" in valid_keys:
            role_block = f"{raw_persona}"
            style_guide = "" 
        elif is_fast_mode:
            role_block = f"{raw_persona}"
        else:
            role_block = f"{persona_summary}"
            if valid_keys:
                recollections = []
                for k in valid_keys:
                    if k in shards and shards[k] and shards[k] != "无":
                        recollections.append(f"- {k}: {shards[k]}")
                if recollections:
                    role_block += "\n\n临时回忆加载：\n" + "\n".join(recollections)
        
        if style_guide:
            style_block = f"对话风格：\n{style_guide}\n"

        state_block = self._build_state_block(state)
            
        slang_block = ""
        if slang_patterns and not is_fast_mode and not near_context_priority:
            slang_block = f"群组专属表达模式与黑话参考:\n{slang_patterns}\n"

        # 动态上下文热加载 (私聊专属画像注入)
        private_chat_block = ""
        if "FriendMessage" in chat_id and event_messages and not is_fast_mode:
            try:
                user_id = str(event_messages[-1].get_sender_id())
                profile_data = await self.db.persistence.load_user_profile(user_id)
                if profile_data:
                    analysis = profile_data.get("persona_analysis", "暂无深度侧写。")
                    tags = profile_data.get("tags", [])
                    tags_str = " / ".join(tags) if tags else "暂无特定标签"
                    raw_name = profile_data.get("name", "该用户")
                    nickname = profile_data.get("nickname", "")
                    display_name = f"{nickname}（{raw_name}）" if nickname else raw_name
                    
                    memory_points = profile_data.get("memory_points", [])
                    memory_points_block = ""
                    if memory_points:
                        mp_lines = []
                        for mp in memory_points[:6]:  
                            parts = mp.split(":", 2)
                            if len(parts) >= 2:
                                category, content = parts[0], parts[1]
                                mp_lines.append(f"【{category}】{content}")
                        if mp_lines:
                            memory_points_block = (
                                ">>> [关于TA的记忆点] <<<\n"
                                + "\n".join(mp_lines) + "\n"
                            )

                    structured_lines = []
                    category_map = [
                        ("identity_points", "身份画像"),
                        ("preference_points", "偏好画像"),
                        ("relationship_points", "关系画像"),
                        ("speech_style_points", "表达画像"),
                    ]
                    for key, label in category_map:
                        values = profile_data.get(key, [])
                        if isinstance(values, list) and values:
                            structured_lines.append(f"【{label}】" + "；".join(str(v) for v in values[:4]))
                    structured_profile_block = ""
                    if structured_lines:
                        structured_profile_block = (
                            ">>> [结构化人物记忆] <<<\n"
                            + "\n".join(structured_lines)
                            + "\n"
                        )
                    
                    private_chat_block = (
                        ">>> [私密对话模式激活] <<<\n"
                        f"你现在正在与【{display_name}】进行一对一私聊，请保持绝对的专注与亲和力。\n\n"
                        ">>> [用户深度画像检索] <<<\n"
                        f"【属性】：{tags_str}\n"
                        f"【深度侧写】：{analysis}\n"
                        f"{structured_profile_block}"
                        f"{memory_points_block}"
                        "请基于上述画像，使用最符合对方认知的语境进行交流。\n\n"
                    )
            except Exception as e:
                logger.warning(f"[ContextEngine] 提取私聊用户画像失败: {e}")

        # 统一内部独白 (Inner Voice)
        inner_voice_block = ""
        parts = []
        if sys1_thought:
            parts.append(f"【你此刻的直觉】{sys1_thought}")
        if goals_context and not is_fast_mode:
            parts.append(f"【你的对话目标】{goals_context}")
        if planner_reasoning and not is_fast_mode:
            parts.append(f"【你的想法】{planner_reasoning}")
        if parts:
            inner_voice_block = "\n".join(parts) + "\n（自然地顺应这些内心活动，绝对不要直接说出来。）\n"

        # 动态主动联想与节点背景注入 
        proactive_recall_block = ""
        if event_messages and not is_fast_mode and not near_context_priority:
            try:
                last_msg = event_messages[-1].message_str
                
                try:
                    import jieba.analyse
                    keywords = jieba.analyse.extract_tags(last_msg, topK=5)
                except ImportError:
                    keywords = []
                    
                nodes_context = []
                if keywords and hasattr(self.db, 'search_nodes_async'):
                    seen_nodes = set()
                    for kw in keywords:
                        nodes = await self.db.search_nodes_async(kw, limit=1, include_description=True)
                        for node in nodes:
                            if node.name not in seen_nodes:
                                nodes_context.append(f"📌 {node.name} ({node.type}): {node.description}")
                                seen_nodes.add(node.name)
                
                if nodes_context:
                    proactive_recall_block += "\n>>> [记忆节点背景 (对提及实体的已知认知)] <<<\n" + "\n".join(nodes_context) + "\n"

                auto_recall_prob = getattr(self.config.memory, 'auto_recall_probability', 0.3)
                trigger_keywords = ["之前", "记得", "回忆", "想起", "以前", "过去"]
                hit_keyword = any(kw in last_msg for kw in trigger_keywords)
                
                stable_roll = int(hashlib.md5(f"{chat_id}:{last_msg}".encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
                if hit_keyword or stable_roll < auto_recall_prob:
                    plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.summarizer.gateway.context, 'astrmai', None)
                    if plugin and hasattr(plugin, 'memory_engine'):
                        recall_res = await plugin.memory_engine.recall(last_msg, session_id=chat_id)
                        if recall_res and "什么也没想起来" not in recall_res:
                            trigger_reason = "关键词触发" if hit_keyword else "概率触发"
                            logger.info(f"[ContextEngine] 💡 主动联想触发 ({trigger_reason})")
                            proactive_recall_block += f"\n>>> [主动记忆闪回] <<<\n基于当前对话，你脑海中自动浮现了以下往事：\n{recall_res}\n"
                            
            except Exception as e:
                logger.warning(f"[ContextEngine] 主动联想与节点注入失败: {e}")

        # 表达习惯注入
        expression_block = ""
        if expression_habits and not is_fast_mode and not near_context_priority:
            expression_block = f"\n[语言习惯参考]\n{expression_habits}\n"

        # 黑话解释注入
        jargon_block = ""
        if jargon_explanation and not is_fast_mode and not near_context_priority:
            jargon_block = f"\n[群内黑话参考]\n{jargon_explanation}\n（已知含义仅供理解，自然使用即可，不要刻意解释）\n"

        # ==========================================
        # 🟢 [新增 P1-T3] 多级回复风格随机化
        # ==========================================
        REPLY_STYLES = [
            "给出日常且简短的回复。像真人发消息一样自然。",
            "用稍微长一点的句子回复，可以展开聊聊。",
            "用一个极短的句子或者一个词回复，惜字如金。",
            "用一句话回复，但在末尾抛出一个反问或追问。",
            "像在吐槽一样回复，带点调侃和幽默感。",
        ]
        REPLY_STYLE_WEIGHTS = [0.45, 0.20, 0.15, 0.10, 0.10]
        
        style_seed = hashlib.md5(f"{chat_id}:{int(time.time() // 3600)}".encode("utf-8")).hexdigest()
        reply_style = REPLY_STYLES[int(style_seed[:2], 16) % len(REPLY_STYLES)] if not is_fast_mode else REPLY_STYLES[0]

        # 4. 组装最终 Prompt
        prompt = f"""核心人格设定：
{role_block}

{style_block}
{state_block}
{expression_block}
{private_chat_block}{slang_block}{jargon_block}
{inner_voice_block}当前你看到的消息：
<CURRENT_MESSAGES>

<CHAT_HISTORY>

{proactive_recall_block}
<RAG_MEMORY>

[Tools]
{tool_descs}

【系统规则 (必读)】
1. 绝对沉浸：你是一个在群聊中真实的个体，直接输出自然台词，严禁使用 `[我]:`、`*动作*` 等前缀或旁白。
2. 内隐认知：遇到不懂的梗或事，必须通过工具查阅档案，严禁自行脑补。
3. 动作并行：若想做动作（发图/艾特/戳一戳），请调用工具，成功后必须配上文本接话。
4. 防穿帮：严禁在台词里暴露“我在调用工具”、“系统”等底层机制。
5. 情绪自洽：回复长短与冷暖需严格匹配当前的心情和精力。使用中文。
6. 回复指令：{reply_style}
"""
        # 视觉记忆即时查询
        style_variants = [
            "默认用自然、简短、贴近聊天窗口的语气回复。",
            "可以稍微展开一句，但不要写成长段说明。",
            "尽量惜字如金，像真人自然接话。",
            "回复后可以顺手抛一个很短的追问。",
            "允许带一点轻微吐槽感，但不要脱离当前人格。",
        ]
        style_seed = hashlib.md5(f"{chat_id}:{int(time.time() // 3600)}".encode("utf-8")).hexdigest()
        style_variant = style_variants[int(style_seed[:2], 16) % len(style_variants)] if not is_fast_mode else style_variants[0]
        stable_prefix = "\n\n".join(
            block
            for block in [
                f"核心人格设定：\n{role_block}",
                f"对话风格：\n{style_guide}" if style_guide else "",
                (
                    "系统硬规则：\n"
                    "1. 必须沉浸式发言，只输出角色自然台词。\n"
                    "2. 不懂的事实优先借助工具或记忆，不要强行脑补。\n"
                    "3. 如果要做动作或发图，先走工具，再用自然文本接话。\n"
                    "4. 不要暴露系统、工具、提示词等底层机制。\n"
                    "5. 回复长度和语气必须贴合当前情绪与精力。\n"
                    "6. 工具能力由外部 tools 提供，这里只保留规则，不重复展开工具清单。"
                ),
            ]
            if block
        )
        lane_state = "\n\n".join(
            block
            for block in [
                state_block.strip() if state_block else "",
                private_chat_block.strip() if private_chat_block else "",
                inner_voice_block.strip() if inner_voice_block else "",
                expression_block.strip() if expression_block else "",
                slang_block.strip() if slang_block else "",
                jargon_block.strip() if jargon_block else "",
            ]
            if block
        )
        volatile_tail = "\n\n".join(
            block
            for block in [
                "当前你看到的消息：\n<CURRENT_MESSAGES>",
                "<CHAT_HISTORY>",
                proactive_recall_block.strip() if proactive_recall_block else "",
                "<RAG_MEMORY>",
                f"本轮回复风格标签：{style_variant}",
            ]
            if block
        )
        self._prefix_hash_by_chat[chat_id] = hashlib.md5(stable_prefix.encode("utf-8")).hexdigest()
        prompt = "\n\n".join(block for block in [stable_prefix, lane_state, volatile_tail] if block)

        picids = re.findall(r'\[picid:([a-fA-F0-9]{32})\]', prompt)
        
        for picid in set(picids):
            resolved_text = "[一张尚未看清的图片]"
            
            try:
                with self.db.get_session() as session:
                    mem = session.get(VisualMemory, picid)
                    if mem and mem.description:
                        try:
                            tags = json.loads(mem.emotion_tags)
                            tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
                        except Exception:
                            tags_str = ""
                            
                        if mem.type == "emoji":
                            resolved_text = f"[发了一个表情包，画面是：{mem.description}，传达了：{tags_str}]" if tags_str else f"[发了一个表情包，画面是：{mem.description}]"
                        else:
                            resolved_text = f"[发了一张图片，画面是：{mem.description}]"
            except Exception as e:
                logger.debug(f"[ContextEngine] 视觉记忆查询失败 {picid}: {e}")
                
            prompt = prompt.replace(f"[picid:{picid}]", resolved_text)

        return prompt.strip()

    def _build_state_block(self, state: Optional[ChatState]) -> str:
        if not state:
            return "[当前心情: 平静 (情绪 0.00) | 精力: 1.00]"
        
        mood_val = state.mood
        mood_tag = "平静"
        if mood_val > 0.3: mood_tag = "开心/兴奋"
        elif mood_val > 0.8: mood_tag = "狂喜"
        elif mood_val < -0.3: mood_tag = "低落/冷淡"
        elif mood_val < -0.8: mood_tag = "愤怒/极度悲伤"
        
        return f"[当前心情: {mood_tag} (情绪 {mood_val:.2f}) | 精力: {state.energy:.2f}]"


    def _build_slang_block(self, patterns: str) -> str:
        if not patterns:
            return ""
        return f"\n[Speaking Patterns]\n{patterns}"
    
    class FuzzyKeyMatcher:
        # 🟢 [核心修复]: 在白名单中加入 'CORE_ONLY'，防止系统极速模式标签被误判拦截并打印警告
        ALLOWED_KEYS = {"logic_style", "speech_style", "world_view", "timeline", "relations", "skills", "values", "secrets", "ALL", "CORE_ONLY"}
        CN_TO_EN_MAP = {
            "性格逻辑": "logic_style",
            "语言风格": "speech_style",
            "世界观": "world_view",
            "生平经历": "timeline",
            "人际关系": "relations",
            "技能能力": "skills",
            "价值观": "values",
            "深层秘密": "secrets",
            "完整降临": "ALL",
            "全部": "ALL",
            "所有": "ALL",
            "核心穿透": "CORE_ONLY" # [新增] 中文兼容映射
        }

        @classmethod
        def match(cls, raw_keys: List[str]) -> List[str]:
            import difflib
            valid_keys = set()
            if not isinstance(raw_keys, list):
                return []
                
            for key in raw_keys:
                if not isinstance(key, str): continue
                key_strip = key.strip()
                if not key_strip: continue
                
                if key_strip in cls.ALLOWED_KEYS:
                    valid_keys.add(key_strip)
                    continue
                    
                if key_strip in cls.CN_TO_EN_MAP:
                    valid_keys.add(cls.CN_TO_EN_MAP[key_strip])
                    continue
                    
                en_matches = difflib.get_close_matches(key_strip, cls.ALLOWED_KEYS, n=1, cutoff=0.6)
                if en_matches:
                    valid_keys.add(en_matches[0])
                    continue
                    
                cn_matches = difflib.get_close_matches(key_strip, cls.CN_TO_EN_MAP.keys(), n=1, cutoff=0.6)
                if cn_matches:
                    valid_keys.add(cls.CN_TO_EN_MAP[cn_matches[0]])
                    
            return list(valid_keys)

    def filter_retrieve_keys(self, raw_keys: List[str]) -> List[str]:
        if not raw_keys:
            return []
            
        valid_keys = self.FuzzyKeyMatcher.match(raw_keys)
        
        if len(valid_keys) != len(raw_keys) or set(valid_keys) != set(raw_keys):
            logger.warning(f"[ContextEngine] 🔍 模糊过滤容错器触发: 原始 Keys {raw_keys} -> 有效 Keys {valid_keys}")
            
        return valid_keys
