from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig

# 暂时导入占位，后续实现具体逻辑
# from astrmai.infra import InfraService
# from astrmai.Heart import System1
# from astrmai.Brain import System2
# from astrmai.evolution import Evolution

@register("astrmai", "Gemini Antigravity", "AstrMai: Dual-Process Architecture Plugin", "1.0.0", "https://github.com/astrmai")
class AstrMaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info(f"[AstrMai] Initializing with Sys1: {config.get('system1_provider_id')} | Sys2: {config.get('system2_provider_id')}")

        # --- Layer Initialization (按照工程蓝图构建) ---
        # self.infra = InfraService(context)
        # self.system1 = System1(self.infra) 
        # self.system2 = System2(self.infra)
        # self.evolution = Evolution(self.infra)
        
        logger.info("[AstrMai] Dual-Process Architecture Loaded (Skeleton Mode).")

    @filter.command("mai")
    async def mai_help(self, event: AstrMessageEvent):
        '''AstrMai 帮助指令'''
        help_text = (
            "🤖 **AstrMai (v1.0.0)**\n"
            "-----------------------\n"
            "双系统认知架构已加载。\n"
            "当前状态: Skeleton Mode\n"
            "System 1 (Intuition): Pending\n"
            "System 2 (Brain): Pending"
        )
        yield event.plain_result(help_text)

    async def terminate(self):
        '''插件卸载清理'''
        logger.info("[AstrMai] Terminating...")