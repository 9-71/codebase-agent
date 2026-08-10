from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    repo_root: Path = Path(os.getenv("REPO_ROOT", ".")).resolve()
    model_backend: str = os.getenv("MODEL_BACKEND", "fake")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    tool_timeout_seconds: float = float(os.getenv("TOOL_TIMEOUT_SECONDS", "3"))
    model_timeout_seconds: float = float(os.getenv("MODEL_TIMEOUT_SECONDS", "20"))
    model_retry_attempts: int = int(os.getenv("MODEL_RETRY_ATTEMPTS", "3"))
    retry_backoff_seconds: float = float(os.getenv("RETRY_BACKOFF_SECONDS", "0.1"))
    max_tool_calls: int = int(os.getenv("MAX_TOOL_CALLS", "4"))
    max_context_chars: int = int(os.getenv("MAX_CONTEXT_CHARS", "16000"))
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "60"))


settings = Settings()
