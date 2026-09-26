# Codebase Agent

A local, observable tool-calling agent for searching a codebase, reading bounded source files, and running allowlisted pytest checks.

The service is designed for local development and self-hosted deployments. It supports a deterministic fake model for offline testing and an Ollama-compatible model backend for local inference.

## Features

- **Tool Calling**: routes requests to `search_code`, `read_file`, and `run_pytest` tools.
- **Path Safety**: rejects path traversal, restricts pytest to `tests/test_*.py`, and never exposes arbitrary shell execution.
- **Read-Only Repository Access**: limits file size and prevents repository content from overriding system instructions.
- **TTL In-Memory Cache**: caches responses by session, message, and repository revision.
- **Resilience Controls**: applies model and tool timeouts, bounded retries, exponential backoff, and structured errors.
- **Observability**: exposes Prometheus HTTP request counters, status codes, latency histograms, and cache hit/miss counters.
- **Container Support**: includes a Dockerfile and Docker Compose configuration for fake or Ollama-backed operation.

## Requirements

- Python 3.10 or newer
- Docker and Docker Compose for containerized operation
- Ollama is optional and only required for local model inference

## Quick Start: Local

From the repository root:

```bash
cd projects/codebase-agent
python -m venv .venv

# Windows PowerShell
.venv\\Scripts\\Activate.ps1

# Linux/macOS
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

The default `MODEL_BACKEND=fake` requires no API key or model download.

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/metrics
curl -X POST http://127.0.0.1:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id":"demo","message":"search FastAPI"}'
```

Run the test suite:

```bash
python -m pytest
```

## Quick Start: Docker

Start the service with the deterministic fake model:

```bash
docker compose up --build agent
```

To use Ollama, start the optional Ollama profile and configure the model backend:

```bash
$env:MODEL_BACKEND="ollama"          # PowerShell
export MODEL_BACKEND=ollama           # Linux/macOS
docker compose --profile ollama up --build
```

Set `OLLAMA_MODEL` to a model available in the Ollama container. The default endpoint is the OpenAI-compatible URL `http://ollama:11434/v1` inside Compose.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `MODEL_BACKEND` | `fake` | `fake` or `ollama` |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible model endpoint |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Model name |
| `REPO_ROOT` | `.` | Read-only repository root |
| `TOOL_TIMEOUT_SECONDS` | `3` | Per-tool timeout |
| `MODEL_TIMEOUT_SECONDS` | `20` | Per-model-call timeout |
| `MODEL_RETRY_ATTEMPTS` | `3` | Maximum model attempts |
| `MAX_CONTEXT_CHARS` | `16000` | Tool output context budget |
| `CACHE_TTL_SECONDS` | `60` | In-memory cache lifetime |

## API

### `POST /v1/chat`

Request:

```json
{"session_id":"demo","message":"search FastAPI"}
```

Response fields include `answer`, `evidence`, `tool_calls`, `trace_id`, and `cache_hit`.

### `GET /healthz`

Returns service status, model backend, and configured repository root.

### `GET /metrics`

Returns Prometheus exposition format with:

- `codebase_agent_http_requests_total`
- `codebase_agent_http_request_duration_seconds`
- `codebase_agent_cache_hits_total`
- `codebase_agent_cache_misses_total`

### `GET /metrics/json`

Returns process-local diagnostic counters in JSON, including requests, failures, retries, cache hits, cache misses, and average duration.

Interactive OpenAPI documentation is available at `/docs` when the service is running.

## Security Model

The agent can only read files below `REPO_ROOT`. Path traversal is rejected with a structured tool error. File reads are size- and line-bounded. Pytest execution accepts only paths under `tests/` whose filename matches `test_*.py`; arbitrary shell commands are not exposed.

The pytest runner continuously drains stdout and stderr in fixed-size chunks, retains at most 4,000 characters per stream, and returns the last 4,000 characters of stdout followed by stderr. Timeout, cancellation, and ordinary exit clean up the invocation's POSIX process group or Windows Job Object; async cancellation waits for that cleanup. Windows starts pytest suspended until job assignment succeeds.

These controls do not sandbox test code or limit its own memory, files, network access, or pytest-internal capture. POSIX children that deliberately leave the process group can escape cleanup, and abrupt termination of the agent itself is outside this cleanup guarantee.

Repository text is treated as untrusted evidence. It is length-limited before being passed back to the model and cannot replace the system instruction.

## Project Layout

```text
app/main.py          FastAPI application and HTTP metrics middleware
app/agent.py         Agent loop, cache, retries, and service metrics
app/tools.py         Read-only codebase tools and safety checks
app/model.py         Fake model and Ollama-compatible model adapter
app/prometheus.py    Dependency-free Prometheus collectors
tests/               Unit and API contract tests
Dockerfile           Container image definition
docker-compose.yml   Fake and optional Ollama service definitions
```

## License

Apache-2.0
