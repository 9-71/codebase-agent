# Codebase Agent

A local-first, observable tool-calling agent for exploring an unfamiliar codebase. It exposes three tools to the model—`search_code`, `read_file`, and `run_pytest`—through a small HTTP API. It is intended for local development and self-hosting, not autonomous code modification.

For example, with a tool-calling model you can ask it to find an implementation, inspect relevant source, and run an eligible test file. This describes a possible workflow, not a recorded demo or a guarantee that a repository has matching tests. The default fake model is only a deterministic smoke-test backend; use the Ollama-compatible backend for model-driven exploration.

## Features

- `search_code` returns up to 20 file-and-line matches, skipping common generated directories, binary suffixes, and files over 512,000 bytes. `read_file` returns a bounded line range from a file under `REPO_ROOT` of at most that size.
- `run_pytest` runs one eligible test file with fixed pytest arguments and a timeout. The API response includes an answer, tool-call records, evidence, and a trace ID.
- The fake model supports offline smoke tests; an Ollama-compatible chat-completions backend supports model-driven tool calls.
- Model calls have timeouts and bounded retries with exponential backoff. Tool calls have timeouts, and failed agent requests return a structured error with a trace ID.
- An in-memory TTL cache keys responses by session, message, and the latest observed repository file modification time. Responses report cache hits.
- `/metrics` exposes Prometheus HTTP request counts by status, latency histograms, and cache hit/miss counters; `/metrics/json` provides process-local diagnostics.
- A Dockerfile and Docker Compose setup support local operation, including an optional Ollama service.

## Quick Start: Local

Requires Python 3.10 or newer. Run these commands from this repository's root (the directory containing `pyproject.toml`). On Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

On Windows PowerShell, create and activate the environment with:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Then run the same install and server commands. The default `MODEL_BACKEND=fake` needs no API key or model download. In another terminal, check the service and send a smoke-test request:

```bash
curl http://127.0.0.1:8000/healthz
curl -X POST http://127.0.0.1:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo","message":"search FastAPI"}'
```

For PowerShell, use:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/v1/chat -Method Post -ContentType 'application/json' -Body '{"session_id":"demo","message":"search FastAPI"}'
```

The fake model deterministically calls `search_code` for `FastAPI`; its answer is canned and does not analyze the search results. Set `REPO_ROOT` before starting the service to explore another repository.

## Architecture

```text
POST /v1/chat (FastAPI)
       |
       v
AgentService -- TTL response cache
       |
       +-- FakeModel or Ollama-compatible model
       |          | tool calls
       +----------v
          CodebaseTools -- search_code / read_file / run_pytest
                 |
                 +-- REPO_ROOT (pytest runs a selected test file)

HTTP middleware -> Prometheus metrics; AgentService -> diagnostic counters
```

The service sends the user request and tool definitions to the selected model, executes requested tools, and returns bounded tool output for a subsequent model turn. The agent loop has a configurable turn limit. No tool directly edits source files, though pytest runs repository code that can have side effects.

## Model backends and Docker

The default `fake` backend chooses one canned tool call from the input and returns a fixed response after the tool result. It does not perform multi-step codebase reasoning.

For model-driven use, set `MODEL_BACKEND=ollama`, `OLLAMA_BASE_URL` to an OpenAI-compatible `/v1` endpoint, and `OLLAMA_MODEL` to a model available there before starting the service. For example, set `export MODEL_BACKEND=ollama` on Linux/macOS or `$env:MODEL_BACKEND='ollama'` in PowerShell, then run the Quick Start server command. The configured default model name is `qwen2.5:3b`; this repository does not download or provision it.

To run the fake-backed service in Docker (Docker and Docker Compose required):

```bash
docker compose up --build agent
```

Compose mounts this repository at `/workspace` read-only and sets `REPO_ROOT=/workspace`. Tests that need to write there can fail. The optional Ollama profile starts an Ollama container, but you must make the configured model available in it before model-driven requests can succeed. Set `MODEL_BACKEND=ollama` in your environment and run:

```bash
docker compose --profile ollama up --build
```

Inside Compose, the configured endpoint defaults to `http://ollama:11434/v1`.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `MODEL_BACKEND` | `fake` | `ollama` selects the Ollama adapter; any other value selects the fake model |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible model endpoint; Compose overrides this |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Model name; must be available at the endpoint |
| `REPO_ROOT` | `.` | Repository root resolved at service startup |
| `TOOL_TIMEOUT_SECONDS` | `3` | Per-tool timeout, including pytest |
| `MODEL_TIMEOUT_SECONDS` | `20` | Per-model-call timeout |
| `MODEL_RETRY_ATTEMPTS` | `3` | Maximum model attempts |
| `RETRY_BACKOFF_SECONDS` | `0.1` | Initial retry backoff in seconds |
| `MAX_TOOL_CALLS` | `4` | Maximum model/tool loop turns |
| `MAX_CONTEXT_CHARS` | `16000` | Per-tool output context limit |
| `CACHE_TTL_SECONDS` | `60` | In-memory cache lifetime in seconds |

