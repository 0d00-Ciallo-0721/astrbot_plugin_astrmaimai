from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import astrbot.api.message_components as Comp

class PreFilters:
    """
    感知与过滤器 (System 1)
    职责: 构建指令防火墙，执行严格的消息清洗与拦截，防止 LLM 幻觉。
    """
    def __init__(self, config):
        self.config = config or {}
        self.foreign_commands = set()
        self._commands_loaded = False # 惰性加载标记

    async def _load_foreign_commands(self):
        """异步动态加载系统内所有注册指令，构建指令黑名单"""
        if self._commands_loaded:
            return

        try:
            all_cmds = await list_commands()
            count = 0
            if all_cmds:
                for cmd in all_cmds:
                    # 1. 记录主指令
                    main_cmd = cmd.get("effective_command")
                    if main_cmd: 
                        self.foreign_commands.add(main_cmd.lower())
                        count += 1
                    
                    # 2. 记录所有别名
                    for alias in cmd.get("aliases", []):
                        self.foreign_commands.add(alias.lower())
                        count += 1
            
            # 3. 追加用户在 config 中自定义的额外拦截词
            extra_cmds = self.config.get("extra_command_list", [])
            for extra in extra_cmds:
                if extra:
                    self.foreign_commands.add(extra.lower())
                    count += 1
            
            logger.info(f"[AstrMai-Sensor] 🛡️ 指令防火墙已加载。共监控 {len(self.foreign_commands)} 个指令词。")
            self._commands_loaded = True
        except Exception as e:
            logger.warning(f"[AstrMai-Sensor] ⚠️ 加载外部指令列表失败: {e}")

    async def should_process_message(self, event: AstrMessageEvent) -> bool:
        """
        核心网关：判断是否应该处理这条消息。
        如果识别为系统指令，则打上标记并拦截。
        """
        await self._load_foreign_commands()

        # 1. 忽略 Bot 自身发出的消息
        if event.get_sender_id() == event.get_self_id():
            return False

        # 2. 深度清洗文本与负载检测
        clean_text_parts = []
        has_payload = False
        
        if event.message_obj and event.message_obj.message:
            for seg in event.message_obj.message:
                # 忽略 At 和 引用 组件的文本干扰
                if isinstance(seg, (Comp.At, Comp.Reply)):
                    continue 
                if isinstance(seg, Comp.Plain):
                    # 彻底移除零宽空格并去除首尾空白
                    text = seg.text.replace('\u200b', '').strip()
                    if text: 
                        clean_text_parts.append(text)
                # 标记是否携带多媒体负载
                if isinstance(seg, (Comp.Image, Comp.Video, Comp.Record, Comp.File)):
                    has_payload = True
        
        clean_text = " ".join(clean_text_parts).strip().lower()
        
        # 3. 🚨 核心指令拦截防火墙 🚨
        if clean_text:
            words = clean_text.split()
            if words:
                first_word = words[0]
                # 兼容前缀：无论用户是否输入了 / 符号，均能匹配
                cmd_key_no_prefix = first_word[1:] if first_word.startswith("/") else first_word
                
                if (first_word in self.foreign_commands or cmd_key_no_prefix in self.foreign_commands):
                    logger.debug(f"[AstrMai-Sensor] 🛑 隔离网关：精准识别到指令 [{first_word}]，彻底拦截。")
                    # 注入强信号，通知整个框架的后续环节该消息免检
                    event.set_extra("astrmai_is_command", True)
                    return False

        # 4. 空消息检查 (过滤纯表情或意外的空包)
        if not clean_text and not has_payload:
            return False

        # 5. 昵称加权机制
        raw_msg = event.message_str or ""
        nicknames = self.config.get('bot_nicknames', [])
        if nicknames and raw_msg:
            for nickname in nicknames:
                if nickname and nickname in raw_msg:
                    logger.debug(f"[AstrMai-Sensor] 🔔 触发昵称点名: {nickname}")
                    event.set_extra("astrmai_bonus_score", 1.0) # 提权标记
                    return True

        # 如果需要，可在此处扩展白名单/黑名单逻辑
        return True

    def is_wakeup_signal(self, event: AstrMessageEvent, bot_self_id: str) -> bool:
        """检测是否为强唤醒信号 (@Bot)"""
        # 如果已被预过滤器判定为指令，绝对不构成唤醒
        if event.get_extra("astrmai_is_command"):
            return False

        if not event.message_obj or not event.message_obj.message:
            return False
            
        try:
            for component in event.message_obj.message:
                if isinstance(component, Comp.At):
                    if str(component.qq) == str(bot_self_id):
                        return True
        except Exception:
            pass
            
        # 昵称唤醒已在 should_process_message 提权，此处若需强唤醒也可复用检测
        return False