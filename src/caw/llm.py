"""Interchangeable LLM backends.

- OpenRouter: HTTP API with OPENROUTER_API_KEY (standard library only).
- AnthropicApi: the `anthropic` SDK with ANTHROPIC_API_KEY (pip install anthropic).
- ClaudeCli: the `claude -p` CLI (an already logged-in Claude Code account, no key).

Only the offer fields (title, company, location, ...) and a short profile summary are
sent. Email contents and links with personal identifiers are never sent.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class ClaudeCli:
    def __init__(self, model: str = "haiku", timeout: int = 300):
        self.model, self.timeout = model, timeout
        self.name = f"claude-cli:{model}"

    def complete(self, system: str, prompt: str) -> str:
        cmd = [
            "claude", "-p", "--model", self.model, "--tools", "",
            "--no-session-persistence", "--output-format", "json",
            "--system-prompt", system,
        ]
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=self.timeout,
                cwd=tempfile.gettempdir(),  # outside the project: no CLAUDE.md or local files
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LLMError(f"claude CLI non eseguibile: {exc}") from exc
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"risposta non JSON dalla CLI: {proc.stdout[:200]!r} {proc.stderr[:200]!r}") from exc
        if proc.returncode != 0 or data.get("is_error"):
            raise LLMError(f"claude CLI: {data.get('result') or proc.stderr[:200]}")
        return data["result"]


class AnthropicApi:
    def __init__(self, model: str = "claude-haiku-5-5"):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError("SDK mancante: pip install anthropic") from exc
        self.client = anthropic.Anthropic()
        self.model = model
        self.name = f"api:{model}"

    def complete(self, system: str, prompt: str) -> str:
        reply = self.client.messages.create(
            model=self.model, max_tokens=4096, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in reply.content if getattr(b, "type", "") == "text")


class OpenRouter:
    """OpenRouter API (OpenAI compatible). Key in OPENROUTER_API_KEY, standard library only."""

    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, model: str = "anthropic/claude-haiku-5.5", timeout: int = 120):
        self.key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self.key:
            raise LLMError("OPENROUTER_API_KEY non impostata (aggiungila al file .env)")
        self.model, self.timeout = model, timeout
        self.name = f"openrouter:{model}"

    def complete(self, system: str, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 4096,
        }).encode()
        req = urllib.request.Request(
            self.URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise LLMError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"OpenRouter non raggiungibile: {exc}") from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"risposta OpenRouter inattesa: {str(data)[:200]}") from exc


def make_llm(backend: str, cli_model: str, api_model: str, openrouter_model: str = "anthropic/claude-haiku-5.5"):
    if backend == "auto":
        if os.environ.get("OPENROUTER_API_KEY"):
            backend = "openrouter"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            backend = "api"
        else:
            backend = "cli"
    if backend == "openrouter":
        return OpenRouter(openrouter_model)
    if backend == "api":
        return AnthropicApi(api_model)
    if backend == "cli":
        return ClaudeCli(cli_model)
    raise LLMError(f"backend sconosciuto: {backend}")


def extract_json_array(text: str) -> list:
    """Extract the first JSON array from the answer (tolerates text or ``` around it)."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise LLMError(f"nessun array JSON nella risposta: {text[:200]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"JSON non valido: {exc}") from exc
    if not isinstance(data, list):
        raise LLMError("la risposta non e' un array")
    return data
