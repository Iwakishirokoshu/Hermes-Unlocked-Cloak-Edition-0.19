"""Anti-Captcha client (https://anti-captcha.com/apidoc).

The third solver alongside CapSolver and 2captcha. Same shape as the other
two — a pure HTTP client, no Playwright. The site_key / gt / challenge must
already be extracted by the caller; :mod:`detector` does that.

    Kind                  Anti-Captcha task type              Notes / extra
    ----                  ----------------------              -------------
    recaptcha_v2          RecaptchaV2TaskProxyless            websiteKey, isInvisible
    recaptcha_v3          RecaptchaV3TaskProxyless            minScore, pageAction
    recaptcha_enterprise  RecaptchaV2EnterpriseTaskProxyless  enterprisePayload
    hcaptcha              HCaptchaTaskProxyless               websiteKey, isInvisible
    turnstile             TurnstileTaskProxyless              action, cData
    funcaptcha            FunCaptchaTaskProxyless             websitePublicKey, surl
    geetest               GeeTestTaskProxyless                gt, challenge (v3)
    geetest_v4            GeeTestTaskProxyless                captcha_id (v4)
    image                 ImageToTextTask                     base64 body

Anti-Captcha reports failures in-band: ``errorId != 0`` with an
``errorCode``/``errorDescription``. Those become :class:`AntiCaptchaError`, and
the router treats that as a soft failure and moves to the next provider.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

ANTICAPTCHA_BASE = "https://api.anti-captcha.com"


class AntiCaptchaError(RuntimeError):
    pass


# Kind -> Anti-Captcha task type. Kinds absent here are unsupported; the router
# will fall through to another provider or raise MANUAL_INTERVENTION_REQUIRED.
_TASK_TYPES = {
    "recaptcha_v2": "RecaptchaV2TaskProxyless",
    "recaptcha_v3": "RecaptchaV3TaskProxyless",
    "recaptcha_enterprise": "RecaptchaV2EnterpriseTaskProxyless",
    "hcaptcha": "HCaptchaTaskProxyless",
    "turnstile": "TurnstileTaskProxyless",
    "funcaptcha": "FunCaptchaTaskProxyless",
    "geetest": "GeeTestTaskProxyless",
    "geetest_v4": "GeeTestTaskProxyless",
    "image": "ImageToTextTask",
}

SUPPORTED_KINDS = sorted(_TASK_TYPES.keys())


def _build_task(kind: str, site_key: str, url: str, extra: dict) -> dict:
    """Build the ``task`` payload for createTask. Raises if unsupported."""
    task_type = _TASK_TYPES.get(kind)
    if not task_type:
        raise AntiCaptchaError(
            f"Anti-Captcha does not support kind={kind!r}. "
            f"Supported: {', '.join(SUPPORTED_KINDS)}"
        )

    task: dict[str, Any] = {"type": task_type, "websiteURL": url}

    if kind == "image":
        body = extra.get("body") or extra.get("base64") or ""
        if not body:
            raise AntiCaptchaError("image captcha needs extra['body'] (base64)")
        task = {"type": task_type, "body": body}
        if extra.get("instructions"):
            task["comment"] = extra["instructions"]
        if extra.get("case_sensitive"):
            task["case"] = True
        return task

    if kind == "funcaptcha":
        if not site_key:
            raise AntiCaptchaError("funcaptcha needs the Arkose public key")
        task["websitePublicKey"] = site_key
        surl = extra.get("surl") or extra.get("api_js_subdomain")
        if surl:
            # Anti-Captcha wants the bare subdomain, not the full URL.
            task["funcaptchaApiJSSubdomain"] = _hostname(surl)
        if extra.get("data"):
            task["data"] = extra["data"]
        return task

    if kind in ("geetest", "geetest_v4"):
        api_server = extra.get("api_server") or extra.get("geetest_api_server")
        if api_server:
            task["geetestApiServerSubdomain"] = _hostname(api_server)
        if kind == "geetest_v4":
            task["version"] = 4
            captcha_id = extra.get("captcha_id") or site_key
            if not captcha_id:
                raise AntiCaptchaError("geetest_v4 needs extra['captcha_id']")
            task["gt"] = captcha_id
            return task
        gt = extra.get("gt") or site_key
        challenge = extra.get("challenge")
        if not gt or not challenge:
            raise AntiCaptchaError("geetest needs extra['gt'] and extra['challenge']")
        task["version"] = 3
        task["gt"] = gt
        task["challenge"] = challenge
        return task

    # The reCAPTCHA / hCaptcha / Turnstile family all key on websiteKey.
    if not site_key:
        raise AntiCaptchaError(f"{kind} needs a site key")
    task["websiteKey"] = site_key

    if kind == "recaptcha_v3":
        task["minScore"] = float(extra.get("min_score", 0.7) or 0.7)
        task["pageAction"] = extra.get("action", "verify")
    elif kind == "recaptcha_enterprise":
        payload = extra.get("enterprise_payload") or extra.get("enterprisePayload")
        if payload:
            task["enterprisePayload"] = payload
        if extra.get("api_domain"):
            task["apiDomain"] = extra["api_domain"]
    elif kind == "turnstile":
        if extra.get("action"):
            task["action"] = extra["action"]
        if extra.get("data"):
            task["cData"] = extra["data"]
    elif kind in ("recaptcha_v2", "hcaptcha"):
        if extra.get("invisible"):
            task["isInvisible"] = True

    return task


def _hostname(value: str) -> str:
    """``https://client-api.arkoselabs.com/x`` -> ``client-api.arkoselabs.com``."""
    text = str(value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


def _extract_token(kind: str, solution: dict) -> str:
    """Pull the right token field out of Anti-Captcha's solution dict."""
    if not isinstance(solution, dict):
        return str(solution)
    for key in ("gRecaptchaResponse", "token", "text", "challenge", "answer"):
        if solution.get(key):
            return str(solution[key])
    return str(solution)


class AntiCaptchaClient:
    """Async Anti-Captcha client. Reuses a single httpx.AsyncClient per instance."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 180.0,
        poll_interval: float = 3.0,
        first_poll_delay: float = 5.0,
    ):
        self.api_key = api_key or os.environ.get("ANTICAPTCHA_API_KEY") or os.environ.get(
            "ANTI_CAPTCHA_API_KEY"
        )
        if not self.api_key:
            raise AntiCaptchaError("ANTICAPTCHA_API_KEY not set")
        self._client = httpx.AsyncClient(timeout=30.0)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.first_poll_delay = first_poll_delay

    async def __aenter__(self) -> "AntiCaptchaClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def name(self) -> str:
        return "anticaptcha"

    async def solve(
        self,
        kind: str,
        site_key: str = "",
        url: str = "",
        *,
        extra: Optional[dict] = None,
    ) -> str:
        extra = dict(extra or {})
        task = _build_task(kind, site_key, url, extra)
        task_id = await self._create_task(task)
        solution = await self._poll(task_id)
        return _extract_token(kind, solution)

    # ------- internals ------- #

    async def _create_task(self, task: dict) -> int:
        payload = {"clientKey": self.api_key, "task": task}
        try:
            resp = await self._client.post(f"{ANTICAPTCHA_BASE}/createTask", json=payload)
        except httpx.HTTPError as exc:
            raise AntiCaptchaError(f"createTask request failed: {exc}") from exc
        data = _json(resp)
        _raise_for_error(data, "createTask")
        task_id = data.get("taskId")
        if not task_id:
            raise AntiCaptchaError(f"createTask returned no taskId: {data}")
        return int(task_id)

    async def _poll(self, task_id: int) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        await asyncio.sleep(self.first_poll_delay)
        while True:
            if loop.time() > deadline:
                raise AntiCaptchaError(f"timed out after {self.timeout}s waiting for task {task_id}")
            payload = {"clientKey": self.api_key, "taskId": task_id}
            try:
                resp = await self._client.post(
                    f"{ANTICAPTCHA_BASE}/getTaskResult", json=payload
                )
            except httpx.HTTPError as exc:
                raise AntiCaptchaError(f"getTaskResult request failed: {exc}") from exc
            data = _json(resp)
            _raise_for_error(data, "getTaskResult")
            status = str(data.get("status", "")).lower()
            if status == "ready":
                solution = data.get("solution")
                if not isinstance(solution, dict):
                    raise AntiCaptchaError(f"task {task_id} ready without a solution: {data}")
                return solution
            # "processing" — keep waiting.
            await asyncio.sleep(self.poll_interval)


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError as exc:
        raise AntiCaptchaError(
            f"non-JSON reply (HTTP {resp.status_code}): {resp.text[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise AntiCaptchaError(f"unexpected reply shape: {data!r}")
    return data


def _raise_for_error(data: dict, where: str) -> None:
    """Anti-Captcha reports failures in-band with errorId != 0."""
    if not data.get("errorId"):
        return
    code = data.get("errorCode") or "UNKNOWN"
    description = data.get("errorDescription") or ""
    raise AntiCaptchaError(f"{where} failed [{code}]: {description}")
