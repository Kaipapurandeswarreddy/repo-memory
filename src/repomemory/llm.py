"""C8 LLM Gateway — stub by default, OpenAI via stdlib urllib only.

Usage:
    gw = LLMGateway()                 # stub, zero keys, zero network
    gw = LLMGateway(mode="openai")    # needs OPENAI_API_KEY, else falls back to stub
    out = gw.generate("task...")
    # -> {"text":..., "tokens_in": int, "tokens_out": int, "latency_ms": int}

Token logging: every call appends JSONL to <log_path> (default
.repomemory/metrics/tokens.jsonl relative to cwd when it exists, else
skipped) and emits a logging INFO line. Counters total_in/total_out
are always kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("repomemory.llm")


def _count_tokens(text: str) -> int:
    # Cheap deterministic proxy: ~4 chars/token (documented as estimate).
    return max(1, len(text or "") // 4)


class LLMGateway:
    def __init__(self, mode: str = "stub", model: str | None = None,
                 log_path: str | os.PathLike | None = None) -> None:
        if mode not in ("stub", "openai", "gemini", "groq", "ollama"):
            raise ValueError("mode must be 'stub', 'openai', 'gemini', 'groq' or 'ollama'")
        self.mode = mode
        defaults = {"stub": "stub", "openai": "gpt-4o-mini",
                    "gemini": "gemini-3.5-flash",
                    "groq": "openai/gpt-oss-20b", "ollama": "gemma4:31b"}
        env_key = {"openai": "OPENAI_MODEL", "gemini": "GEMINI_MODEL",
                   "groq": "GROQ_MODEL", "ollama": "OLLAMA_MODEL"}.get(mode)
        env_model = os.environ.get(env_key, "") if env_key else ""
        self.model = model or env_model or defaults[mode]
        self.log_path = Path(log_path) if log_path else self._default_log_path()
        self.total_in = 0
        self.total_out = 0
        self.calls = 0

    # -- public ----------------------------------------------------------
    def generate(self, prompt: str) -> dict:
        t0 = time.perf_counter()
        if self.mode == "openai" and os.environ.get("OPENAI_API_KEY"):
            try:
                text = self._openai_complete(prompt)
            except Exception as e:  # network/API failure -> deterministic fallback
                log.warning("openai call failed (%s); falling back to stub", e)
                text = self._stub_text(prompt)
        elif self.mode == "gemini" and (os.environ.get("GEMINI_API_KEY")
                                        or os.environ.get("GOOGLE_API_KEY")):
            try:
                text = self._gemini_complete(prompt)
            except Exception as e:
                log.warning("gemini call failed (%s); falling back to stub", type(e).__name__)
                text = self._stub_text(prompt)
        elif self.mode == "groq" and os.environ.get("GROQ_API_KEY"):
            try:
                text = self._openai_compat_complete(
                    "https://api.groq.com/openai/v1/chat/completions",
                    os.environ["GROQ_API_KEY"], prompt)
            except Exception as e:
                log.warning("groq call failed (%s); falling back to stub", type(e).__name__)
                text = self._stub_text(prompt)
        elif self.mode == "ollama" and os.environ.get("OLLAMA_API_KEY"):
            try:
                text = self._openai_compat_complete(
                    "https://ollama.com/v1/chat/completions",
                    os.environ["OLLAMA_API_KEY"], prompt)
            except Exception as e:
                log.warning("ollama call failed (%s); falling back to stub", type(e).__name__)
                text = self._stub_text(prompt)
        else:
            if self.mode in ("openai", "gemini", "groq", "ollama"):
                log.warning("%s key not set; using stub",
                            {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
                             "groq": "GROQ_API_KEY",
                             "ollama": "OLLAMA_API_KEY"}[self.mode])
            text = self._stub_text(prompt)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        # stub is instant; still report >=1ms for stable schema
        latency_ms = max(1, latency_ms)
        tokens_in = _count_tokens(prompt)
        tokens_out = _count_tokens(text)
        self.total_in += tokens_in
        self.total_out += tokens_out
        self.calls += 1
        self._log_tokens(tokens_in, tokens_out, latency_ms)
        return {"text": text, "tokens_in": tokens_in,
                "tokens_out": tokens_out, "latency_ms": latency_ms}

    # -- stub -------------------------------------------------------------
    @staticmethod
    def _stub_text(prompt: str) -> str:
        h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        first = (prompt.strip().splitlines() or [""])[0][:120]
        return (
            f"# RepoMemory stub patch (deterministic, no network) [{h}]\n"
            f"# task: {first}\n"
            f"# NOTE: stub mode emits a template only — wire a real model for content.\n"
            f'"""apply manually"""\n'
            f"# prompt_chars={len(prompt)}\n"
        )

    # -- openai (urllib, no deps) ------------------------------------------
    def _openai_complete(self, prompt: str) -> str:
        import urllib.request

        key = os.environ["OPENAI_API_KEY"]
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]

    # -- openai-compatible (groq, ollama cloud, any /v1/chat/completions) ----
    def _openai_compat_complete(self, url: str, key: str, prompt: str) -> str:
        import time as _time
        import urllib.error
        import urllib.request

        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }).encode("utf-8")
        last: Exception | None = None
        for attempt in range(3):  # retry 429/503 demand spikes
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
                    payload = json.loads(resp.read().decode("utf-8"))
                return payload["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (429, 503) or attempt == 2:
                    raise
                _time.sleep(2 ** attempt * 5)
            except Exception as e:  # noqa: BLE001
                last = e
                break
        assert last is not None
        raise last

    # -- gemini (urllib, no deps) -------------------------------------------
    def _gemini_complete(self, prompt: str) -> str:
        import time as _time
        import urllib.error
        import urllib.request

        key = os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
        model = self.model or "gemini-3.5-flash"
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024},
        }).encode("utf-8")
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        last: Exception | None = None
        for attempt in range(3):  # retry 429/503 demand spikes
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
                    payload = json.loads(resp.read().decode("utf-8"))
                cands = payload.get("candidates", [])
                parts = (cands[0].get("content", {}).get("parts", []) if cands else [])
                texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
                if not texts:
                    raise RuntimeError(f"empty gemini response: {str(payload)[:200]}")
                return "".join(texts)
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (429, 503) or attempt == 2:
                    raise
                _time.sleep(2 ** attempt * 5)  # 5s, 10s backoff
            except Exception as e:  # noqa: BLE001
                last = e
                break
        assert last is not None
        raise last

    # -- token accounting ---------------------------------------------------
    @staticmethod
    def _default_log_path() -> Path | None:
        cand = Path.cwd() / ".repomemory" / "metrics" / "tokens.jsonl"
        try:
            if cand.parent.parent.exists():  # only log inside an initialised store
                return cand
        except OSError:
            pass
        return None

    def _log_tokens(self, ti: int, to: int, latency_ms: int) -> None:
        log.info("llm mode=%s in=%d out=%d latency_ms=%d", self.mode, ti, to, latency_ms)
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.time(), "mode": self.mode, "model": self.model,
                   "tokens_in": ti, "tokens_out": to, "latency_ms": latency_ms,
                   "total_in": self.total_in, "total_out": self.total_out}
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass
