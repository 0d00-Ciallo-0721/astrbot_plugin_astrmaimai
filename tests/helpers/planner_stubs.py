import sys
import types


def install_planner_stubs():
    def _make_module(name):
        module = types.ModuleType(name)
        sys.modules[name] = module
        return module

    gateway_mod = _make_module("astrmai.infra.gateway")
    gateway_mod.GlobalModelGateway = type("GlobalModelGateway", (), {})

    context_engine_mod = _make_module("astrmai.Brain.context_engine")
    context_engine_mod.ContextEngine = type("ContextEngine", (), {})

    executor_mod = _make_module("astrmai.Brain.executor")

    class ConcurrentExecutor:
        def __init__(self, *args, **kwargs):
            pass

    executor_mod.ConcurrentExecutor = ConcurrentExecutor

    reply_engine_mod = _make_module("astrmai.Brain.reply_engine")
    reply_engine_mod.ReplyEngine = type("ReplyEngine", (), {})

    goal_manager_mod = _make_module("astrmai.Brain.goal_manager")

    class GoalManager:
        def __init__(self, *args, **kwargs):
            pass

        async def analyze_and_update(self, *args, **kwargs):
            return ""

        def get_goals_context(self, *args, **kwargs):
            return ""

    goal_manager_mod.GoalManager = GoalManager

    action_modifier_mod = _make_module("astrmai.Brain.action_modifier")

    class ActionModifier:
        def __init__(self, *args, **kwargs):
            pass

        def modify_tools(self, tools, **kwargs):
            return tools

    action_modifier_mod.ActionModifier = ActionModifier

    expression_selector_mod = _make_module("astrmai.Brain.expression_selector")

    class ExpressionSelector:
        def __init__(self, *args, **kwargs):
            pass

        async def select(self, *args, **kwargs):
            return ""

    expression_selector_mod.ExpressionSelector = ExpressionSelector

    pfc_mod = _make_module("astrmai.Brain.tools.pfc_tools")
    local_pfc_mod = _make_module("astrmai.conversation.planning.tools.pfc_tools")

    def _tool_class(name):
        class Tool:
            def __init__(self, *args, **kwargs):
                self.name = name
                self.description = name

        Tool.__name__ = name
        return Tool

    for tool_name in [
        "WaitTool",
        "OmniPerceptionTool",
        "QQFriendLookupTool",
        "QQGroupMemberLookupTool",
        "QQUserIdentityLookupTool",
        "QQForwardMessageLookupTool",
        "QQGroupPresenceLookupTool",
        "QQRecentContactLookupTool",
        "QQMessageArtifactLookupTool",
        "VisionMessageAnalyzeTool",
        "CrossSessionReplyLookupTool",
        "QQCustomFaceSendTool",
        "QuoteReplyActionTool",
        "QQMessageRecallLookupTool",
        "TopicThreadLookupTool",
        "BotCapabilityLookupTool",
        "MemoryWriteCorrectionTool",
        "UnverifiedReportRecordTool",
        "PersonaFactCheckTool",
        "GroupActivitySnapshotTool",
        "ContactRouteSuggestTool",
        "CrossChatMemoryQueryTool",
        "ConstructAtEventTool",
        "CustomFaceCatalogQueryTool",
        "GroupSignTool",
        "ProactivePokeTool",
        "ProactiveMemeTool",
        "MemeResonanceTool",
        "TopicHijackTool",
        "SpaceTransitionTool",
        "RegretAndWithdrawTool",
        "MessageEmojiLikeTool",
        "MessageReactionTool",
        "ProactiveLikeTool",
        "SelfLoreQueryTool",
    ]:
        tool_cls = _tool_class(tool_name)
        setattr(pfc_mod, tool_name, tool_cls)
        setattr(local_pfc_mod, tool_name, tool_cls)

    memory_engine_mod = _make_module("astrmai.memory.engine")
    memory_engine_mod.MemoryEngine = type("MemoryEngine", (), {})

    evolution_mod = _make_module("astrmai.evolution.processor")
    evolution_mod.EvolutionManager = type("EvolutionManager", (), {})
