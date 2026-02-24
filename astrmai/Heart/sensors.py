# astrmai/Heart/sensors.py
import re
from typing import List
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp
from astrbot.core.star.command_management import list_commands

class PreFilters:
    """
    感知与过滤器 (System 1: Fused Version)
    职责: 构建指令防火墙，执行严格的消息清洗与拦截。
    """
    def __init__(self, config: dict):
        self.config = config or {}
        self.foreign_commands = set()
        self._commands_loaded = False 

    async def _load_foreign_commands(self):
        """异步动态加载系统内所有注册指令"""
        if self._commands_loaded:
            return

        try:
            all_cmds = await list_commands()
            if all_cmds:
                for cmd in all_cmds:
                    main_cmd = cmd.get("effective_command")
                    if main_cmd: 
                        self.foreign_commands.add(main_cmd.lower())
                    for alias in cmd.get("aliases", []):
                        self.foreign_commands.add(alias.lower())
            
            # 追加配置中自定义的拦截词
            extra_cmds = self.config.get("extra_command_list", [])
            for extra in extra_cmds:
                if extra:
                    self.foreign_commands.add(extra.lower())
            
            logger.info(f"[AstrMai-Sensor] 🛡️ 指令防火墙已加载。共监控 {len(self.foreign_commands)} 个指令词。")
            self._commands_loaded = True
        except Exception as e:
            logger.warning(f"[AstrMai-Sensor] ⚠️ 加载外部指令列表失败: {e}")

    async def should_process_message(self, event: AstrMessageEvent) -> bool:
        """
        核心网关：判断是否应该处理这条消息，并打上指令隔离标签。
        """
        await self._load_foreign_commands()

        # 1. 忽略 Bot 自身发出的消息
        if event.get_sender_id() == event.get_self_id():
            return False

        # 2. 深度清洗文本与负载检测
        clean_text_parts = []
        has_payload = False
        image_urls = []
        
        if event.message_obj and event.message_obj.message:
            for seg in event.message_obj.message:
                if isinstance(seg, (Comp.At, Comp.Reply)):
                    continue 
                if isinstance(seg, Comp.Plain):
                    text = seg.text.replace('\u200b', '').strip()
                    if text: 
                        clean_text_parts.append(text)
                if isinstance(seg, (Comp.Image, Comp.Video, Comp.Record, Comp.File)):
                    has_payload = True
                if isinstance(seg, Comp.Image) and seg.url:
                    image_urls.append(seg.url)
        
        clean_text = " ".join(clean_text_parts).strip().lower()
        
        # 记录提取的图片信息，供 AttentionGate 放入 LastMessageMetadata
        event.set_extra("extracted_image_urls", image_urls)
        
        # 3. 🚨 核心指令拦截防火墙 🚨
        if clean_text:
            words = clean_text.split()
            if words:
                first_word = words[0]
                cmd_key_no_prefix = first_word[1:] if first_word.startswith("/") else first_word
                
                if (first_word in self.foreign_commands or cmd_key_no_prefix in self.foreign_commands):
                    logger.debug(f"[AstrMai-Sensor] 🛑 隔离网关：精准识别到指令 [{first_word}]，彻底拦截。")
                    event.set_extra("astrmai_is_command", True)
                    return False

        # 4. 空消息检查
        if not clean_text and not has_payload:
            return False

        # 5. 昵称点名提权
        raw_msg = event.message_str or ""
        nicknames = self.config.get('bot_nicknames', [])
        if nicknames and raw_msg:
            for nickname in nicknames:
                if nickname and nickname in raw_msg:
                    logger.debug(f"[AstrMai-Sensor] 🔔 触发昵称点名: {nickname}")
                    event.set_extra("astrmai_bonus_score", 1.0) 
                    return True

        return True

    def is_wakeup_signal(self, event: AstrMessageEvent, bot_self_id: str) -> bool:
        """检测是否为强唤醒信号 (@Bot)"""
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
            
        return False

    async def is_command(self, text: str) -> bool:
        """
        [新增] 判断文本是否命中指令防火墙
        """
        if not text: return False
        
        # 1. 检查基础指令前缀
        if text.startswith(("/", "!", "！")):
            return True
            
        # 2. 检查动态加载的系统指令库
        first_word = text.split()[0].lower()
        if self.foreign_commands and first_word in self.foreign_commands:
            return True
            
        return False            