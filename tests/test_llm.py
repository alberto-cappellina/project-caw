import io
import json
import urllib.error

import pytest

from caw import llm
from caw.llm import LLMError, OpenRouter, make_llm


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_openrouter_request_and_parse(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        return FakeResp(json.dumps({"choices": [{"message": {"content": "[1]"}}]}).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    out = OpenRouter("anthropic/claude-haiku-5.5").complete("sys", "user")
    assert out == "[1]"
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-or-test"
    assert seen["body"]["model"] == "anthropic/claude-haiku-5.5"
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


def test_openrouter_errors(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    def http_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b'{"error":"bad key"}'))

    monkeypatch.setattr(llm.urllib.request, "urlopen", http_error)
    with pytest.raises(LLMError, match="401"):
        OpenRouter().complete("s", "p")

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda r, timeout: FakeResp(b'{"x": 1}'))
    with pytest.raises(LLMError, match="inattesa"):
        OpenRouter().complete("s", "p")


def test_openrouter_requires_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        OpenRouter()


def test_auto_backend_priority(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert make_llm("auto", "haiku", "m").name.startswith("claude-cli")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert make_llm("auto", "haiku", "m").name.startswith("openrouter:")
