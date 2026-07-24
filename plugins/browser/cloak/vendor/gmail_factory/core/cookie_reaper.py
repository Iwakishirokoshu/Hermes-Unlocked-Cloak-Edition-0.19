"""
Cookie Reaper - Pre-trusted Google cookie injection for Selenium and Playwright
Loads and injects cookies to establish trusted sessions before signup.
"""
import os
import time
import json
import hmac
import base64
import logging

logger = logging.getLogger('gmail_creator_cookies')

DATA_DIR = "data"
COOKIE_FILE = os.path.join(DATA_DIR, "crypt_cookies.json")


def _resign_cookie(raw_cookie, ts):
    """Re-sign cookie to keep Google's MAC signature valid."""
    try:
        key = b'session_trust_key_2025'
        msg = f"{raw_cookie}|{ts}".encode()
        sig = base64.urlsafe_b64encode(
            hmac.new(key, msg, 'sha1').digest()
        ).decode().rstrip('=')
        return f"{raw_cookie}.{sig}"
    except Exception:
        return raw_cookie


def load_trust_cookies():
    """Load pre-trusted cookies from crypt_cookies.json."""
    try:
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
            if isinstance(cookies, list) and len(cookies) > 0:
                return cookies
    except Exception as e:
        logger.warning(f"Error loading trust cookies: {e}")
    return []


def inject_cookies_selenium(driver):
    """Inject pre-trusted Google cookies via Selenium WebDriver."""
    cookies = load_trust_cookies()
    if not cookies:
        logger.info("No trust cookies found — skipping injection")
        return False

    try:
        driver.get("https://www.google.com")
        time.sleep(2)

        ts = int(time.time())
        injected = 0

        for cookie in cookies:
            try:
                if '___PLACEHOLDER___' in str(cookie.get('value', '')):
                    continue

                if cookie.get('value') and '___' not in cookie.get('value', ''):
                    cookie['value'] = _resign_cookie(cookie['value'], ts)

                cookie['expiry'] = int(time.time()) + 86400 * 365

                if cookie.get('sameSite') == 'None':
                    del cookie['sameSite']
                cookie.pop('expires', None)

                driver.add_cookie(cookie)
                injected += 1
            except Exception:
                continue

        if injected > 0:
            logger.info(f"Cookie Reaper: Injected {injected} trust cookies (Selenium)")
            return True
    except Exception as e:
        logger.warning(f"Selenium cookie injection error: {e}")
    return False


async def inject_cookies_playwright(page):
    """Inject pre-trusted Google cookies via Playwright page."""
    cookies = load_trust_cookies()
    if not cookies:
        logger.info("No trust cookies found — skipping injection")
        return False

    try:
        ts = int(time.time())
        playwright_cookies = []

        for cookie in cookies:
            try:
                if '___PLACEHOLDER___' in str(cookie.get('value', '')):
                    continue

                value = cookie.get('value', '')
                if value and '___' not in value:
                    value = _resign_cookie(value, ts)

                pw_cookie = {
                    "name": cookie.get("name", ""),
                    "value": value,
                    "domain": cookie.get("domain", ".google.com"),
                    "path": cookie.get("path", "/"),
                    "expires": int(time.time()) + 86400 * 365,
                    "httpOnly": cookie.get("httpOnly", False),
                    "secure": cookie.get("secure", True),
                }
                same_site = cookie.get("sameSite", "Lax")
                if same_site in ("Strict", "Lax", "None"):
                    pw_cookie["sameSite"] = same_site

                playwright_cookies.append(pw_cookie)
            except Exception:
                continue

        if playwright_cookies:
            context = page.context
            await context.add_cookies(playwright_cookies)
            logger.info(f"Cookie Reaper: Injected {len(playwright_cookies)} trust cookies (Playwright)")
            return True
    except Exception as e:
        logger.warning(f"Playwright cookie injection error: {e}")
    return False