## API and metrics

### `POST /v1/chat`

Request:

```json
{"session_id":"demo","message":"search FastAPI"}
```

Response fields include `answer`, `evidence`, `tool_calls`, `trace_id`, and `cache_hit`. A failed agent execution returns HTTP 503 with `error_code`, `message`, and `trace_id`.

### `GET /healthz`

Returns service status, model backend, and configured repository root.

### `GET /metrics` and `GET /metrics/json`

The Prometheus endpoint exposes `codebase_agent_http_requests_total`, `codebase_agent_http_request_duration_seconds`, `codebase_agent_cache_hits_total`, and `codebase_agent_cache_misses_total`. The JSON endpoint returns process-local request, success, failure, timeout, retry, and cache counters plus average duration. Interactive OpenAPI documentation is available at `/docs` while the service runs.

## Safety and limitations

No arbitrary shell or file-writing tool is exposed to the model. `read_file` resolves requested paths within `REPO_ROOT`; reads and search are size-bounded. The model can still request any readable file within that root, so configure `REPO_ROOT` with care. Repository text is sent back to the model as length-limited tool output alongside a system instruction to treat it as untrusted evidence. That instruction is not a guarantee against prompt injection.

`run_pytest` accepts a relative path beginning with `tests/` whose filename matches `test_*.py`. It resolves the selected file within the repository's `tests` directory; for a missing direct path, it also checks the corresponding path under `tests/fixture_repo/`. It then runs `python -m pytest` on that file from `REPO_ROOT` with `-q -p no:cacheprovider`. There is no arbitrary command parameter. The runner drains stdout and stderr in chunks of up to 8,192 bytes, keeps at most 4,000 characters from each stream while running, and returns at most 4,000 characters from the combined stdout-then-stderr tails.

For this invocation, the runner stops the POSIX process group or Windows Job Object on timeout, cancellation, and ordinary parent exit, then waits for the pytest process to exit. Async cancellation also waits for the worker's cleanup before returning. On Windows, pytest starts suspended until assignment to the Job Object succeeds. This manages normal descendant processes; it is not a sandbox. Tests execute repository code and may write files, use the network, or consume resources. Pytest's own capture buffers are not bounded by the runner. POSIX descendants that deliberately leave the process group can escape cleanup, and abrupt termination of the agent itself is outside these cleanup controls. Use trusted repositories and an appropriately isolated environment.

## Testing

From the repository root after installing `requirements.txt`:

```bash
python -m pytest
```

The tests cover tool routing and caching, path and pytest restrictions, retries and timeouts, the HTTP contract, bounded pytest output, and process-tree cleanup. They use a small fixture repository under `tests/fixture_repo`. The CI workflow runs the suite on Linux and Windows.

## Project Layout

```text
app/main.py                    FastAPI application and HTTP metrics middleware
app/agent.py                   Agent loop, cache, retries, and service metrics
app/tools.py                   Codebase tools and path/test restrictions
app/_pytest_runner.py          Bounded pytest output and process cleanup
app/_windows_process.py        Windows Job Object and pipe helpers
app/model.py                   Fake model and Ollama-compatible model adapter
app/config.py                  Environment-backed settings
app/cache.py                   In-memory TTL cache
app/models.py                  API request and response models
app/prometheus.py              Dependency-free Prometheus collectors
tests/test_agent.py            Agent, tool, and API contract tests
tests/test_pytest_resources.py Pytest output and process lifecycle tests
tests/fixture_repo/            Small fixture repository
Dockerfile                     Container image definition
docker-compose.yml             Fake and optional Ollama service definitions
.env.example                   Example environment variables
requirements.txt              Local and CI dependencies
pyproject.toml                 Package metadata and pytest configuration
.github/workflows/tests.yml    Linux and Windows test workflow
```

## License

Apache-2.0
