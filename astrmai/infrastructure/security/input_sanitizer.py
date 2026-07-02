"""Input sanitizer — unified entry point for prompt injection defense.

Currently wraps PromptEnvelope's sanitize methods.
Future: WebUI API body sanitization, DB write sanitization.
"""


class InputSanitizer:
    @staticmethod
    def sanitize(text: str) -> str:
        """Wrap user-supplied text in boundary tags to prevent prompt injection.

        TODO: Add HTML/script tag stripping for WebUI API body sanitization.
        """
        from ...conversation.contracts.prompt_envelope import PromptEnvelope

        return PromptEnvelope.sanitize_user_input(text)

    @staticmethod
    def sanitize_memory(text: str) -> str:
        """Wrap retrieved-memory content to prevent persistent prompt injection."""
        from ...conversation.contracts.prompt_envelope import PromptEnvelope

        return PromptEnvelope.sanitize_memory_content(text)


__all__ = ["InputSanitizer"]
