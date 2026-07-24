"""Small, dependency-free secret redactor for Gmail Factory runtime logs."""

from __future__ import annotations

import re

try:
    # Hermes supplies the canonical implementation when this vendor is run
    # inside the agent. Keep the vendor runnable on its own as well.
    from agent.redact import redact_cdp_url as _hermes_redact_cdp_url
except ImportError:  # pragma: no cover - exercised by standalone installs
    _hermes_redact_cdp_url = None


_SENSITIVE_URL_PARAM_RE = re.compile(
    r"([?&;](?:access_token|refresh_token|id_token|token|api_key|apikey|"
    r"client_secret|password|auth|jwt|session|secret|key|code|signature)=)[^#&;\s]+",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)([^/\s?#@]+)@"
)
_PLAIN_PROXY_CREDENTIALS_RE = re.compile(
    r"(?<![\w./-])((?:\[[^\]]+\]|[A-Za-z0-9._-]+):\d{1,5}):([^:\s/@]+):([^:\s/@]+)"
)


def redact_runtime_value(value: object) -> str:
    """Mask endpoint and proxy credentials before they leave the runtime."""

    text = "" if value is None else str(value)
    if _hermes_redact_cdp_url is not None:
        text = _hermes_redact_cdp_url(text)

    text = _SENSITIVE_URL_PARAM_RE.sub(r"\1***", text)
    text = _URL_USERINFO_RE.sub(lambda match: f"{match.group(1)}***@", text)
    return _PLAIN_PROXY_CREDENTIALS_RE.sub(r"\1:***:***", text)


def redact_proxy_for_log(proxy: object) -> str:
    """Preserve an unauthenticated endpoint but hide proxy credentials."""

    text = "" if proxy is None else str(proxy).strip()
    if not text:
        return ""
    if "://" in text:
        return redact_runtime_value(text)

    parts = text.split(":")
    if len(parts) == 2 and all(parts):
        return text
    if len(parts) == 4 and all(parts):
        return f"{parts[0]}:{parts[1]}:***:***"
    return "<redacted-proxy>"
