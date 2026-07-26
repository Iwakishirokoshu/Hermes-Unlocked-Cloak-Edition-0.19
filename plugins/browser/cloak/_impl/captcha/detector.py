"""In-page captcha detector.

Runs a single ``page.evaluate(...)`` call in the active CloakBrowser tab
that classifies the page and (when possible) extracts ``site_key`` + any
extra fields the solver needs.

The JS is ported / consolidated from captcha-solver's ``detect_captcha``
but rewritten as one self-contained expression so we don't pay 18
``page.content()`` roundtrips.

Returns a dict::

    {
      "kind":      "turnstile" | "recaptcha_v2" | ... | None,
      "site_key":  "0x4..." | None,
      "page_url":  "https://...",
      "extra":     {"action": "...", "gt": "...", "captcha_url": "..."},
      "confidence": "high" | "medium" | "low",
    }

``kind == None`` means no captcha detected.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Pure JS — single expression returning a plain object. Designed to be
# fed straight into Playwright's ``page.evaluate(_DETECT_JS)``.
_DETECT_JS = r"""
(() => {
  const out = { kind: null, site_key: null, page_url: location.href, extra: {}, confidence: "high" };
  const $ = (sel) => document.querySelector(sel);
  // Documents with an opaque origin (about:blank, data:, sandboxed frames, a
  // page left in an error state) throw SecurityError on document.cookie. Read
  // it once behind a guard: a blocked cookie jar must not abort the whole scan
  // and turn "no captcha here" into a tool error.
  const cookies = (() => { try { return document.cookie || ""; } catch (e) { return ""; } })();

  // --- Cloudflare interstitial (full block, not just embedded Turnstile) ---
  const html = document.documentElement.outerHTML.toLowerCase();
  if (html.includes("checking your browser") && html.includes("cloudflare")) {
    out.kind = "cloudflare_interstitial"; return out;
  }
  if ($("#cf-challenge-running, [class*=cf-challenge-running]")) {
    out.kind = "cloudflare_interstitial"; return out;
  }

  // --- Cloudflare Turnstile ---
  const tDiv = $(".cf-turnstile, [data-sitekey][class*=turnstile], turnstile-wrapper");
  const tIframe = $('iframe[src*="challenges.cloudflare.com"]');
  if (tDiv || tIframe) {
    out.kind = "turnstile";
    out.site_key = (tDiv && tDiv.getAttribute("data-sitekey")) || null;
    const action = tDiv && tDiv.getAttribute("data-action");
    if (action) out.extra.action = action;
    const cdata = tDiv && tDiv.getAttribute("data-cdata");
    if (cdata) out.extra.data = cdata;
    return out;
  }

  // --- hCaptcha ---
  const hDiv = $(".h-captcha, [data-hcaptcha-sitekey]");
  const hIframe = $('iframe[src*="hcaptcha"]');
  if (hDiv || hIframe) {
    out.kind = "hcaptcha";
    out.site_key = (hDiv && (hDiv.getAttribute("data-sitekey") || hDiv.getAttribute("data-hcaptcha-sitekey"))) || null;
    return out;
  }

  // --- reCAPTCHA family ---
  const rDiv = $(".g-recaptcha");
  const rIframe = $('iframe[src*="recaptcha"]');
  const rScript = $('script[src*="recaptcha"]');
  if (rDiv || rIframe || rScript) {
    // Enterprise vs v3 vs v2 (priority order matters).
    const isEnterprise =
      !!$('script[src*="recaptcha/enterprise"]') ||
      (typeof window.___grecaptcha_cfg !== "undefined" &&
        Object.values(window.___grecaptcha_cfg.clients || {}).some(c => c && c.enterprise));
    const isV3 =
      !!$(".grecaptcha-badge") &&
      !$('iframe[src*="recaptcha"][src*="bframe"]');
    out.kind = isEnterprise ? "recaptcha_enterprise" : (isV3 ? "recaptcha_v3" : "recaptcha_v2");
    out.site_key = (rDiv && rDiv.getAttribute("data-sitekey")) || null;
    // Some sites stash the key in iframe ?k=...
    if (!out.site_key && rIframe) {
      try {
        const u = new URL(rIframe.src, location.href);
        out.site_key = u.searchParams.get("k");
      } catch (e) {}
    }
    return out;
  }

  // --- GeeTest ---
  if ($(".geetest_holder, .geetest_panel") || $('script[src*="geetest"]')) {
    out.kind = "geetest";
    out.confidence = "medium";
    // gt + challenge usually injected via initGeetest() — we can't read them
    // synchronously, caller must inspect network or call window.initGeetest.
    return out;
  }

  // --- FunCaptcha / Arkose ---
  const fDiv = $('#arkose, [data-arkose], .arkose-iframe-container, .funcaptcha, [data-pkey]');
  const fScript = $('script[src*="arkose"], script[src*="funcaptcha"], script[src*="arkoselabs"]');
  const fIframe = $('iframe[src*="arkose"], iframe[src*="funcaptcha"]');
  if (fDiv || fScript || fIframe) {
    const pkey = (fDiv && (fDiv.getAttribute("data-pkey") || fDiv.getAttribute("data-public-key"))) || null;
    // The enforcement script loads well before the challenge exists, and with a
    // convincing fingerprint the challenge may never render. Script-only, with
    // no iframe and no public key, means "announced, not rendered" — not a
    // solvable captcha. Calling it solvable sends the solver a null site_key.
    if (!fIframe && !pkey) {
      out.pending = true;
      out.confidence = "medium";
      out.extra.vendor = "arkose";
      out.extra.pending_reason = "Arkose script present, challenge not rendered yet";
      return out;
    }
    out.kind = "funcaptcha";
    out.site_key = pkey;
    if (fIframe) {
      try {
        const u = new URL(fIframe.src, location.href);
        const surl = u.origin + u.pathname.split("/").slice(0, -1).join("/");
        out.extra.surl = surl;
      } catch (e) {}
    }
    return out;
  }

  // --- Amazon WAF ---
  if ($('#captchacharacters, .captchacharacters') ||
      $('script[src*="captcha.js"], script[src*="aws-waf"]') ||
      html.includes("aws-waf")) {
    out.kind = "amazon_waf";
    out.confidence = "medium";
    return out;
  }

  // --- Friendly Captcha ---
  const frDiv = $('.frc-captcha, frc-captcha');
  if (frDiv || $('script[src*="friendly-challenge"], script[src*="friendlycaptcha"]')) {
    out.kind = "friendly_captcha";
    out.site_key = (frDiv && frDiv.getAttribute("data-sitekey")) || null;
    return out;
  }

  // --- KeyCaptcha ---
  if ($('script[src*="keycaptcha"]') || $('#kc_div, .keycaptcha')) {
    out.kind = "keycaptcha";
    out.confidence = "low";
    return out;
  }

  // --- DataDome ---
  if ($('script[src*="datadome"], script[src*="dd-js"]') ||
      $('iframe[src*="datadome"], iframe[src*="captcha.datadome"]') ||
      cookies.includes("datadome")) {
    out.kind = "datadome";
    const dIframe = $('iframe[src*="datadome"], iframe[src*="captcha.datadome"]');
    if (dIframe) out.extra.captcha_url = dIframe.src;
    out.extra.userAgent = navigator.userAgent;
    return out;
  }

  // --- Kasada ---
  if ($('script[src*="kasada"], script[src*="cdn.cas"], script[id*="kasada"]')) {
    out.kind = "kasada"; out.confidence = "medium"; return out;
  }

  // --- Akamai ---
  if ($('script[src*="akamai"], script[src*="akamai.bmp"], script[src*=".akamaized.net"]')) {
    out.kind = "akamai"; out.confidence = "low"; return out;
  }

  // --- Imperva / Incapsula ---
  if ($('script[src*="incapsula"], script[src*="imperva"]') ||
      cookies.includes("incap_ses") || cookies.includes("visid_incap")) {
    out.kind = "imperva"; out.confidence = "low"; return out;
  }

  // --- Yandex SmartCaptcha ---
  const yDiv = $('.smart-captcha, #js-captcha-si, [data-sitekey][class*=smart-captcha]');
  if (yDiv || $('script[src*="smartcaptcha"]')) {
    out.kind = "yandex";
    out.site_key = (yDiv && yDiv.getAttribute("data-sitekey")) || null;
    return out;
  }

  // --- Tencent ---
  const tcDiv = $('#tcaptcha_transform_dy, .tcaptcha, [data-appid]');
  if (tcDiv || $('script[src*="tencent.com/captcha"]')) {
    out.kind = "tencent";
    if (tcDiv) out.extra.app_id = tcDiv.getAttribute("data-appid") || "";
    return out;
  }

  // --- Lemin ---
  const lDiv = $('[data-captcha-id], .lemin-captcha, #lemin-captcha');
  if (lDiv || $('script[src*="leminnow"]')) {
    out.kind = "lemin";
    if (lDiv) {
      out.extra.captcha_id = lDiv.getAttribute("data-captcha-id") || "";
      out.extra.div_id = lDiv.id || "";
    }
    return out;
  }

  // --- MTCaptcha ---
  const mDiv = $('.mtcaptcha, [data-sitekey][class*=mtcaptcha]');
  if (mDiv || $('script[src*="mtcaptcha"]')) {
    out.kind = "mtcaptcha";
    out.site_key = (mDiv && mDiv.getAttribute("data-sitekey")) || null;
    return out;
  }

  // --- Generic image captcha (last resort) ---
  const img = $('img[src*="captcha"], img[alt*="captcha" i]');
  if (img) {
    out.kind = "image";
    out.extra.image_src = img.src;
    out.confidence = "low";
    return out;
  }

  // --- Challenge announced but not yet rendered ---
  // A verification step often paints "checking your browser" / "captcha
  // loading" seconds before the widget exists — and with a good fingerprint it
  // may resolve on its own and never render one at all. Reporting a plain
  // "no captcha" here is what made the two outcomes indistinguishable, so say
  // "pending" and let the caller wait it out.
  const vendorScript = $('script[src*="arkoselabs"], script[src*="funcaptcha"], script[src*="hcaptcha"], script[src*="recaptcha"], script[src*="challenges.cloudflare.com"]');
  const emptyHolder = $('#arkose, [data-arkose], .arkose-iframe-container, [data-pkey], .funcaptcha, .h-captcha, .g-recaptcha, .cf-turnstile');
  const text = (document.body && document.body.innerText || "").toLowerCase().slice(0, 4000);
  const phrases = ["captcha loading", "loading captcha", "checking your browser",
                   "verifying you are human", "verify you are human", "just a moment",
                   "please wait while we verify", "browser verification"];
  const phrase = phrases.find(p => text.includes(p)) || null;
  if (phrase || vendorScript || emptyHolder) {
    out.pending = true;
    out.confidence = "medium";
    out.extra.pending_reason = phrase
      ? ("page says: " + phrase)
      : (emptyHolder ? "challenge container present but empty" : "challenge script loaded, widget not rendered");
    return out;
  }

  return out; // kind: null
})();
"""


def _empty_result() -> dict:
    return {"kind": None, "site_key": None, "page_url": "", "extra": {}, "confidence": "high"}


def _normalised(result: Any) -> Optional[dict]:
    if not isinstance(result, dict):
        return None
    result.setdefault("kind", None)
    result.setdefault("site_key", None)
    result.setdefault("page_url", "")
    if not isinstance(result.get("extra"), dict):
        result["extra"] = {}
    result.setdefault("confidence", "high")
    result["pending"] = bool(result.get("pending"))
    return result


async def _scan_once(page: Any) -> dict:
    """One frame-aware pass. See :func:`detect_in_playwright_page`."""
    """Run the detector in a Playwright page; returns the dict described above.

    Every frame is scanned, not just the main document. Arkose/FunCaptcha,
    hCaptcha and reCAPTCHA all render inside an iframe, and the top document
    cannot even read a cross-origin one from JS — so a main-frame-only scan
    reported "no captcha" on exactly the pages that had one. Playwright can
    evaluate inside those frames, so ask each of them.

    ``page`` may be either an async Playwright Page or anything with a
    compatible ``evaluate`` coroutine.
    """
    frames = list(getattr(page, "frames", None) or [])
    if not frames:
        return _normalised(await page.evaluate(_DETECT_JS)) or _empty_result()

    # frames[0] is the main frame; scanning in order reports a top-level
    # captcha as top-level rather than attributing it to a subframe.
    main_url = str(getattr(frames[0], "url", "") or "")
    fallback: Optional[dict] = None
    for index, frame in enumerate(frames):
        try:
            result = _normalised(await frame.evaluate(_DETECT_JS))
        except Exception as exc:  # noqa: BLE001
            # A frame can detach mid-scan, or refuse evaluation while it
            # navigates. One bad frame must not blind the whole scan.
            logger.debug("captcha scan skipped a frame: %s", exc)
            continue
        if result is None:
            continue
        if result.get("kind"):
            if index:
                # Solvers key on the page the operator is on, not on the
                # challenge iframe's own URL.
                result["extra"]["frame_url"] = result.get("page_url") or ""
                result["extra"]["in_iframe"] = True
                result["page_url"] = main_url or result.get("page_url") or ""
            return result
        if fallback is None:
            fallback = result
    return fallback or _empty_result()


async def detect_in_playwright_page(page: Any, wait_ms: int = 0) -> dict:
    """Classify any captcha on the page, optionally waiting for it to settle.

    A verification step commonly announces itself ("captcha loading", "checking
    your browser") seconds before the widget appears — and with a convincing
    fingerprint it may clear on its own and never show one. A single snapshot
    cannot tell "no captcha" from "not yet", which leaves the caller guessing at
    exactly the moment it matters.

    With ``wait_ms`` the scan keeps polling until it finds a real captcha, or
    the page reports clean twice in a row, or the budget runs out. The result
    carries ``pending`` and ``waited_ms`` so a timeout is visible as a timeout
    rather than as an all-clear.
    """
    deadline_ms = max(0, int(wait_ms or 0))
    result = await _scan_once(page)
    if result.get("kind") or deadline_ms <= 0:
        result.setdefault("waited_ms", 0)
        return result

    step = 0.5
    waited = 0.0
    clean_streak = 1 if not result.get("pending") else 0
    while waited * 1000 < deadline_ms:
        await asyncio.sleep(step)
        waited += step
        result = await _scan_once(page)
        if result.get("kind"):
            break
        # Two consecutive clean reads: the challenge resolved itself, or there
        # never was one. One is not enough — the widget may be mid-swap.
        clean_streak = 0 if result.get("pending") else clean_streak + 1
        if clean_streak >= 2:
            break
    result["waited_ms"] = int(waited * 1000)
    return result


def detector_js() -> str:
    """Expose the JS snippet for callers that have raw CDP only."""
    return _DETECT_JS
