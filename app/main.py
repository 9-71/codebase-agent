import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .agent import AgentExecutionError, AgentService
from .config import settings
from .models import ChatRequest, ChatResponse, HealthResponse, MetricsResponse
from .prometheus import HTTP_LATENCY, HTTP_REQUESTS, render_prometheus

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
service = AgentService(settings)
app = FastAPI(title="Codebase Agent", version="0.1.0")


@app.middleware("http")
async def collect_http_metrics(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        if request.url.path != "/metrics":
            HTTP_REQUESTS.inc((request.method, request.url.path, "500"))
            HTTP_LATENCY.observe(time.perf_counter() - started, (request.method, request.url.path))
        raise
    if request.url.path != "/metrics":
        HTTP_REQUESTS.inc((request.method, request.url.path, str(response.status_code)))
        HTTP_LATENCY.observe(time.perf_counter() - started, (request.method, request.url.path))
    return response


@app.exception_handler(AgentExecutionError)
async def agent_error_handler(request: Request, exc: AgentExecutionError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error_code": "AGENT_UNAVAILABLE", "message": str(exc), "trace_id": exc.trace_id},
    )


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", model_backend=settings.model_backend, repo_root=str(settings.repo_root))


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/metrics/json", response_model=MetricsResponse)
async def metrics_json() -> MetricsResponse:
    average = service.total_duration_ms / service.metrics["requests"] if service.metrics["requests"] else 0.0
    return MetricsResponse(**service.metrics, average_duration_ms=round(average, 2))


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await service.chat(request.session_id, request.message)
