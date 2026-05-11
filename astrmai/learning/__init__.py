from importlib import import_module

__all__ = [
    "BotReplyRecorder",
    "BotReplyRecordedEvent",
    "EvolutionManager",
    "ExpressionAutoCheckTask",
    "ExpressionReflector",
    "ExpressionReviewService",
    "MessageRecorder",
    "MiningCompletedEvent",
    "ReviewItem",
    "ReflectTracker",
    "UserMessageRecordedEvent",
]


def __getattr__(name):
    module_map = {
        "BotReplyRecorder": ".logging",
        "BotReplyRecordedEvent": ".contracts",
        "EvolutionManager": ".evolution_manager",
        "ExpressionAutoCheckTask": ".review.expression_auto_check_task",
        "ExpressionReflector": ".review.reflector",
        "ExpressionReviewService": ".review.review_service",
        "MessageRecorder": ".logging",
        "MiningCompletedEvent": ".contracts",
        "ReviewItem": ".contracts",
        "ReflectTracker": ".review.reflect_tracker",
        "UserMessageRecordedEvent": ".contracts",
    }
    module_name = module_map.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
