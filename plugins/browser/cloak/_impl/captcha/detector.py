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
  const fIframe = $('iframe[src*="arkose"], iframe[src*="funcaptcha"], iframe[title*="arkose" i], iframe[name*="arkose" i], iframe[id*="arkose" i], iframe[title*="funcaptcha" i]');
  if (fDiv || fScript || fIframe) {
    // The public key is what a solver actually needs, and sites rarely put it
    // in a data- attribute. Arkose carries it in the enforcement URL instead:
    //   https://client-api.arkoselabs.com/v2/<PUBLIC_KEY>/api.js
    //   .../fc/api/nojs/?pkey=<PUBLIC_KEY>
    // Reading only data-pkey meant the challenge was found but never solvable.
    const findPkey = () => {
      const attr = fDiv && (fDiv.getAttribute("data-pkey") || fDiv.getAttribute("data-public-key"));
      if (attr) return attr;
      const urls = [];
      const push = (el) => { const v = el && el.getAttribute("src"); if (v) urls.push(v); };
      Array.prototype.forEach.call(
        document.querySelectorAll('script[src*="arkose" i], script[src*="funcaptcha" i], iframe[src*="arkose" i], iframe[src*="funcaptcha" i]'),
        push);
      for (const u of urls) {
        const v2 = u.match(/\/v2\/([A-Za-z0-9-]{8,})\//);
        if (v2) return v2[1];
        const qs = u.match(/[?&](?:public_key|pkey)=([A-Za-z0-9-]{8,})/i);
        if (qs) return qs[1];
      }
      // Some embeds stash it on window for their own callback wiring.
      try {
        const w = window.arkoseConfig || window.funCaptchaConfig || null;
        if (w && (w.public_key || w.publicKey)) return w.public_key || w.publicKey;
      } catch (e) {}
      return null;
    };
    const pkey = findPkey();
    const frameSrc = (fIframe && fIframe.getAttribute("src")) || "";
    const hasVendorSrc = /arkose|funcaptcha/i.test(frameSrc);
    // A solver needs the public key. Without one — enforcement script only, or
    // an Arkose frame that exists but has not been populated yet, which is what
    // "Captcha is loading..." looks like in the DOM — the challenge is announced
    // rather than solvable. Reporting it as solvable hands the solver a null
    // site_key; reporting it as absent tells the agent to walk into a wall.
    if (!pkey && !hasVendorSrc) {
      out.pending = true;
      out.confidence = "medium";
      out.extra.vendor = "arkose";
      out.extra.pending_reason = fIframe
        ? "Arkose frame present but the public key is not exposed yet"
        : "Arkose script present, challenge not rendered yet";
      return out;
    }
    out.kind = "funcaptcha";
    out.site_key = pkey;
    if (hasVendorSrc) {
      try {
        const u = new URL(frameSrc, location.href);
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
  const headings = Array.prototype.map.call(
    document.querySelectorAll("h1, h2, [role=heading]"), h => h.textContent || "").join(" ");
  const text = ((document.body && document.body.innerText || "") + " " + headings)
    .toLowerCase().slice(0, 4000);
  const patterns = [
    /captcha\s+(?:is\s+)?loading/,          // "Captcha is loading..."
    /loading\s+(?:the\s+)?captcha/,
    /checking\s+your\s+browser/,
    /verif(?:ying|y)\s+(?:that\s+)?you\s+are\s+human/,
    /just\s+a\s+moment/,
    /please\s+wait\s+while\s+we\s+verify/,
    /browser\s+verification/,
    /one\s+more\s+step/,
  ];
  const hit = patterns.map(re => text.match(re)).find(Boolean) || null;
  const phrase = hit ? hit[0] : null;
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


def _pages_to_scan(page: Any) -> list:
    """Every tab of the profile, starting with the one we were handed.

    The pooled client is pinned to the context's first tab, so a flow that
    opens the challenge in a second tab left the detector reading the page the
    operator had already left — answering "no captcha" while the screenshot
    plainly showed one, which is worse than not answering at all.
    """
    context = getattr(page, "context", None)
    pages = list(getattr(context, "pages", None) or [])
    if not pages:
        return [page]
    # Keep the handed-in page first, so a captcha there reports as tab 0.
    return [page] + [other for other in pages if other is not page]


async def _scan_once(page: Any) -> dict:
    """One pass over every tab of the profile, and every frame of each tab."""
    fallback: Optional[dict] = None
    for tab_index, tab in enumerate(_pages_to_scan(page)):
        try:
            result = await _scan_page(tab)
        except Exception as exc:  # noqa: BLE001
            # A tab can close mid-scan; that must not blind the rest.
            logger.debug("captcha scan skipped a tab: %s", exc)
            continue
        if result.get("kind") or result.get("pending"):
            if tab_index:
                result["extra"]["tab_index"] = tab_index
                result["extra"]["other_tab"] = True
            return result
        if fallback is None:
            fallback = result
    return fallback or _empty_result()


async def _scan_page(page: Any) -> dict:
    """One frame-aware pass over a single tab.

    Every frame is scanned, not just the main document. Arkose/FunCaptcha,
    hCaptcha and reCAPTCHA all render inside an iframe, and the top document
    cannot even read a cross-origin one from JS — so a main-frame-only scan
    reported "no captcha" on exactly the pages that had one. Playwright can
    evaluate inside those frames, so ask each of them.
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
        # "Still loading" counts as a hit too. Returning only on a resolved
        # kind meant a pending banner in a subframe lost to the main frame's
        # clean read — which is exactly the Figma signup, where the banner and
        # the Arkose frame live inside a login iframe.
        if result.get("kind") or result.get("pending"):
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
