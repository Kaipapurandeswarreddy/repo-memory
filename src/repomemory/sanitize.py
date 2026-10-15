"""Secret sanitization (4 layers, port of repomemory-v2/src/a2e/sanitize.ts)
+ circuit breaker for failing hosts.

Layers:
 1. Known secrets value -> {{VAR}} (length-desc, >=4 chars)
 2. URL auth params -> {{PARAM}}
 3. JSON sensitive keys -> {{KEY}}
 4. Credential prefixes (Bearer, sk-, ghp_, eyJ...) -> {{REDACTED_*}}
"""

from __future__ import annotations

import re

SENSITIVE_PARAMS = frozenset({
    "apikey", "api_key", "key", "token", "access_token", "secret",
    "password", "appid", "app_key", "client_secret", "auth",
    "api-key", "x-api-key", "client_id", "app_id",
})

SENSITIVE_JSON_KEYS = frozenset({
    "authorization", "api_key", "apikey", "api-key", "x-api-key",
    "token", "access_token", "secret", "password", "client_secret",
    "auth_token", "bearer", "secret_key", "private_key", "signing_key",
})

CREDENTIAL_PREFIXES = (
    "Bearer ", "Basic ", "Token ",
    "sk-", "pk-", "rk-",
    "ghp_", "gho_", "ghs_",
    "xoxb-", "xoxp-",
    "sk-ant-",
    "eyJ",
)


def resolve_secrets(text: str, secrets: dict[str, str]) -> str:
    """Replace {{VAR}} placeholders with real values before execution."""
    if not text or not secrets:
        return text

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        return secrets.get(name, m.group(0))

    return re.sub(r"\{\{(\w+)\}\}", _sub, text)


def sanitize_secrets(text: str, secrets: dict[str, str] | None = None) -> str:
    """Redact credentials before persisting to memory."""
    result = text or ""
    secrets = secrets or {}
    # 1. known values, longest first
    entries = sorted(
        ((k, v) for k, v in secrets.items() if v and len(v) >= 4),
        key=lambda kv: len(kv[1]), reverse=True)
    for name, value in entries:
        result = result.replace(value, "{{" + name + "}}")
    # 2. URL params
    def _url(m: re.Match) -> str:
        url = m.group(0)
        q = url.find("?")
        if q == -1:
            return url
        base, qs = url[:q], url[q + 1:]
        changed = False
        parts = []
        for param in qs.split("&"):
            if "=" not in param:
                parts.append(param)
                continue
            k, v = param.split("=", 1)
            if k.lower() in SENSITIVE_PARAMS and not v.startswith("{{"):
                parts.append(f"{k}={{{{{k.upper()}}}}}")
                changed = True
            else:
                parts.append(param)
        return base + "?" + "&".join(parts) if changed else url
    result = re.sub(r"https?://[^\s\]\"\']+", _url, result)
    # 3. JSON keys
    def _json(m: re.Match) -> str:
        key, val = m.group(1), m.group(2)
        if val.startswith("{{") or val.startswith("/workflow/"):
            return m.group(0)
        if key.lower() in SENSITIVE_JSON_KEYS:
            safe = re.sub(r"[^A-Z0-9]", "_", key.upper())
            return f'"{key}":"{{{{{safe}}}}}"'
        return m.group(0)
    result = re.sub(r'"([^"]+)"\s*:\s*"([^"]{4,})"', _json, result)
    # 4. prefixes
    for prefix in CREDENTIAL_PREFIXES:
        esc = re.escape(prefix)
        result = re.sub(r"\"(" + esc + r"[^\"\\s]{8,})\"",
                        '"{{REDACTED_' + re.sub(r"[^A-Za-z]", "", prefix).upper() + '}}"',
                        result)
    return result


def check_circuit_breaker(store, agent_id: str = "", user_id: str = "",
                           host: str = "", threshold: int = 3) -> dict:
    """Count a2e-error/correction memories mentioning host; open if >= threshold."""
    if not host:
        return {"open": False, "count": 0}
    count = 0
    try:
        rels = store._iter_history_rels() if hasattr(store, "_iter_history_rels") else store.list_files()
        for rel in rels:
            hist = store.load_history(rel) if hasattr(store, "load_history") else []
            for n in hist:
                if not isinstance(n, dict):
                    continue
                tags = " ".join([str(n.get("summary", "")), str(n.get("why", "")),
                                 str(n.get("reverted_attempt", ""))]).lower()
                cats = str(n.get("category", "")) + str(n.get("kind", ""))
                if host.lower() in tags and ("error" in cats.lower() or "correct" in cats.lower() or "revert" in cats.lower()):
                    count += 1
    except Exception:
        count = 0
    open_ = count >= threshold
    return {"open": open_, "count": count,
            "message": f"Circuit breaker open — {host} has {count} recent errors" if open_ else "closed"}


__all__ = ["resolve_secrets", "sanitize_secrets", "check_circuit_breaker",
           "SENSITIVE_PARAMS", "SENSITIVE_JSON_KEYS", "CREDENTIAL_PREFIXES"]
