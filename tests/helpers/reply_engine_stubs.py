import sys
import types
from types import SimpleNamespace


def install_reply_engine_stubs():
    event_mod = sys.modules["astrbot.api.event"]

    class MessageChain:
        def __init__(self):
            self.chain = []

    event_mod.MessageChain = MessageChain

    message_components_mod = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text):
            self.text = text

    class At:
        def __init__(self, qq):
            self.qq = qq

    message_components_mod.Plain = Plain
    message_components_mod.At = At
    sys.modules["astrbot.api.message_components"] = message_components_mod

    affection_mod = types.ModuleType("astrmai.Heart.affection_router")

    class AffectionRouter:
        @staticmethod
        def route(**kwargs):
            return kwargs.get("fallback_uid")

    affection_mod.AffectionRouter = AffectionRouter
    sys.modules["astrmai.Heart.affection_router"] = affection_mod

    state_engine_mod = types.ModuleType("astrmai.Heart.state_engine")
    state_engine_mod.StateEngine = type("StateEngine", (), {})
    sys.modules["astrmai.Heart.state_engine"] = state_engine_mod

    mood_manager_mod = types.ModuleType("astrmai.Heart.mood_manager")
    mood_manager_mod.MoodManager = type("MoodManager", (), {})
    sys.modules["astrmai.Heart.mood_manager"] = mood_manager_mod

    datamodels_mod = types.ModuleType("astrmai.infra.datamodels")
    datamodels_mod.ChatState = type("ChatState", (), {})
    datamodels_mod.VisualMemory = type("VisualMemory", (), {})
    sys.modules["astrmai.infra.datamodels"] = datamodels_mod

    meme_config_mod = types.ModuleType("astrmai.meme_engine.meme_config")
    meme_config_mod.MEMES_DIR = ""
    sys.modules["astrmai.meme_engine.meme_config"] = meme_config_mod

    meme_sender_mod = types.ModuleType("astrmai.meme_engine.meme_sender")

    async def send_meme(**kwargs):
        return None

    meme_sender_mod.send_meme = send_meme
    sys.modules["astrmai.meme_engine.meme_sender"] = meme_sender_mod


class _FakeConvManager:
    async def get_curr_conversation_id(self, chat_id):
        return "conv-1"

    async def add_message_pair(self, cid, user_message, assistant_message):
        return None


class _FakeContext:
    def __init__(self):
        self.sent = []
        self.conversation_manager = _FakeConvManager()

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class _DummyLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeStateEngine:
    def __init__(self):
        self.gateway = SimpleNamespace(context=_FakeContext())
        self.config = SimpleNamespace(
            reply=SimpleNamespace(
                segment_min_len=4,
                no_segment_max_len=200,
                meme_probability=0,
                emotion_mapping={},
                fallback_text="...",
                typing_speed_factor=0.0,
            ),
            global_settings=SimpleNamespace(debug_mode=False),
        )

    async def get_state(self, chat_id):
        return SimpleNamespace()

    async def atomic_update_mood(self, chat_id, delta=0.0):
        return 0.0

    def _get_user_lock(self, user_id):
        return _DummyLock()

    async def calculate_and_update_affection(self, **kwargs):
        return None


class FakeEvent:
    def __init__(self, sender_id, sender_name, text):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extra = {}

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return "bot-1"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value
