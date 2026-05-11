from importlib import import_module

__all__ = [
    "AstrMaiBaseSubAgent",
    "ComputerAgent",
    "CronAgent",
    "CronHeartbeatGuard",
    "HandoffRegistry",
    "Sys3Router",
    "describe_workmode_capabilities",
]


async def describe_workmode_capabilities(router, cron_guard, *, enabled: bool) -> dict:
    agents = []
    if router and hasattr(router, "get_static_agent_names"):
        agents = router.get_static_agent_names()
    router_status = {}
    if router and hasattr(router, "describe_status"):
        router_status = await router.describe_status()
    cron_status = {"running": False}
    if cron_guard and hasattr(cron_guard, "describe_status"):
        cron_status = cron_guard.describe_status()
    return {
        "enabled": enabled,
        "agents": agents,
        "router": router_status,
        "cron_guard": cron_status,
    }


def __getattr__(name):
    module_map = {
        "AstrMaiBaseSubAgent": ".subagents.base_agent",
        "ComputerAgent": ".subagents.computer_agent",
        "CronAgent": ".subagents.cron_agent",
        "CronHeartbeatGuard": ".cron_guard.heartbeat",
        "HandoffRegistry": ".tools.handoff_registry",
        "Sys3Router": ".router",
    }
    module_name = module_map.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
