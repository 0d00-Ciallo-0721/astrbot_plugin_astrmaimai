from types import SimpleNamespace

from astrmai.conversation.ingress.command_guard import check_framework_command


class _Event:
    def __init__(self, activated_handlers=None):
        self.extras = {"activated_handlers": activated_handlers or []}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class _Facade:
    def __init__(self, registered=False):
        self.registered = registered
        self.calls = []

    def is_framework_command(self, message_text):
        self.calls.append(message_text)
        return self.registered


def _handler(filter_ref, *, enabled=True):
    return SimpleNamespace(
        enabled=enabled,
        event_filters=[filter_ref],
        handler_module_path="data.plugins.future_plugin.main",
        handler_name="future_command",
    )


def test_activated_command_handler_is_authoritative_for_future_plugins():
    facade = _Facade(registered=False)
    event = _Event([_handler(SimpleNamespace(command_name="未来命令"))])

    decision = check_framework_command(facade, "未来命令 参数", event=event)

    assert decision.should_passthrough is True
    assert decision.command_name == "未来命令"
    assert decision.owner_module == "data.plugins.future_plugin.main"
    assert decision.handler_name == "future_command"
    assert decision.detection_source == "activated_handler"
    assert facade.calls == []


def test_activated_command_group_is_detected_without_plugin_whitelist():
    facade = _Facade(registered=False)
    event = _Event([_handler(SimpleNamespace(group_name="未来命令组"))])

    decision = check_framework_command(facade, "未来命令组 子命令", event=event)

    assert decision.should_passthrough is True
    assert decision.command_name == "未来命令组"
    assert decision.detection_source == "activated_handler"


def test_disabled_handler_does_not_override_runtime_registry_fallback():
    facade = _Facade(registered=True)
    event = _Event([_handler(SimpleNamespace(command_name="旧命令"), enabled=False)])

    decision = check_framework_command(facade, "/语音 你好", event=event)

    assert decision.should_passthrough is True
    assert decision.command_name == "语音"
    assert decision.detection_source == "runtime_command_registry"
    assert facade.calls == ["/语音 你好"]


def test_non_command_message_continues_astrmai_pipeline():
    facade = _Facade(registered=False)

    decision = check_framework_command(facade, "普通聊天", event=_Event())

    assert decision.should_passthrough is False
    assert decision.reason == ""
    assert facade.calls == ["普通聊天"]


def test_runtime_registry_supports_external_alias_and_astrmai_own_command():
    for message_text, expected_name in (("/语音 你好", "语音"), ("/mai", "mai"), ("/help", "help")):
        facade = _Facade(registered=True)

        decision = check_framework_command(facade, message_text, event=_Event())

        assert decision.should_passthrough is True
        assert decision.command_name == expected_name
        assert decision.detection_source == "runtime_command_registry"
