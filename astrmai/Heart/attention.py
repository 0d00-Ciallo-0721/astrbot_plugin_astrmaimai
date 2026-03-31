import asyncio
import time
from typing import List, Dict, Any
from dataclasses import dataclass, field
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .state_engine import StateEngine
from .judge import Judge
from .sensors import PreFilters
from astrbot.api.message_components import Image, Plain, At, Face # 导入 AstrBot 的底层消息组件

@dataclass
class SessionContext:
    """纯内存态并发上下文，全局共享序列池"""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accumulation_pool: List[Any] = field(default_factory=list)
    is_evaluating: bool = False
    last_active_time: float = field(default_factory=time.time) # [新增] 用于惰性 GC 追踪生命周期


class AttentionGate:
    def __init__(self, state_engine: StateEngine, judge: Judge, sensors: PreFilters, system2_callback, config=None, visual_cortex=None, persona_summarizer=None):
        self.state_engine = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback 
        self.config = config if config else state_engine.config
        self.visual_cortex = visual_cortex # [新增] 多模态视觉皮层
        self.persona_summarizer = persona_summarizer # [新增] 挂载人设压缩器
        
        self.focus_pools: Dict[str, SessionContext] = {}
        self._pool_lock = asyncio.Lock()
        
        # [彻底修复 Bug 3] 新增受控的后台任务追踪池
        self._background_tasks = set()

    # [新增] 从 Image 组件提取 Base64 数据的辅助方法
    async def _extract_image_base64(self, image_component: Any) -> str:
        import base64
        # 1. 尝试直接获取 Base64
        if hasattr(image_component, 'file_to_base64'):
            try:
                res = await image_component.file_to_base64()
                if res: return res
            except Exception:
                pass
        
        # 2. 如果是 URL，发起请求下载
        url = getattr(image_component, 'url', None)
        if url:
            return await self._extract_image_base64_from_url(url)
        
        # 3. 如果是本地路径
        file_path = getattr(image_component, 'file', None) or getattr(image_component, 'path', None)
        if file_path:
            try:
                with open(file_path, 'rb') as f:
                    return base64.b64encode(f.read()).decode('utf-8')
            except Exception:
                pass
        return ""

    # [新增] 从 URL 提取 Base64 数据的辅助方法
    async def _extract_image_base64_from_url(self, url: str) -> str:
        import aiohttp
        import base64
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return base64.b64encode(data).decode('utf-8')
        except Exception as e:
            logger.debug(f"[{self.__class__.__name__}] 获取图片 URL 失败: {e}")
        return ""

    # [修改] 位置: astrmai/Heart/attention.py -> AttentionGate 类下
    async def _get_or_create_session(self, chat_id: str) -> SessionContext:
        async with self._pool_lock:
            if chat_id not in self.focus_pools:
                self.focus_pools[chat_id] = SessionContext()
            # [新增] 每次获取时刷新活跃时间戳
            self.focus_pools[chat_id].last_active_time = time.time()
            return self.focus_pools[chat_id]

    def _is_image_only(self, event: AstrMessageEvent) -> bool:
        """判断是否为纯图片消息"""
        has_img = bool(event.get_extra("extracted_image_urls"))
        has_text = bool(event.message_str and event.message_str.strip())
        return has_img and not has_text

    def _check_continuous_images(self, pool: List[AstrMessageEvent]) -> int:
        """计算末尾连续图片消息的数量"""
        count = 0
        for e in reversed(pool):
            if self._is_image_only(e):
                count += 1
            else:
                break
        return count

    def _fire_background_task(self, coro):
        """[新增] 安全触发后台任务，接管游离 Task 防止静默崩溃"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)


    def _handle_task_result(self, task: asyncio.Task):
        """[新增] 清理已完成的任务并暴漏异常"""
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[Attention Task Error] 注意力系统后台任务发生异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass       

    async def process_event(self, event: AstrMessageEvent) -> str:
        """
        [修改] 注意力判断入口，返回枚举态字符串 (ENGAGED, BUFFERED, IGNORE) 
        精准指导 AstrBot 原生底层的 Stop_Event。
        """
        msg_id = getattr(event.message_obj, 'message_id', None) if getattr(event, 'message_obj', None) else None
        if not msg_id:
            msg_timestamp = getattr(event, 'timestamp', '')
            msg_id = hash(f"{event.message_str}_{event.get_sender_id()}_{msg_timestamp}")

        if not hasattr(AttentionGate, '_global_msg_cache'):
            import collections
            AttentionGate._global_msg_cache = collections.deque(maxlen=200)

        if msg_id in AttentionGate._global_msg_cache:
            return "IGNORE" 
            
        AttentionGate._global_msg_cache.append(msg_id)

        msg_str = event.message_str
        chat_id = str(event.unified_msg_origin)
        
        parts = chat_id.split(":")
        platform_type = parts[1] if len(parts) >= 3 else ("GroupMessage" if event.get_group_id() else "FriendMessage")
        is_private = (platform_type == "FriendMessage")
        event.set_extra("is_private_chat", is_private)
        
        sender_id = str(event.get_sender_id())
        self_id = str(event.get_self_id())
        
        max_len = getattr(self.config.attention, 'max_message_length', 100)
        if msg_str and len(msg_str.strip()) > max_len:
            return "IGNORE" 
            
        wakeup_words = self.config.system1.wakeup_words if self.config and hasattr(self.config.system1, "wakeup_words") else []
        msg_lower = msg_str.strip().lower() if msg_str else ""
        is_keyword_wakeup = any(msg_lower.startswith(kw.lower()) for kw in wakeup_words) if wakeup_words else False
        is_at_wakeup = self.sensors.is_wakeup_signal(event, self_id)
        is_nickname_wakeup = event.get_extra("astrmai_bonus_score", 0.0) >= 1.0
        
        is_strong_wakeup = is_at_wakeup or is_keyword_wakeup or is_nickname_wakeup

        complex_keywords = ["为什么", "怎么", "帮我", "代码", "解释", "写", "什么", "翻译", "分析"]
        is_simple_payload = len(msg_lower) <= 15 and not any(cw in msg_lower for cw in complex_keywords)

        if is_strong_wakeup and is_simple_payload:
            logger.info(f"[{chat_id}] ⚡ [快速模式] 开启窗口，绕过滑动防抖直达 Sys2！")
            event.set_extra("retrieve_keys", ["CORE_ONLY"])
            event.set_extra("is_fast_mode", True)
            event.set_extra("sys1_thought", "听到召唤，立即响应。")
            
            # 🚀 [新增] 快速提取视觉特征，确保穿透模式下主脑也能看见图
            components = event.message_obj.message if (hasattr(event, "message_obj") and event.message_obj) else event.message_str
            await self._normalize_content_to_str(components, event=event)

            if self.sys2_process:
                self._fire_background_task(self.sys2_process(event, [event]))
            return "ENGAGED"

        is_cmd = await self.sensors.is_command(msg_str)
        if is_cmd:
            setattr(event, "is_command_trigger", True)
            return "IGNORE"

        should_process = await self.sensors.should_process_message(event)
        if not should_process or event.get_extra("astrmai_is_command"):
            return "IGNORE" 

        chat_state = await self.state_engine.get_state(chat_id)
        
        extracted_images = event.get_extra("extracted_image_urls") or []
        if extracted_images:
            await self.state_engine.persistence.add_last_message_meta(
                chat_id, sender_id, True, extracted_images
            )

        session = await self._get_or_create_session(chat_id)

        async with session.lock:
            if not session.is_evaluating:
                if not is_strong_wakeup and not is_private: 
                    min_entropy = getattr(self.config.attention, 'throttle_min_entropy', 2)
                    import re
                    pure_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', msg_str) if msg_str else ""
                    if len(pure_text) < min_entropy and not extracted_images:
                        return "IGNORE" 
                    
                    probability = getattr(self.config.attention, 'throttle_probability', 0.1)
                    import random
                    if random.random() > probability:
                        return "IGNORE" 

            if not is_private and not event.get_extra("is_virtual_poke"): 
                msg_hash = hash(msg_str) if msg_str else hash(str(extracted_images))
                if not hasattr(session, 'last_hash'):
                    session.last_hash = None
                    session.repeat_count = 0
                
                if session.last_hash == msg_hash:
                    session.repeat_count += 1
                    threshold = getattr(self.config.attention, 'repeater_threshold', 3)
                    if session.repeat_count == threshold - 1:
                        self._fire_background_task(event.send(event.plain_result(msg_str)))
                    
                    if session.repeat_count >= 1:
                        return "ENGAGED"
                else:
                    session.last_hash = msg_hash
                    session.repeat_count = 0

            session.accumulation_pool.append(event)
            event.set_extra("astrmai_timestamp", time.time())

            if session.is_evaluating:
                logger.info(f"[{chat_id}] ⏳ [窗口持续] 写入消息 -> 累积池 (当前积压: {len(session.accumulation_pool)}条)")
                return "BUFFERED" 
            
            session.is_evaluating = True

        logger.info(f"[{chat_id}] 👁️ [普通模式] 开启窗口...")
        self._fire_background_task(self._debounce_and_judge(chat_id, session, self_id))
        return "BUFFERED"
    
    async def _normalize_content_to_str(self, components: Any, depth: int = 0, event: AstrMessageEvent = None) -> str:
        """
        [修改] 视觉盲区模式 (Vision-Blind System 1)：
        向 System 1 汇报的文本流中遇到图片强行替换为 [图片]。并将真实 URL 存入 event，留给 System 2。
        同时后台保留多模态记忆解析以完善持久化记忆。
        """
        if depth > 3:
            return "[引用层级过深，已截断]"
            
        if not components:
            return ""
        if isinstance(components, str):
            return components
            
        outline = ""
        if isinstance(components, list):
            for i in components:
                try:
                    component_type = getattr(i, 'type', None)
                    if not component_type:
                        component_type = i.__class__.__name__.lower()
                    
                    if isinstance(i, dict):
                        component_type = i.get("type", "unknown").lower()
                        if component_type in ["plain", "text"]:
                            outline += i.get("text", "")
                        elif component_type == "image":
                            # 🚀 [主脑直通车] 收集真实 URL 留给 System 2
                            url = i.get("url", "") or i.get("file", "") or i.get("path", "")
                            if url and event:
                                vision_urls = event.get_extra("direct_vision_urls", [])
                                if url not in vision_urls:
                                    vision_urls.append(url)
                                event.set_extra("direct_vision_urls", vision_urls)
                            
                            # 🚀 [后台记忆] 触发异步视觉皮层入库（不影响文本流）
                            import random
                            import hashlib
                            prob = getattr(self.config.vision, 'image_recognition_probability', 0.5) if hasattr(self.config, 'vision') else 0.0
                            if random.random() < prob:
                                base64_data = await self._extract_image_base64_from_url(url) if url else ""
                                if base64_data:
                                    pic_md5 = hashlib.md5(base64_data.encode('utf-8')).hexdigest()
                                    if getattr(self, 'visual_cortex', None):
                                        self._fire_background_task(self.visual_cortex.process_image_async(pic_md5, base64_data))
                                        
                            # 🚀 [视觉盲区] 对 System 1 只暴露简单的占位符
                            outline += "[图片]"
                            
                        elif component_type == "at":
                            name = i.get("name", "")
                            qq = i.get("qq", "User")
                            outline += f"[@{name}({qq})]" if name else f"[@{qq}]"
                        else:
                            val = i.get("text", "")
                            if val: outline += val
                        continue

                    if component_type == "reply" or i.__class__.__name__ == "Reply":
                        sender_id = getattr(i, 'sender_id', '')
                        sender_nickname = getattr(i, 'sender_nickname', '')
                        
                        sender_info = ""
                        if sender_nickname:
                            sender_info = f"{sender_nickname}({sender_id})"
                        elif sender_id:
                            sender_info = f"{sender_id}"
                        else:
                            sender_info = "未知用户"
                        
                        reply_content = ""
                        if hasattr(i, 'chain') and i.chain:
                            reply_content = await self._normalize_content_to_str(i.chain, depth + 1, event)
                        elif hasattr(i, 'message_str') and i.message_str:
                            reply_content = i.message_str
                        elif hasattr(i, 'text') and i.text:
                            reply_content = i.text
                        else:
                            reply_content = "[内容不可用]"
                            
                        if len(reply_content) > 150:
                            reply_content = reply_content[:150] + "..."
                        
                        outline += f"「↪ 引用 {sender_info} 的消息：{reply_content}」"
                        continue
                        
                    if component_type == "plain" or i.__class__.__name__ == "Plain":
                        outline += getattr(i, 'text', '')
                    elif component_type == "image" or i.__class__.__name__ == "Image":
                        # 🚀 [主脑直通车] 收集真实 URL 留给 System 2
                        url = getattr(i, 'url', '') or getattr(i, 'file', '') or getattr(i, 'path', '')
                        if url and event:
                            vision_urls = event.get_extra("direct_vision_urls", [])
                            if url not in vision_urls:
                                vision_urls.append(url)
                            event.set_extra("direct_vision_urls", vision_urls)
                        
                        # 🚀 [后台记忆] 触发异步视觉皮层入库（不影响文本流）
                        import random
                        import hashlib
                        prob = getattr(self.config.vision, 'image_recognition_probability', 0.5) if hasattr(self.config, 'vision') else 0.0
                        if random.random() < prob:
                            base64_data = await self._extract_image_base64(i)
                            if base64_data:
                                pic_md5 = hashlib.md5(base64_data.encode('utf-8')).hexdigest()
                                if getattr(self, 'visual_cortex', None):
                                    self._fire_background_task(self.visual_cortex.process_image_async(pic_md5, base64_data))
                                    
                        # 🚀 [视觉盲区] 对 System 1 只暴露简单的占位符
                        outline += "[图片]"
                        
                    elif component_type == "face" or i.__class__.__name__ == "Face":
                        outline += f"[表情:{getattr(i, 'id', getattr(i, 'name', ''))}]"
                    elif component_type == "at" or i.__class__.__name__ == "At":
                        qq = getattr(i, 'qq', '')
                        name = getattr(i, 'name', '')
                        if str(qq).lower() == "all":
                            outline += "[@全体成员]"
                        elif name:
                            outline += f"[@{name}({qq})]"
                        else:
                            outline += f"[@{qq}]"
                    elif component_type == "record" or i.__class__.__name__ == "Record":
                        outline += "[语音]"
                    elif component_type == "video" or i.__class__.__name__ == "Video":
                        outline += "[视频]"
                    elif component_type == "share" or i.__class__.__name__ == "Share":
                        title = getattr(i, 'title', '')
                        content = getattr(i, 'content', '')
                        outline += f"[分享:《{title}》{content}]"
                    elif component_type == "contact" or i.__class__.__name__ == "Contact":
                        outline += f"[联系人:{getattr(i, 'id', '')}]"
                    elif component_type == "location" or i.__class__.__name__ == "Location":
                        title = getattr(i, 'title', '')
                        content = getattr(i, 'content', '')
                        outline += f"[位置:{title}({content})]"
                    elif component_type == "music" or i.__class__.__name__ == "Music":
                        title = getattr(i, 'title', '')
                        content = getattr(i, 'content', '')
                        outline += f"[音乐:{title}({content})]"
                    elif component_type == "poke" or i.__class__.__name__ == "Poke":
                        outline += f"[戳一戳 对:{getattr(i, 'qq', '')}]"
                    elif component_type in ["forward", "node", "nodes"] or i.__class__.__name__ in ["Forward", "Node", "Nodes"]:
                        outline += "[合并转发消息]"
                    elif component_type == "json" or i.__class__.__name__ == "Json":
                        data = getattr(i, 'data', None)
                        if isinstance(data, str):
                            import json
                            try:
                                json_data = json.loads(data)
                                if "prompt" in json_data:
                                    outline += f"[JSON卡片:{json_data.get('prompt', '')}]"
                                elif "app" in json_data:
                                    outline += f"[小程序:{json_data.get('app', '')}]"
                                else:
                                    outline += "[JSON消息]"
                            except (json.JSONDecodeError, ValueError, TypeError):
                                outline += "[JSON消息]"
                        else:
                            outline += "[JSON消息]"
                    elif component_type in ["rps", "dice", "shake"] or i.__class__.__name__ in ["RPS", "Dice", "Shake"]:
                        outline += f"[{component_type}]"
                    elif component_type == "file" or i.__class__.__name__ == "File":
                        outline += f"[文件:{getattr(i, 'name', '')}]"
                    elif component_type == "wechatemoji" or i.__class__.__name__ == "WechatEmoji":
                        outline += "[微信表情]"
                    else:
                        if component_type == "anonymous":
                            outline += "[匿名]"
                        elif component_type == "redbag":
                            outline += "[红包]"
                        elif component_type == "xml":
                            outline += "[XML消息]"
                        elif component_type == "cardimage":
                            outline += "[卡片图片]"
                        elif component_type == "tts":
                            outline += "[TTS]"
                        else:
                            val = getattr(i, "text", "")
                            if val:
                                outline += val
                            else:
                                outline += f"[{component_type}]"
                except Exception as e:
                    import traceback
                    from astrbot.api import logger
                    logger.error(f"处理消息组件时出错: {e}")
                    logger.error(f"错误详情: {traceback.format_exc()}")
                    outline += f"[处理失败的消息组件]"
                    continue
                    
        return outline
    
    def _convert_interaction_to_narrative(self, content: str, bot_name: str) -> str:
        """
        [优化版] 将上方产生的机器结构化技术标记，转换为大模型视角的自然叙述与动作描写
        """
        import re
        if not content: return ""

        # 1. 戳一戳虚拟事件翻译 (Interaction: A -> B)
        def poke_repl(match):
            s_name, t_name = match.groups()
            if bot_name and (t_name == bot_name or t_name == '我'):
                return f"[{s_name} 伸出手指戳了戳你的脸蛋]"
            return f"[{s_name} 伸出手指戳了戳 {t_name}]"
        
        content = re.sub(r"\(Interaction:\s*(.*?)\s*->\s*(.*?)\)", poke_repl, content)
            
        # 2. 图片内容翻译
        content = re.sub(r"\[图片描述:\s*(.*?)\s*\(Ref:.*?\)\]", r"[分享了一张图片，画面是：\1]", content)
        content = re.sub(r"\[图片\]", r"[发了一张图片]", content)

        # 3. 引用回复翻译 (联动上方更新的格式)
        # 将结构化的: 「↪ 引用 张三(12345) 的消息：你好呀」 翻译成 AI 更能理解的画面感动作: [指着 张三(12345) 的话回应：你好呀]
        content = re.sub(r"「↪ 引用 (.*?) 的消息：(.*?)」", r"[回复 \1 的话\2]", content)
        
        # 兼容旧版的简易匹配格式
        content = re.sub(r"\(回复\s*(.*?):.*?\)", r"[指着 \1 的话回应]", content)
        content = content.replace("(回复消息)", "[回复了对方]")
        content = content.replace("(回复", "[指着话题回应]")

        # 4. @提及翻译
        if bot_name:
            # 如果艾特了机器人本身 (带或不带QQ号)
            content = re.sub(rf"\[@{bot_name}(?:\([^)]+\))?\]", "[@你]", content)
        
        # 替换其他 @ 用户 (抹平艾特带来的割裂感，将其转换为动作描写)
        content = re.sub(r"\[@(.*?)\]", r"[@ \1]", content)
        
        # 5. 网址链接防噪过滤 (防止极长 URL 破坏 LLM 上下文注意力)
        content = re.sub(r"(https?://[^\s]+)", r"[分享了一个网页链接]", content)

        # 6. 清理多余的技术标识 (如图像缓存提取的 Ref ID) 与冗余空白字符
        content = re.sub(r"\(Ref:.*?\)", "", content)
        content = re.sub(r"\s+", " ", content).strip()
        
        return content

    async def inject_external_event(self, chat_id: str, event_data: dict):
        """
        将外部非原生事件安全地压入倒计时的 2 秒滑动窗口池中。
        """
        import time
        session = await self._get_or_create_session(chat_id)
        
        async with session.lock:
            # 🟢 [修复] 增加 message_str 等默认属性，实现 100% 鸭子类型伪装，防止底层逻辑崩溃
            class ExternalEventAdapter(dict):
                def __init__(self, data):
                    super().__init__(data)
                    # 伪装基础属性，即使漏网也不会引发 AttributeError
                    self.message_str = data.get("content", "") 
                    
                def get_extra(self, key, default=None):
                    return self.get(key, default)
            
            adapted_event = ExternalEventAdapter(event_data)
            
            if "astrmai_timestamp" not in adapted_event:
                adapted_event["astrmai_timestamp"] = time.time()
                
            session.accumulation_pool.append(adapted_event)
            session.last_active_time = time.time()

    async def _format_and_filter_messages(self, events: List[AstrMessageEvent]):
        """
        [修改] 斗图过滤与同源消息折叠，接入转义层。
        [修改] 将 event(e) 参数传递给 _normalize_content_to_str，以激活旁路拦截机制。
        """
        if not events: return "", []
        
        filtered_events = []
        continuous_img_count = 0
        
        # 1. 斗图过滤阶段
        for e in events:
            if self._is_image_only(e):
                continuous_img_count += 1
                if continuous_img_count >= 3:
                    continue # 直接过滤丢弃
            else:
                continuous_img_count = 0
            filtered_events.append(e)

        grouped_texts = []
        curr_sender = None
        curr_msgs = []
        
        # 尝试获取机器人主称呼
        bot_name = "我"
        if hasattr(self, 'config') and self.config and hasattr(self.config, 'system1'):
            if self.config.system1.nicknames:
                bot_name = self.config.system1.nicknames[0]
                
        # 2. 同源聚合与画面感转义阶段
        for e in filtered_events:
            sender = e.get_sender_name()
            
            # [核心修改] 区分虚拟事件与普通消息组件提取
            if e.get_extra("is_virtual_poke"):
                raw_content = e.message_str
            else:
                components = e.message_obj.message if (hasattr(e, "message_obj") and e.message_obj) else e.message_str
                # [核心修改] 使用 await 等待异步方法返回，并传入 event 以激活旁路拦截
                raw_content = await self._normalize_content_to_str(components, event=e)
                
            # [核心修改] 执行叙事转义
            content = self._convert_interaction_to_narrative(raw_content, bot_name)
            
            e.set_extra("astrmai_rich_text", content)
            
            # 兜底空消息
            if not content.strip():
                content = "[图片]"
            
            if sender != curr_sender:
                if curr_sender is not None:
                    grouped_texts.append(f"{curr_sender}：{'，'.join(curr_msgs)}")
                curr_sender = sender
                curr_msgs = [content]
            else:
                curr_msgs.append(content)
                
        if curr_sender is not None:
            grouped_texts.append(f"{curr_sender}：{'，'.join(curr_msgs)}")

        return "\n".join(grouped_texts), filtered_events
    
    # [修改] 位置: astrmai/Heart/attention.py -> AttentionGate 类下
    async def _debounce_and_judge(self, chat_id: str, session: SessionContext, self_id: str):
        try:
            while True:
                import time
                no_msg_start_time = time.time()
                last_pool_len = 0
                debounce_window = float(getattr(self.config.attention, 'debounce_window', 2.0))
                
                while True:
                    current_pool_len = len(session.accumulation_pool)
                    
                    if current_pool_len >= 15:
                        break
                        
                    if self._check_continuous_images(session.accumulation_pool) >= 3:
                        break

                    if current_pool_len > last_pool_len:
                        no_msg_start_time = time.time()
                        last_pool_len = current_pool_len
                        ts = session.accumulation_pool[-1].get_extra("astrmai_timestamp")
                        if ts: no_msg_start_time = ts
                    
                    if time.time() - no_msg_start_time > debounce_window:
                        break
                    import asyncio
                    await asyncio.sleep(0.3)

                async with session.lock:
                    events_to_process = list(session.accumulation_pool)
                    session.accumulation_pool.clear()
                    
                if not events_to_process:
                    break 

                total_msgs = len(events_to_process)
                
                bot_reply_msgs = [e for e in events_to_process if hasattr(e, 'get_extra') and e.get_extra("is_external_bot_reply")]
                bot_reply_count = len(bot_reply_msgs)
                
                if bot_reply_count > 0:
                    if bot_reply_count <= (total_msgs / 3):
                        from astrbot.api import logger
                        logger.info(f"[{chat_id}] ⚠️ 检测到插件插话，但处于活跃对话流中 (Bot:{bot_reply_count} / 总:{total_msgs})，判定为聊天背景音，Sys1 继续接管。")
                    else:
                        from astrbot.api import logger
                        logger.info(f"[{chat_id}] 🛑 插件响应占比过高 (Bot:{bot_reply_count} / 总:{total_msgs})，判定为纯功能交互，Sys1 隐退。")
                        return 
                        
                events_to_process = [e for e in events_to_process if not (hasattr(e, 'get_extra') and e.get_extra("is_external_bot_reply"))]
                
                if not events_to_process:
                    break
                
                from astrbot.api import logger
                logger.info(f"[{chat_id}] 🚪 [窗口关闭] 写入sys1进行评估 (共处理 {len(events_to_process)} 条有效消息)...")
                
                try:
                    logger.info(f"[{chat_id}] 🔍 [Sys1 追踪] 开始格式化与过滤消息...")
                    combined_text, final_events = await self._format_and_filter_messages(events_to_process)
                    logger.info(f"[{chat_id}] 🔍 [Sys1 追踪] 消息格式化完毕。最终文本: '{combined_text[:50]}...' (有效事件数: {len(final_events)})")
                    
                    if final_events:
                        anchor_event = final_events[0]
                        main_event = final_events[-1] 
                        main_event.set_extra("astrmai_anchor_event", anchor_event)
                        main_event.set_extra("astrmai_window_events", final_events)
                        
                        # 🚀 [全知视界编排] 汇总窗口内的所有图片真实 URL，统一塞给主事件，交由 System 2 接管
                        all_vision_urls = []
                        for e in final_events:
                            urls = e.get_extra("direct_vision_urls", [])
                            if urls:
                                all_vision_urls.extend(urls)
                        
                        unique_urls = list(dict.fromkeys(all_vision_urls)) # 去重并保持顺序
                        if unique_urls:
                            main_event.set_extra("direct_vision_urls", unique_urls)
                        
                        is_wakeup = any(self.sensors.is_wakeup_signal(e, self_id) for e in final_events)
                        is_first_event_wakeup = self.sensors.is_wakeup_signal(final_events[0], self_id) if final_events else False
                        
                        sys1_persona = "保持你原本的性格特征"
                        
                        if getattr(self, 'persona_summarizer', None):
                            target_persona_id = getattr(self.config.persona, 'persona_id', "")
                            cache_key = target_persona_id.strip() if target_persona_id else f"session_{chat_id}"
                            
                            cached_data = self.persona_summarizer.cache.get(cache_key)
                            if cached_data and isinstance(cached_data, dict):
                                sys1_persona = cached_data.get("summary", "")
                            else:
                                sys1_persona = f"角色ID: {target_persona_id}" if target_persona_id else "傲娇系AI智能体"

                        logger.info(f"[{chat_id}] ⚖️ [Sys1 追踪] 移交 Judge 裁决 (强唤醒={is_wakeup}, 携带人设长度={len(sys1_persona)})...")
                        
                        # 🚀 [视觉盲区裁决] Sys1 不再接收任何 picid 或 URL，只接收干净的文本与 [图片] 占位符
                        sys1_eval_text = combined_text
                        
                        plan = await self.judge.evaluate(
                            chat_id=chat_id, 
                            message=sys1_eval_text,  
                            is_force_wakeup=is_wakeup,
                            persona_summary=sys1_persona,
                            window_events_count=len(final_events),
                            is_first_event_wakeup=is_first_event_wakeup
                        )
                        
                        logger.info(f"[{chat_id}] 📋 [Sys1 追踪] Judge 裁决结果 -> Action: {plan.action} | Thought: {plan.thought}")
                        
                        main_event.set_extra("sys1_thought", plan.thought)
                        # [Sys3新增] 透传裁决动作供 Planner 区分路由
                        main_event.set_extra("judge_action", plan.action) 

                        if plan.action in ["REPLY", "WAIT", "TOOL_CALL"]:
                            safe_thought = plan.thought or "无"
                            thought_abbr = safe_thought[:5] + "..." if len(safe_thought) > 5 else safe_thought
                            
                            retrieve_keys = plan.meta.get("retrieve_keys", [])

                            logger.info(
                                f"[{chat_id}] 🚀 [窗口结束] 快速注入sys2 | "
                                f"动作: {plan.action} | "
                                f"记忆Keys: {retrieve_keys} | "
                                f"潜意识: {thought_abbr} | "
                                f"携带消息: {len(final_events)}条"
                            )
                            if self.sys2_process:
                                logger.info(f"[{chat_id}] 🔄 [Sys1 追踪] 开始调用 sys2_process (后台异步抛出)...")
                                self._fire_background_task(self.sys2_process(main_event, final_events))
                                logger.info(f"[{chat_id}] ✅ [Sys1 追踪] sys2_process 已安全抛出至后台。")
                        else:
                            logger.info(f"[{chat_id}] 💤 [窗口结束] Sys1 决定静默不回复 (判定Action: {plan.action})")
                    else:
                        logger.info(f"[{chat_id}] 🈳 [Sys1 追踪] 过滤后无有效事件，放弃评估。")
                        
                except Exception as inner_e:
                    from astrbot.api import logger
                    logger.error(f"[{chat_id}] ⚠️ 批次消息处理失败，安全拦截防崩溃: {inner_e}", exc_info=True)

                async with session.lock:
                    if not session.accumulation_pool:
                        break
                    else:
                        from astrbot.api import logger
                        logger.info(f"[{chat_id}] ⚠️ [发现积压消息] 写入sys1，开启新一轮注意力维持...")

        except Exception as e:
            from astrbot.api import logger
            logger.exception(f"Attention Aggregation Critical Error: {e}")
        finally:
            async with session.lock:
                session.is_evaluating = False
                session.last_hash = None
                session.repeat_count = 0
                from astrbot.api import logger
                logger.debug(f"[{chat_id}] 🔓 注意力生命周期结束，锁已安全释放。")