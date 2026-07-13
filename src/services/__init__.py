# Namespace package for service-layer wrappers
from utils import LLMService
from config import Config

# Lazily constructed so LLM-free code paths (geometry, design_session,
# verification scripts) can import services.* without LLM credentials.
_global_llm_service = None


def __getattr__(name):
    global _global_llm_service
    if name == "global_llm_service":
        if _global_llm_service is None:
            _global_llm_service = LLMService(Config())
        return _global_llm_service
    raise AttributeError(f"module 'services' has no attribute {name!r}")
