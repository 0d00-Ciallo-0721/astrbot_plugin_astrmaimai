from importlib import import_module

__all__ = ["GlobalModelGateway", "DatabaseService", "ChatState", "UserProfile", "EventBus"]


def __getattr__(name):
    if name == "GlobalModelGateway":
        return import_module(".gateway", __name__).GlobalModelGateway
    if name == "DatabaseService":
        return import_module(".database", __name__).DatabaseService
    if name in {"ChatState", "UserProfile"}:
        datamodels = import_module(".datamodels", __name__)
        return getattr(datamodels, name)
    if name == "EventBus":
        return import_module(".event_bus", __name__).EventBus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
