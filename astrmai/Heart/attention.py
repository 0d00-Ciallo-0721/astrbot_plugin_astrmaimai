import asyncio
import time
from typing import List, Dict, Any
from dataclasses import dataclass, field
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .state_engine import StateEngine
from .judge import Judge
from .sensors import PreFilters

@dataclass
class SessionContext:
    """纯内存态并发上下文，全局共享序列池"""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accumulation_pool: List[Any] = field(default_factory=list)
    is_evaluating: bool = False

class AttentionGate:
    def __init__(self, state_engine: StateEngine, judge: Judge, sensors: PreFilters, system2_callback, config=None):
        self.state_engine = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback 
        self.config = config if config else state_engine.config
        
        self.focus_pools: Dict[str, SessionContext] = {}
        self._pool_lock = asyncio.Lock()

    async def _get_or_create_session(self, chat_id: str) -> SessionContext:
        async with self._pool_lock:
            if chat_id not in self.focus_pools:
                self.focus_pools[chat_id] = SessionContext()
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

    async def process_event(self, event: AstrMessageEvent):
        msg_str = event.message_str
        chat_id = str(event.unified_msg_origin)
        sender_id = str(event.get_sender_id())
        self_id = str(event.get_self_id())
        
        max_len = getattr(self.config.attention, 'max_message_length', 100)
        if msg_str and len(msg_str.strip()) > max_len:
            logger.debug(f"[{chat_id}] 🛡️ 拦截异常数据：消息长度超限 ({len(msg_str.strip())} > {max_len})，已直接丢弃。")
            return
        # --- 判断强唤醒特征 ---
        wakeup_words = self.config.system1.wakeup_words if self.config and hasattr(self.config.system1, "wakeup_words") else []
        msg_lower = msg_str.strip().lower() if msg_str else ""
        is_keyword_wakeup = any(msg_lower.startswith(kw.lower()) for kw in wakeup_words) if wakeup_words else False
        is_at_wakeup = self.sensors.is_wakeup_signal(event, self_id)
        is_nickname_wakeup = event.get_extra("astrmai_bonus_score", 0.0) >= 1.0
        
        is_strong_wakeup = is_at_wakeup or is_keyword_wakeup or is_nickname_wakeup

        # 【Step 0: 快速穿透判定 (直达 Sys2，连窗口都不等)】
        # 必须同时满足：是强唤醒 + 载荷极其简单 (防止复杂问题被剥夺 Sys1 的思考能力)
        complex_keywords = ["为什么", "怎么", "帮我", "代码", "解释", "写", "什么", "翻译", "分析"]
        is_simple_payload = len(msg_lower) <= 15 and not any(cw in msg_lower for cw in complex_keywords)

        if is_strong_wakeup and is_simple_payload:
            logger.info(f"[{chat_id}] ⚡ 触发超级快速穿透模式，绕过所有窗口组件直达 Sys2！")
            event.set_extra("retrieve_keys", ["CORE_ONLY"])
            event.set_extra("is_fast_mode", True)
            event.set_extra("sys1_thought", "听到召唤，立即响应。")
            if self.sys2_process:
                await self.sys2_process(event, [event])
            return

        is_cmd = await self.sensors.is_command(msg_str)
        if is_cmd:
            setattr(event, "is_command_trigger", True)
            logger.info(f"[AstrMai-Sensor] 🛡️ 识别到指令: {msg_str[:10]}... 已标记并拦截。")
            return 

        should_process = await self.sensors.should_process_message(event)
        if not should_process or event.get_extra("astrmai_is_command"):
            return

        chat_state = await self.state_engine.get_state(chat_id)
        
        extracted_images = event.get_extra("extracted_image_urls") or []
        if extracted_images:
            await self.state_engine.persistence.add_last_message_meta(
                chat_id, sender_id, True, extracted_images
            )

        session = await self._get_or_create_session(chat_id)

        async with session.lock:
            # 【中间组件 1: 消息级节流 (Spinal Cord)】
            if not session.is_evaluating:
                # 只有普通的闲聊，才会被信息熵和概率过滤器拦截。强唤醒 (is_strong_wakeup) 必须豁免，保证能开窗入池。
                if not is_strong_wakeup:
                    # 1. 信息熵检测
                    min_entropy = getattr(self.config.attention, 'throttle_min_entropy', 2)
                    import re
                    pure_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', msg_str) if msg_str else ""
                    if len(pure_text) < min_entropy and not extracted_images:
                        return
                    
                    # 2. 随机开窗概率
                    probability = getattr(self.config.attention, 'throttle_probability', 0.1)
                    import random
                    if random.random() > probability:
                        return

            # 3. 复读机拦截 (Repeat Interceptor)
            msg_hash = hash(msg_str) if msg_str else hash(str(extracted_images))
            if not hasattr(session, 'last_hash'):
                session.last_hash = None
                session.repeat_count = 0
            
            if session.last_hash == msg_hash:
                session.repeat_count += 1
                threshold = getattr(self.config.attention, 'repeater_threshold', 3)
                if session.repeat_count == threshold - 1:
                    logger.info(f"[{chat_id}] 🤖 触发人类本质，执行复读...")
                    import asyncio
                    asyncio.create_task(event.send(event.plain_result(msg_str)))
                
                if session.repeat_count >= 1:
                    return
            else:
                session.last_hash = msg_hash
                session.repeat_count = 0

            session.accumulation_pool.append(event)
            event.set_extra("astrmai_timestamp", time.time())

            if session.is_evaluating:
                logger.debug(f"[{chat_id}] 🧠 Busy: 追加消息 -> 累积池")
                return
            
            session.is_evaluating = True

        logger.info(f"[{chat_id}] 👁️ 注意力聚焦，开启多用户并发聚合池!")
        import asyncio
        asyncio.create_task(self._debounce_and_judge(chat_id, session, self_id))

    def _normalize_content_to_str(self, components: Any) -> str:
        """
        [新增/完善] 将底层富文本组件规范化为字符串标记 (增强鸭子类型版)
        全面接入几十种富文本组件的解析，并解除对底层硬编码类名的死板依赖。
        """
        if not components:
            return ""
        if isinstance(components, str):
            return components
            
        outline = ""
        if isinstance(components, list):
            for i in components:
                try:
                    # 使用反射与鸭子类型获取组件类型，兼容所有平台适配器
                    component_type = getattr(i, 'type', None)
                    if not component_type:
                        component_type = i.__class__.__name__.lower()
                    
                    # 1. 兼容普通字典格式 (Dict 格式适配)
                    if isinstance(i, dict):
                        component_type = i.get("type", "unknown").lower()
                        if component_type in ["plain", "text"]:
                            outline += i.get("text", "")
                        elif component_type == "image":
                            outline += "[图片]"
                        elif component_type == "at":
                            name = i.get("name", "")
                            qq = i.get("qq", "User")
                            outline += f"[@{name}({qq})]" if name else f"[@{qq}]"
                        else:
                            val = i.get("text", "")
                            if val: outline += val
                        continue

                    # 2. 特别优化 Reply 组件的处理 (递归解析引用的链式消息)
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
                            reply_content = self._normalize_content_to_str(i.chain)
                        elif hasattr(i, 'message_str') and i.message_str:
                            reply_content = i.message_str
                        elif hasattr(i, 'text') and i.text:
                            reply_content = i.text
                        else:
                            reply_content = "[内容不可用]"
                            
                        # 防止引用过长冲爆上下文限制
                        if len(reply_content) > 150:
                            reply_content = reply_content[:150] + "..."
                        
                        # 特殊标识符，供给下一步的 _convert_interaction_to_narrative 进行剧本化替换
                        outline += f"「↪ 引用 {sender_info} 的消息：{reply_content}」"
                        continue
                        
                    # 3. 几十种杂项组件的兜底处理
                    if component_type == "plain" or i.__class__.__name__ == "Plain":
                        outline += getattr(i, 'text', '')
                    elif component_type == "image" or i.__class__.__name__ == "Image":
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
        content = re.sub(r"「↪ 引用 (.*?) 的消息：(.*?)」", r"[指着 \1 的话回应：\2]", content)
        
        # 兼容旧版的简易匹配格式
        content = re.sub(r"\(回复\s*(.*?):.*?\)", r"[指着 \1 的话回应]", content)
        content = content.replace("(回复消息)", "[回复了对方]")
        content = content.replace("(回复", "[指着话题回应]")

        # 4. @提及翻译
        if bot_name:
            # 如果艾特了机器人本身 (带或不带QQ号)
            content = re.sub(rf"\[@{bot_name}(?:\([^)]+\))?\]", "[看向你]", content)
        
        # 替换其他 @ 用户 (抹平艾特带来的割裂感，将其转换为动作描写)
        content = re.sub(r"\[@(.*?)\]", r"[看向 \1]", content)
        
        # 5. 网址链接防噪过滤 (防止极长 URL 破坏 LLM 上下文注意力)
        content = re.sub(r"(https?://[^\s]+)", r"[分享了一个网页链接]", content)

        # 6. 清理多余的技术标识 (如图像缓存提取的 Ref ID) 与冗余空白字符
        content = re.sub(r"\(Ref:.*?\)", "", content)
        content = re.sub(r"\s+", " ", content).strip()
        
        return content

    def _format_and_filter_messages(self, events: List[AstrMessageEvent]):
        """
        [修改] 斗图过滤与同源消息折叠，接入转义层
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
                raw_content = self._normalize_content_to_str(components)
                
            # [核心修改] 执行叙事转义
            content = self._convert_interaction_to_narrative(raw_content, bot_name)
            
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

    async def _debounce_and_judge(self, chat_id: str, session: SessionContext, self_id: str):
        """[修改] _debounce_and_judge (捕获多端时序并熔断，同时清理复读机状态)"""
        try:
            logger.debug(f"[{chat_id}] ⏱️ 开启聚合滑动窗口...")
            import time
            no_msg_start_time = time.time()
            last_pool_len = 0
            debounce_window = getattr(self.config.attention, 'debounce_window', 2.0)
            
            while True:
                current_pool_len = len(session.accumulation_pool)
                
                if current_pool_len >= 15:
                    logger.debug(f"[{chat_id}] 触发容量限制 (>=15)，立即熔断。")
                    break
                    
                if self._check_continuous_images(session.accumulation_pool) >= 3:
                    logger.debug(f"[{chat_id}] 触发斗图防爆 (连续图片>=3)，立即熔断。")
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
                return

            combined_text, final_events = self._format_and_filter_messages(events_to_process)
            
            if not final_events: return

            anchor_event = final_events[0]
            main_event = final_events[-1] 
            main_event.set_extra("astrmai_anchor_event", anchor_event)
            # [阶段二新增] 透传完整的窗口期消息事件队列 (W) 供下游情绪路由计算
            main_event.set_extra("astrmai_window_events", final_events)

            logger.info(f"[{chat_id}] 📦 窗口闭合。过滤后留存 {len(final_events)} 条消息。\n聚合内容:\n{combined_text}")
            
            is_wakeup = any(self.sensors.is_wakeup_signal(e, self_id) for e in final_events)
            is_first_event_wakeup = self.sensors.is_wakeup_signal(final_events[0], self_id) if final_events else False
            
            plan = await self.judge.evaluate(
                chat_id=chat_id, 
                message=combined_text, 
                is_force_wakeup=is_wakeup,
                persona_summary="",
                window_events_count=len(final_events),
                is_first_event_wakeup=is_first_event_wakeup
            )
            
            main_event.set_extra("sys1_thought", plan.thought)

            if plan.action in ["REPLY", "WAIT"]:
                if self.sys2_process:
                    await self.sys2_process(main_event, final_events)

        except Exception as e:
            logger.exception(f"Attention Aggregation Error: {e}")
        finally:
            async with session.lock:
                session.is_evaluating = False
                # 滑动窗口关闭时清理复读机记忆，防止跨窗口期的误判
                session.last_hash = None
                session.repeat_count = 0
            logger.debug(f"[{chat_id}] 🔓 注意力评估状态已释放。")