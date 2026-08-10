"""Small dependency-free Prometheus collector for the local demo service."""

from collections import defaultdict
from threading import Lock


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\"", '\\"').replace("\n", "\\n")


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: tuple[str, str] | None = None) -> str:
    pairs = list(zip(names, values))
    if extra:
        pairs.append(extra)
    if not pairs:
        return ""
    return "{" + ",".join(f'{name}="{_escape(value)}"' for name, value in pairs) + "}"


class Counter:
    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self.values: defaultdict[tuple[str, ...], float] = defaultdict(float)
        if not label_names:
            self.values[()] = 0
        self.lock = Lock()

    def inc(self, labels: tuple[str, ...] = (), amount: float = 1) -> None:
        if len(labels) != len(self.label_names):
            raise ValueError(f"{self.name} expects {len(self.label_names)} labels")
        with self.lock:
            self.values[labels] += amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        for labels, value in sorted(self.values.items()):
            lines.append(f"{self.name}{_labels(self.label_names, labels)} {value:g}")
        return lines


class Histogram:
    BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self.values: defaultdict[tuple[str, ...], list[float]] = defaultdict(list)
        self.lock = Lock()

    def observe(self, value: float, labels: tuple[str, ...] = ()) -> None:
        if len(labels) != len(self.label_names):
            raise ValueError(f"{self.name} expects {len(self.label_names)} labels")
        with self.lock:
            self.values[labels].append(value)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        for labels, values in sorted(self.values.items()):
            for bucket in self.BUCKETS:
                count = sum(value <= bucket for value in values)
                lines.append(f"{self.name}_bucket{_labels(self.label_names, labels, ('le', str(bucket)))} {count}")
            lines.append(f'{self.name}_bucket{_labels(self.label_names, labels, ("le", "+Inf"))} {len(values)}')
            lines.append(f"{self.name}_count{_labels(self.label_names, labels)} {len(values)}")
            lines.append(f"{self.name}_sum{_labels(self.label_names, labels)} {sum(values):g}")
        return lines


HTTP_REQUESTS = Counter("codebase_agent_http_requests_total", "Total HTTP requests.", ("method", "path", "status"))
HTTP_LATENCY = Histogram("codebase_agent_http_request_duration_seconds", "HTTP request duration in seconds.", ("method", "path"))
AGENT_CACHE_HITS = Counter("codebase_agent_cache_hits_total", "Agent cache hits.")
AGENT_CACHE_MISSES = Counter("codebase_agent_cache_misses_total", "Agent cache misses.")


def render_prometheus() -> str:
    lines: list[str] = []
    for metric in (HTTP_REQUESTS, HTTP_LATENCY, AGENT_CACHE_HITS, AGENT_CACHE_MISSES):
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"
