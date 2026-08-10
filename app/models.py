from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any]
    ok: bool
    duration_ms: float
    error: str | None = None


class ChatResponse(BaseModel):
    answer: str
    evidence: list[str]
    tool_calls: list[ToolCall]
    trace_id: str
    cache_hit: bool = False


class HealthResponse(BaseModel):
    status: str
    model_backend: str
    repo_root: str


class MetricsResponse(BaseModel):
    requests: int
    successes: int
    failures: int
    timeouts: int
    retries: int
    cache_hits: int
    cache_misses: int
    average_duration_ms: float
