import json
from typing import Any, Protocol
from urllib.request import Request, urlopen


class Model(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class FakeModel:
    """Deterministic backend for tests and a zero-key smoke test."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if messages[-1]["role"] == "tool":
            return {"content": "已根据工具返回的代码证据完成分析。", "tool_calls": []}
        user_text = messages[-1]["content"].lower()
        if "../" in user_text or "secret.txt" in user_text:
            return {
                "content": "Validating that the requested path is inside the working directory.",
                "tool_calls": [{"name": "read_file", "arguments": {"path": "../secret.txt"}}],
            }
        if "test" in user_text:
            return {"content": "建议先运行白名单 pytest，并根据结果定位失败模块。", "tool_calls": [{"name": "run_pytest", "arguments": {"test_path": "tests/test_smoke.py"}}]}
        if "read" in user_text or "文件" in user_text:
            return {"content": "我先读取目标文件，再依据代码证据回答。", "tool_calls": [{"name": "read_file", "arguments": {"path": "README.md", "start_line": 1, "end_line": 20}}]}
        return {"content": "我先搜索代码库中的相关实现，再给出带文件位置的回答。", "tool_calls": [{"name": "search_code", "arguments": {"query": "FastAPI"}}]}


class OllamaModel:
    def __init__(self, base_url: str, model: str, timeout: float = 20) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        payload = json.dumps({"model": self.model, "messages": messages, "tools": tools, "temperature": 0}).encode()
        request = Request(self.url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        message = data["choices"][0]["message"]
        calls = []
        for call in message.get("tool_calls", []):
            function = call["function"]
            calls.append({"name": function["name"], "arguments": json.loads(function.get("arguments", "{}"))})
        return {"content": message.get("content", ""), "tool_calls": calls}
