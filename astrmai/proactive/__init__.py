from importlib import import_module

__all__ = [
    "DecayService",
    "DiaryService",
    "DreamScheduler",
    "ProactiveTask",
    "ReviewDispatcher",
    "WakeupService",
    "describe_proactive_capabilities",
]


async def describe_proactive_capabilities(task, *, enabled: bool, dream_visible: bool) -> dict:
    task_status = {"running": False}
    review_status = {"ready": False, "pending": 0}
    dream_status = {
        "dream_visible": dream_visible,
        "interval_seconds": 0,
        "last_dream_time": 0.0,
        "dream_agent_bound": False,
        "dream_generator_bound": False,
    }
    if task and hasattr(task, "describe_status"):
        task_status = task.describe_status()
    if task and getattr(task, "review_dispatcher", None) and hasattr(task.review_dispatcher, "describe_status"):
        review_status = await task.review_dispatcher.describe_status()
    if task and getattr(task, "dream_scheduler", None) and hasattr(task.dream_scheduler, "describe_status"):
        dream_status = task.dream_scheduler.describe_status()
    return {
        "enabled": enabled,
        "dream_visible": dream_visible,
        "task_status": task_status,
        "dream_scheduler": dream_status,
        "review_dispatcher": review_status,
    }


def __getattr__(name):
    module_map = {
        "DecayService": ".decay_service",
        "DiaryService": ".diary_service",
        "DreamScheduler": ".dream_scheduler",
        "ProactiveTask": ".proactive_task",
        "ReviewDispatcher": ".review_dispatcher",
        "WakeupService": ".wakeup_service",
    }
    module_name = module_map.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
