# utils/pre_filters.py
import random
import re
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp
from astrbot.core.star.command_management import list_commands

# (使用相对路径导入 v4.0 模块)
from ..config import HeartflowConfig

class PreFilters:
    """
    (新) v4.0 消息预过滤器
    职责：负责 _should_process_message 逻辑
    来源：迁移自 v3.5 utils.py
    """

    def __init__(self, config: HeartflowConfig):
        # (v4.0) 依赖注入
        self.config = config
        # [新增] 外部指令缓存 (Set[str])
        self.foreign_commands = set()
    
    def add_ignored_commands(self, cmds: list):
        """
        手动将一组指令添加到拦截名单中
        """
        if not cmds: return
        for cmd in cmds:
            if cmd:
                self.foreign_commands.add(cmd.lower())
        logger.debug(f"HeartCore: 已手动添加 {len(cmds)} 个内部拦截指令。")
        
    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """
        (v8.1 修复) 手动检查是否为 @Bot 事件
        """
        if not event.message_obj or not event.message_obj.message:
            return False
            
        try:
            for component in event.message_obj.message:
                if isinstance(component, Comp.At):
                    if str(component.qq) == str(event.get_self_id()):
                        return True # 是 @Bot
        except Exception:
            return False # 捕获异常
        return False # 不是 @Bot

    async def load_foreign_commands(self):
        """
        加载指令集 (系统注册 + 配置手动补充)
        """
        try:
            # 1. 获取系统注册的所有指令
            all_cmds = await list_commands()
            count = 0
            
            if all_cmds:
                for cmd in all_cmds:
                    # [关键修复] 删除了原本的排除逻辑
                    # 原错误代码: if cmd.get('plugin') == 'heartcore': continue
                    # 现在逻辑: 无论是不是自己的指令，只要是指令，都加入黑名单，防止进入 LLM
                    
                    # 添加主指令
                    main_cmd = cmd.get("effective_command")
                    if main_cmd: 
                        self.foreign_commands.add(main_cmd.lower())
                        count += 1
                    
                    # 添加别名
                    for alias in cmd.get("aliases", []):
                        self.foreign_commands.add(alias.lower())
                        count += 1
            
            # 2. 添加配置中手动补充的指令 (Extra Commands)
            for extra in self.config.extra_command_list:
                if extra:
                    self.foreign_commands.add(extra.lower())
                    count += 1
            
            logger.info(f"💖 HeartCore: 指令防火墙已加载。共监控 {len(self.foreign_commands)} 个指令词。")
            
        except Exception as e:
            logger.warning(f"HeartCore: 加载外部指令列表失败: {e}")


    def should_process_message(self, event: AstrMessageEvent) -> bool:
        """
        判断是否应该处理这条消息 (返回 False 则拦截)
        修复版：增强指令识别，并注入 'heartflow_is_command' 标记
        """
        # 1. 忽略自己发送的消息
        if event.get_sender_id() == event.get_self_id():
            return False

        # --- [增强] 预处理与清洗 ---
        clean_text_parts = []
        has_payload = False
        
        if event.message_obj and event.message_obj.message:
            for seg in event.message_obj.message:
                if isinstance(seg, (Comp.At, Comp.Reply)):
                    continue 
                if isinstance(seg, Comp.Plain):
                    # 彻底移除零宽空格并去除首尾空白
                    text = seg.text.replace('\u200b', '').strip()
                    if text: clean_text_parts.append(text)
                if isinstance(seg, (Comp.Image, Comp.Video, Comp.Record, Comp.File)):
                    has_payload = True
        
        # 组合纯文本并转小写
        clean_text = " ".join(clean_text_parts).strip().lower()
        
        # --- [关键] 指令拦截 (第一优先级) ---
        if clean_text:
            words = clean_text.split()
            if words:
                first_word = words[0]
                # 兼容带 '/' 和不带 '/' 的情况
                cmd_key_no_prefix = first_word[1:] if first_word.startswith("/") else first_word
                
                if (first_word in self.foreign_commands or 
                    cmd_key_no_prefix in self.foreign_commands):
                    
                    logger.debug(f"💖 HeartCore 隔离：识别到指令 [{first_word}]，已标记并拦截。")
                    # [核心操作] 注入指令标记，防止后续任何环节误存
                    event.set_extra("heartflow_is_command", True)
                    return False

        # --- 后续逻辑 (仅当不是指令时执行) ---
        
        # 3. 空消息检查
        if not clean_text and not has_payload:
            if not event.get_extra("heartflow_is_poke_event"):
                return False

        # 4. 昵称点名检测
        raw_msg = event.message_str or ""
        if self.config.bot_nicknames and raw_msg:
            for nickname in self.config.bot_nicknames:
                if nickname and nickname in raw_msg:
                    logger.debug(f"心流点名：检测到昵称 {nickname}。")
                    event.set_extra("heartflow_bonus_score", 1.0)
                    return True

        # 5. 白名单检查
        if self.config.whitelist_enabled:
            chat_id = event.unified_msg_origin
            if chat_id not in self.config.chat_whitelist:
                return False

        # 6. 黑名单检查
        sender_id = event.get_sender_id()
        if sender_id in self.config.user_blacklist:
            if random.random() > self.config.blacklist_pass_probability:
                return False

        return True