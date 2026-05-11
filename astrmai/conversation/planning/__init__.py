from importlib import import_module

_EXPORTS = {
    "CognitiveDecision": ".cognitive_loop",
    "CognitiveLoop": ".cognitive_loop",
    "CognitiveLoopGateDecision": ".cognitive_loop",
    "BehaviorTuningPolicy": ".behavior_tuning",
    "ContextEngine": ".context_engine",
    "ActionModifier": ".expression_policy",
        "AgencyReflection": ".agency_runtime",
        "AgencyRuntimeStore": ".agency_runtime",
        "AgencyReflectionBridge": ".agency_feedback_bridge",
    "ExpressionSelector": ".expression_policy",
    "GoalManager": ".goal_service",
    "MessageRenderer": ".message_renderer",
    "Planner": ".planner",
    "PlanningInputLoader": ".planning_input_loader",
    "PreBudgetInputs": ".planning_input_loader",
    "ToolStateInputs": ".planning_input_loader",
    "build_prompt_envelope": ".prompt_builder",
    "PromptRefiner": ".prompt_refiner",
    "ThinkLevelDecision": ".think_level_policy",
    "ThinkLevelPolicy": ".think_level_policy",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(f"{__name__}{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value
