class LLMCascadeFailureException(Exception):
    """Raised when every model candidate in the cascade fails."""