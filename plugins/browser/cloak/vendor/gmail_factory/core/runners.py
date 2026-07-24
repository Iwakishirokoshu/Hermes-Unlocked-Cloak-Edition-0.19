"""
Runners - Orchestrates account creation flows (Playwright, Appium)
Integrates with phone_bypass, retry_engine, proxy_manager, and account_manager.
"""
import asyncio
import random
import logging
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Config
from core.phone_bypass import handle_verification
from core.redaction import redact_runtime_value as _redact_runtime_value
from core.retry_engine import retry_engine, CreationError

try:
    from core.stealth_browser import PlaywrightStealthManager
except ImportError:
    PlaywrightStealthManager = None

AppiumManager = None  # Appium engine dropped from Hermes vendor build.

logger = logging.getLogger('gmail_creator_runners')


def _update_progress(progress, task, **kwargs):
    if progress is not None and task is not None:
        progress.update(task, **kwargs)


def run_appium_flow(i, num_accounts, username, first_name, last_name, password,
                    month, day, year, gender, progress, account_task):
    if AppiumManager is None:
        logger.error("Appium is not installed. Install with: pip install Appium-Python-Client")
        return False
    if progress and account_task is not None:
        _update_progress(progress, account_task, completed=10,
                        description=f"[blue]Starting Appium (Android) flow...[/]")
    manager = AppiumManager()
    if not manager.initialize():
        return False

    try:
        _update_progress(progress, account_task, completed=30, description="Navigating to Android Add Account...")
        if not manager.navigate_to_add_account():
            return False

        _update_progress(progress, account_task, completed=50, description="Starting Google Creation Flow...")
        if not manager.start_creation_flow():
            return False

        _update_progress(progress, account_task, completed=65, description="Entering Name...")
        if not manager.fill_name(first_name, last_name):
            return False

        _update_progress(progress, account_task, completed=75, description="Entering Birthday/Gender...")
        if not manager.fill_birthday_gender(month, day, year, gender):
            return False

        _update_progress(progress, account_task, completed=85, description="Checking for Phone Verification Bypass...")
        if not manager.bypass_phone_challenge():
            return False

        _update_progress(progress, account_task, completed=100, description="Account Created (Appium)!")
        return True
    except Exception as e:
        logger.error(f"Appium flow failed: {_redact_runtime_value(e)}")
        return False
    finally:
        manager.close()


def _parse_birthday(birthday_str):
    try:
        parts = birthday_str.strip().split()
        if len(parts) < 3:
            return "1", "1", "1990"
        month = str(int(parts[0]))
        day = str(int(parts[1]))
        year = str(int(parts[2]))
        if not (1 <= int(month) <= 12):
            month = "1"
        if not (1 <= int(day) <= 31):
            day = "1"
        if not (1900 <= int(year) <= 2010):
            year = "1990"
        return month, day, year
    except Exception:
        return "1", "1", "1990"


async def _try_click(page, selectors, is_mobile=False, timeout=5000):
    # Fast pass: check all selectors immediately without waiting
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                if is_mobile:
                    await el.tap()
                else:
                    await el.click()
                return True
        except Exception:
            continue
    # Slow pass: wait for first available selector
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout)
            if el and await el.is_visible():
                if is_mobile:
                    await el.tap()
                else:
                    await el.click()
                return True
        except Exception:
            continue
    return False


async def _wait_for_page_change(page, old_url, timeout_s=10):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            if page.url != old_url:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Google Trust-Building Pre-Warm
# ──────────────────────────────────────────────────────────────────────────────
async def _google_prewarm(page):
    logger.info("[PREWARM] Starting Google trust-building session...")

    async def _safe_goto(url, timeout=20000):
        try:
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            await page.wait_for_timeout(random.randint(2000, 5000))
            return True
        except Exception as e:
            logger.debug(f"[PREWARM] Navigation failed for {_redact_runtime_value(url)}: {_redact_runtime_value(e)}")
            return False

    async def _accept_cookies():
        for sel in [
            "button:has-text('Accept all')", "button:has-text('I agree')",
            "button:has-text('Reject all')", "button:has-text('Accept')",
            "#L2AGLb", ".tHlp8d",
        ]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await page.wait_for_timeout(800)
                    return
            except Exception:
                pass

    async def _human_scroll(min_px=300, max_px=900):
        try:
            dist = random.randint(min_px, max_px)
            steps = random.randint(3, 8)
            for _ in range(steps):
                await page.mouse.wheel(0, dist // steps)
                await page.wait_for_timeout(random.randint(100, 300))
            await page.wait_for_timeout(random.randint(400, 900))
            back = random.randint(50, 200)
            await page.mouse.wheel(0, -back)
            await page.wait_for_timeout(random.randint(300, 700))
        except Exception:
            pass

    async def _random_mouse_movement():
        try:
            x = random.randint(100, 800)
            y = random.randint(100, 500)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await page.wait_for_timeout(random.randint(200, 600))
        except Exception:
            pass

    try:
        # 1. Google Home — establish cookies
        if await _safe_goto("https://www.google.com"):
            await _accept_cookies()
            await _random_mouse_movement()
            await _human_scroll(200, 400)

            # 2. Real search — builds activity signal
            searches = [
                "best free email service 2025", "how to create gmail account",
                "google workspace features", "gmail tips and tricks",
                "weather today", "latest news today",
            ]
            try:
                search_box = await page.query_selector("textarea[name='q'], input[name='q']")
                if search_box:
                    await search_box.click()
                    await page.wait_for_timeout(random.randint(400, 800))
                    query = random.choice(searches)
                    for ch in query:
                        await page.keyboard.type(ch)
                        await page.wait_for_timeout(random.randint(50, 120))
                    await page.wait_for_timeout(random.randint(500, 1000))
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(random.randint(3000, 6000))
                    await _human_scroll()
                    await _random_mouse_movement()

                    # Click a search result
                    try:
                        results = await page.query_selector_all("a h3")
                        if results and len(results) > 1:
                            target = random.choice(results[:5])
                            await target.click(timeout=3000)
                            await page.wait_for_timeout(random.randint(3000, 7000))
                            await _human_scroll(150, 500)
                    except Exception:
                        pass
            except Exception:
                pass

        # 3. YouTube — strong Google ecosystem signal
        if await _safe_goto("https://www.youtube.com"):
            await _accept_cookies()
            await _random_mouse_movement()
            await _human_scroll(200, 600)
            await page.wait_for_timeout(random.randint(3000, 5000))

            # Click a video thumbnail
            try:
                thumbnails = await page.query_selector_all("a#thumbnail")
                if thumbnails and len(thumbnails) > 2:
                    target = random.choice(thumbnails[:6])
                    await target.click(timeout=3000)
                    await page.wait_for_timeout(random.randint(5000, 10000))
            except Exception:
                pass

        # 4. Google Maps — adds location trust
        if await _safe_goto("https://www.google.com/maps"):
            await page.wait_for_timeout(random.randint(3000, 5000))
            await _random_mouse_movement()

        # 5. Second Google search for variety
        if await _safe_goto("https://www.google.com"):
            try:
                search_box = await page.query_selector("textarea[name='q'], input[name='q']")
                if search_box:
                    await search_box.click()
                    await page.wait_for_timeout(300)
                    query2 = random.choice(["new gmail features", "google account security", "best email provider"])
                    for ch in query2:
                        await page.keyboard.type(ch)
                        await page.wait_for_timeout(random.randint(50, 110))
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(random.randint(3000, 5000))
                    await _human_scroll(200, 500)
            except Exception:
                pass

        # 6. Google Translate — adds another service signal
        if await _safe_goto("https://translate.google.com"):
            await page.wait_for_timeout(random.randint(2000, 4000))
            try:
                textarea = await page.query_selector("textarea")
                if textarea:
                    text = random.choice(["hello world", "good morning", "thank you", "how are you"])
                    await textarea.click()
                    for ch in text:
                        await page.keyboard.type(ch)
                        await page.wait_for_timeout(random.randint(50, 120))
                    await page.wait_for_timeout(random.randint(2000, 4000))
            except Exception:
                pass

        # 7. Warm accounts.google.com domain — critical for trust
        try:
            await page.goto("https://accounts.google.com", timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(random.randint(2000, 4000))
            await _random_mouse_movement()
        except Exception:
            pass

        logger.info("[PREWARM] Trust session built successfully.")

    except Exception as e:
        logger.warning(f"[PREWARM] Non-fatal error: {_redact_runtime_value(e)}")


# ──────────────────────────────────────────────────────────────────────────────
# Main Playwright Flow
# ──────────────────────────────────────────────────────────────────────────────
async def async_playwright_flow(i, num_accounts, username, first_name, last_name,
                                 password, progress, account_task, proxy,
                                 month, day, year, gender,
                                 use_sms_api=False, flow_mode="standard"):
    _update_progress(progress, account_task, completed=5, description="Starting Playwright Stealth flow...")
    manager = PlaywrightStealthManager()

    try:
        # ── Initialize browser ────────────────────────────────────────────
        if not await manager.initialize(proxy=proxy, is_premium=use_sms_api):
            logger.error("PlaywrightStealthManager.initialize() returned False")
            return False, CreationError.BROWSER_CRASH

        page = manager.page
        is_mobile = getattr(manager, 'is_mobile', False)

        # ── Pre-Warming ──────────────────────────────────────────────────
        if not use_sms_api:
            _update_progress(progress, account_task, completed=10, description="Building Google trust session...")
            await _google_prewarm(page)
        else:
            _update_progress(progress, account_task, completed=10, description="Premium Mode: Direct to Registration...")

        # ── Step 1: Navigate to Google Signup ────────────────────────────
        _update_progress(progress, account_task, completed=15,
                        description=f"Opening Google Sign Up page [{flow_mode.upper()}]...")

        if flow_mode == "youtube":
            signup_urls = [
                "https://accounts.google.com/signup/v2/webcreateaccount?biz=false&cc=youtube&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26app%3Ddesktop%26hl%3Den%26next%3Dhttps%253A%252F%252Fwww.youtube.com%252F&flowEntry=SignUp&flowName=GlifWebSignIn&hl=en",
                "https://accounts.google.com/signup/v2/createaccount?continue=https://www.youtube.com/&flowName=GlifWebSignIn&flowEntry=SignUp",
            ]
        elif flow_mode == "workspace":
            signup_urls = [
                "https://accounts.google.com/lifecycle/steps/signup/name?continue=https://workspace.google.com/&flowEntry=SignUp&flowName=GlifWebSignIn&hl=en",
                "https://accounts.google.com/signup/v2/createaccount?continue=https://workspace.google.com/&flowName=GlifWebSignIn&flowEntry=SignUp",
            ]
        else:
            signup_urls = [
                "https://accounts.google.com/lifecycle/steps/signup/name?continue=https%3A%2F%2Fmyaccount.google.com%3Futm_source%3Daccount&dsh=S1527412391%3A" + str(random.randint(1000000000, 9999999999)) + "&flowEntry=SignUp&flowName=GlifWebSignIn&hl=en&theme=glif",
                "https://accounts.google.com/signup/v2/webcreateaccount?biz=false&cc=youtube&continue=https%3A%2F%2Fwww.youtube.com%2Fsignin&flowEntry=SignUp&flowName=GlifWebSignIn&hl=en",
                "https://accounts.google.com/lifecycle/steps/signup/name?continue=https%3A%2F%2Fmail.google.com%2Fmail%2F&flowEntry=SignUp&flowName=GlifWebSignIn&hl=en-US&service=mail&theme=glif",
                "https://accounts.google.com/signup/v2/createaccount?biz=false&flowName=GlifWebSignIn&flowEntry=SignUp&hl=en",
                "https://accounts.google.com/lifecycle/steps/signup/name?continue=https%3A%2F%2Fplay.google.com&flowEntry=SignUp&flowName=GlifWebSignIn&hl=en&theme=glif",
            ]
            random.shuffle(signup_urls)

        ERROR_PAGE_SIGNALS = [
            "something went wrong", "حدث خطأ ما", "try again later",
            "حاول مرة أخرى لاحقًا", "couldn't create your account",
            "تعذر إنشاء حسابك", "sorry, we couldn't sign you up",
            "this browser or app may not be secure",
            "المتصفح أو التطبيق غير آمن",
        ]

        navigated = False
        random.shuffle(signup_urls)

        for url_idx, url in enumerate(signup_urls):
            try:
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                await page.wait_for_timeout(random.randint(1500, 3000))

                # Check for "Something went wrong" or error pages
                try:
                    page_content = (await page.content()).lower()
                    page_url = page.url.lower()

                    if any(sig in page_content for sig in ERROR_PAGE_SIGNALS):
                        logger.warning(f"Error page detected on URL #{url_idx+1}: {url[:60]}...")
                        _update_progress(progress, account_task,
                                        description=f"[yellow]Error page detected, trying alternate URL...[/]")
                        await page.wait_for_timeout(random.randint(3000, 6000))
                        # Clear cookies and try again with next URL
                        try:
                            await page.context.clear_cookies()
                            await page.wait_for_timeout(1000)
                        except Exception:
                            pass
                        continue

                    if "accounts.google.com/v3/signin/rejected" in page_url or "accounts.google.com/speedbump" in page_url:
                        logger.warning(f"Rejected/speedbump page on URL #{url_idx+1}")
                        await page.wait_for_timeout(random.randint(3000, 6000))
                        continue
                except Exception:
                    pass

                el = await page.wait_for_selector('input[name="firstName"]', timeout=10000)
                if el:
                    navigated = True
                    break
            except Exception:
                logger.debug(f"Signup URL #{url_idx+1} failed, trying next...")
                await page.wait_for_timeout(random.randint(2000, 4000))
                continue

        if not navigated:
            # Last resort: navigate to google.com, then manually navigate to signup
            try:
                logger.info("All signup URLs failed. Trying via google.com redirect...")
                _update_progress(progress, account_task,
                                description="[yellow]Trying alternate signup path...[/]")
                await page.goto("https://accounts.google.com/signin", timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                # Click "Create account"
                for create_sel in [
                    "a:has-text('Create account')", "button:has-text('Create account')",
                    "a:has-text('إنشاء حساب')", "span:has-text('Create account')",
                ]:
                    try:
                        el = await page.query_selector(create_sel)
                        if el and await el.is_visible():
                            await el.click()
                            await page.wait_for_timeout(2000)
                            break
                    except Exception:
                        continue
                # Click "For my personal use"
                for personal_sel in [
                    "li:has-text('For my personal use')", "span:has-text('For my personal use')",
                    "li:has-text('لاستخدامي الشخصي')", "div:has-text('For my personal use')",
                ]:
                    try:
                        el = await page.query_selector(personal_sel)
                        if el and await el.is_visible():
                            await el.click()
                            await page.wait_for_timeout(3000)
                            break
                    except Exception:
                        continue

                el = await page.wait_for_selector('input[name="firstName"]', timeout=10000)
                if el:
                    navigated = True
            except Exception:
                pass

        if not navigated:
            logger.error("Could not find Google signup form after trying all URLs.")
            return False, CreationError.TIMEOUT

        await page.wait_for_timeout(1000)

        # ── Step 2: Enter Name ────────────────────────────────────────────
        _update_progress(progress, account_task, completed=30, description="Entering name...")
        typed_first = await manager.natural_type("input[name='firstName']", first_name)
        if not typed_first:
            try:
                el = await page.query_selector("input[name='firstName']")
                if el:
                    await el.fill(first_name)
            except Exception:
                pass
        await page.wait_for_timeout(400)

        typed_last = await manager.natural_type("input[name='lastName']", last_name)
        if not typed_last:
            try:
                el = await page.query_selector("input[name='lastName']")
                if el:
                    await el.fill(last_name)
            except Exception:
                pass
        await page.wait_for_timeout(600)

        await _try_click(page, [
            "button:has-text('Next')", "button:has-text('التالي')",
            "button[type='submit']",
        ])
        await page.wait_for_timeout(3000)

        # ── Step 3: Birthday & Gender ─────────────────────────────────────
        _update_progress(progress, account_task, completed=50, description="Entering birthday & gender...")
        await manager.fill_birthday_gender(month, day, year, gender)
        await page.wait_for_timeout(600)

        await _try_click(page, [
            "button:has-text('Next')", "button:has-text('التالي')",
            "button[type='submit']",
        ])
        await page.wait_for_timeout(3000)

        # Check if gender error appeared — retry if so
        for _gender_retry in range(3):
            try:
                page_text = await page.content()
                gender_errors = [
                    "please select your gender", "select your gender",
                    "يُرجى تحديد الجنس", "حدد جنسك",
                    "enter your birthday", "أدخل تاريخ ميلادك",
                ]
                if any(err in page_text.lower() for err in gender_errors):
                    logger.warning(f"Gender/birthday error detected (retry {_gender_retry+1})")
                    _update_progress(progress, account_task, description="Retrying gender selection...")
                    await manager.fill_birthday_gender(month, day, year, gender)
                    await page.wait_for_timeout(800)
                    await _try_click(page, [
                        "button:has-text('Next')", "button:has-text('التالي')",
                        "button[type='submit']",
                    ])
                    await page.wait_for_timeout(3000)
                else:
                    break
            except Exception:
                break

        # ── Step 4: Choose username ───────────────────────────────────────
        _update_progress(progress, account_task, completed=60, description="Choosing Gmail username...")
        await page.wait_for_timeout(2500)

        USERNAME_SELECTORS = [
            'input[name="Username"]', 'input[name="username"]',
            'input[type="text"][autocomplete="username"]',
            'input[aria-label*="username" i]', 'input[aria-label*="Gmail address" i]',
        ]

        async def find_username_field():
            for sel in USERNAME_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        return el
                except Exception:
                    continue
            return None

        username_field = await find_username_field()

        if not username_field:
            # Try "Create your own" option
            try:
                loc = page.locator("text=Create your own Gmail address").or_(
                    page.locator("text=إنشاء عنوان Gmail"))
                if await loc.count() > 0:
                    await loc.first.click()
                    await page.wait_for_timeout(1500)
            except Exception:
                pass

            if not await find_username_field():
                try:
                    radios = await page.query_selector_all("input[type='radio']")
                    if radios:
                        await radios[-1].scroll_into_view_if_needed()
                        await radios[-1].click()
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

            if not await find_username_field():
                try:
                    await page.evaluate("""() => {
                        for (const el of document.querySelectorAll('label,span,div')) {
                            if (el.textContent.trim().includes('Create your own') ||
                                el.textContent.trim().includes('إنشاء عنوان Gmail')) {
                                el.click(); break;
                            }
                        }
                    }""")
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass

            for _ in range(16):
                username_field = await find_username_field()
                if username_field:
                    break
                await asyncio.sleep(0.5)

        def _make_alt_username(base, attempt):
            import string as _str
            suffixes = [
                str(random.randint(100, 9999)),
                "".join(random.choices(_str.ascii_lowercase, k=3)),
                str(random.randint(10, 99)) + random.choice(_str.ascii_lowercase),
                first_name.lower() + str(random.randint(10, 999)),
                last_name.lower() + str(random.randint(10, 999)),
            ]
            return base.rstrip("0123456789") + suffixes[attempt % len(suffixes)]

        current_username = username
        max_username_retries = 5

        for attempt in range(max_username_retries):
            username_field = await find_username_field()
            if not username_field:
                await page.evaluate("""(u) => {
                    const sels = ['input[name="Username"]','input[name="username"]',
                                  'input[autocomplete="username"]','input[type="text"]'];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.offsetParent !== null) {
                            el.focus(); el.value = u;
                            el.dispatchEvent(new Event('input',{bubbles:true}));
                            el.dispatchEvent(new Event('change',{bubbles:true}));
                            break;
                        }
                    }
                }""", current_username)
            else:
                await username_field.scroll_into_view_if_needed()
                await username_field.click()
                await page.wait_for_timeout(200)
                await username_field.click(click_count=3)
                await page.wait_for_timeout(100)
                await username_field.fill(current_username)
                await username_field.dispatch_event("input")
                await username_field.dispatch_event("change")
                await page.wait_for_timeout(800)

            await page.wait_for_timeout(600)
            await _try_click(page, ["button:has-text('Next')", "button[type='submit']"])
            await page.wait_for_timeout(3000)

            try:
                page_text = await page.content()
                taken_signals = [
                    "that username is taken", "username is taken", "try another",
                    "اسم المستخدم مأخوذ", "جرب اسمًا آخر", "this username is not available",
                ]
                if any(s.lower() in page_text.lower() for s in taken_signals):
                    if attempt < max_username_retries - 1:
                        current_username = _make_alt_username(current_username, attempt)
                        _update_progress(progress, account_task,
                                        description=f"[yellow]Username taken -> retrying: {current_username}[/]")
                        await page.wait_for_timeout(1200)
                        continue
                    else:
                        return False, CreationError.USERNAME_TAKEN
                else:
                    username = current_username
                    break
            except Exception:
                break

        # ── Step 5: Password ──────────────────────────────────────────────
        _update_progress(progress, account_task, completed=70, description="Entering password...")

        for sel in ['input[name="Passwd"]', 'input[type="password"]', 'input[aria-label*="password" i]']:
            try:
                pw_el = await page.wait_for_selector(sel, timeout=10000)
                if pw_el and await pw_el.is_visible():
                    await pw_el.click()
                    await page.wait_for_timeout(200)
                    await pw_el.fill(password)
                    break
            except Exception:
                continue

        for sel in ['input[name="PasswdAgain"]', 'input[name="ConfirmPasswd"]',
                    'input[aria-label*="Confirm" i]']:
            try:
                cp_el = await page.query_selector(sel)
                if cp_el and await cp_el.is_visible():
                    await cp_el.click()
                    await page.wait_for_timeout(200)
                    await cp_el.fill(password)
                    break
            except Exception:
                continue

        await page.wait_for_timeout(600)
        await _try_click(page, ["button:has-text('Next')", "button[type='submit']"])
        await page.wait_for_timeout(4000)

        # ── Step 6: Verification (using phone_bypass module) ──────────────
        _update_progress(progress, account_task, completed=85, description="Handling verification...")

        success, method, should_restart = await handle_verification(
            page,
            is_mobile=is_mobile,
            use_sms_api=use_sms_api,
            progress=progress,
            account_task=account_task,
        )

        if should_restart:
            # Escaped verification — need to restart the entire signup flow
            logger.info(f"Verification escaped ({method}) — restarting signup flow...")
            _update_progress(progress, account_task, completed=25,
                            description=f"[green]Escaped {method} — restarting from name...[/]")
            await page.wait_for_timeout(1200)

            try:
                typed = await manager.natural_type("input[name='firstName']", first_name)
                if not typed:
                    el = await page.query_selector("input[name='firstName']")
                    if el:
                        await el.fill(first_name)
                await page.wait_for_timeout(400)
                typed = await manager.natural_type("input[name='lastName']", last_name)
                if not typed:
                    el = await page.query_selector("input[name='lastName']")
                    if el:
                        await el.fill(last_name)
                await page.wait_for_timeout(600)
                await _try_click(page, [
                    "button:has-text('Next')", "button:has-text('التالي')",
                    "button[type='submit']",
                ])
                await page.wait_for_timeout(3000)
            except Exception as e:
                logger.error(f"Failed to re-enter name after QR escape: {_redact_runtime_value(e)}")
                return False, CreationError.QR_BLOCKED

            await manager.fill_birthday_gender(month, day, year, gender)
            await page.wait_for_timeout(600)
            await _try_click(page, ["button:has-text('Next')", "button[type='submit']"],
                             is_mobile=is_mobile)
            await page.wait_for_timeout(3000)

            # Re-enter username
            try:
                radios = await page.query_selector_all("input[type='radio']")
                if radios:
                    await radios[-1].scroll_into_view_if_needed()
                    await radios[-1].click()
                    await page.wait_for_timeout(1000)

                for sel in ['input[name="Username"]', 'input[name="username"]', 'input[type="text"]']:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.fill(username)
                        await page.wait_for_timeout(500)
                        break

                await _try_click(page, ["button:has-text('Next')", "button[type='submit']"])
                await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Re-enter password
            try:
                for sel in ['input[name="Passwd"]', 'input[type="password"]']:
                    pw_el = await page.query_selector(sel)
                    if pw_el and await pw_el.is_visible():
                        await pw_el.fill(password)
                        break
                for sel in ['input[name="PasswdAgain"]', 'input[aria-label*="Confirm" i]']:
                    pw2_el = await page.query_selector(sel)
                    if pw2_el and await pw2_el.is_visible():
                        await pw2_el.fill(password)
                        break
                await page.wait_for_timeout(500)
                await _try_click(page, ["button:has-text('Next')", "button[type='submit']"])
                await page.wait_for_timeout(3000)
            except Exception:
                pass

            # Re-check verification after restart
            success2, method2, _ = await handle_verification(
                page, is_mobile=is_mobile, use_sms_api=use_sms_api,
                progress=progress, account_task=account_task,
            )
            if not success2:
                error_type = CreationError.QR_BLOCKED if "qr" in method2 else CreationError.PHONE_REQUIRED
                return False, error_type
            method = method2

        if not success:
            error_type = CreationError.QR_BLOCKED if "qr" in method else CreationError.PHONE_REQUIRED
            if "send_sms" in method:
                _update_progress(progress, account_task,
                                description=f"[bold red]IP flagged — use proxy/VPN[/]")
            else:
                _update_progress(progress, account_task,
                                description=f"[bold red]Failed: {method}[/]")
            return False, error_type

        # ── Step 7: Accept terms ──────────────────────────────────────────
        _update_progress(progress, account_task, completed=92, description="Accepting terms...")

        # First check if we're actually on a terms/privacy page
        try:
            current_url = page.url.lower()
            page_html = await page.content()
            page_lower = page_html.lower()

            on_terms_page = (
                "privacy" in page_lower or "terms" in page_lower or
                "i agree" in page_lower or "أوافق" in page_lower or
                "express personalization" in page_lower or
                "التخصيص السريع" in page_lower or
                "consentsummary" in current_url or
                "speedbump" in current_url
            )

            if not on_terms_page:
                # Check if already on success page
                success_signals = [
                    "myaccount.google.com", "mail.google.com",
                    "welcome to google", "مرحبًا بك في google",
                ]
                if any(s in page_lower or s in current_url for s in success_signals):
                    logger.info("Already past terms page — account appears created")
                    on_terms_page = False
        except Exception:
            on_terms_page = True

        # Check for captcha before accepting terms
        try:
            captcha_frame = await page.query_selector('iframe[src*="recaptcha"], iframe[title*="reCAPTCHA"]')
            if captcha_frame:
                from core.captcha_solver import CaptchaSolver
                site_key = await page.evaluate("""() => {
                    const el = document.querySelector('.g-recaptcha');
                    return el ? el.getAttribute('data-sitekey') : null;
                }""")
                if site_key:
                    _update_progress(progress, account_task, description="Solving captcha...")
                    token = await asyncio.to_thread(CaptchaSolver.solve, site_key, page.url)
                    if token:
                        await page.evaluate(f"""(token) => {{
                            const el = document.getElementById('g-recaptcha-response');
                            if (el) {{ el.value = token; el.style.display = 'none'; }}
                            if (typeof ___grecaptcha_cfg !== 'undefined') {{
                                Object.entries(___grecaptcha_cfg.clients).forEach(([k,v]) => {{
                                    if (v && v.R && v.R.callback) v.R.callback(token);
                                }});
                            }}
                        }}""", token)
                        logger.info("Captcha solved and injected successfully")
                        await page.wait_for_timeout(1000)
        except Exception as cap_err:
            logger.debug(f"Captcha check (non-fatal): {cap_err}")

        # Click agree/accept/continue buttons (with short timeouts to avoid hanging)
        terms_selectors = [
            "button:has-text('I agree')", "button:has-text('أوافق')",
            "button:has-text('Agree')", "button:has-text('Accept')",
            "button:has-text('Continue')", "button:has-text('متابعة')",
        ]
        await _try_click(page, terms_selectors, timeout=2000)
        await page.wait_for_timeout(2000)

        # Click through additional screens (express personalization, etc) — max 3 rounds
        extra_selectors = [
            "button:has-text('I agree')", "button:has-text('أوافق')",
            "button:has-text('Accept all')", "button:has-text('قبول الكل')",
            "button:has-text('Confirm')", "button:has-text('تأكيد')",
            "button:has-text('Next')", "button:has-text('التالي')",
            "button:has-text('Continue')", "button:has-text('متابعة')",
            "button:has-text('Skip')", "button:has-text('تخطي')",
        ]
        for _ in range(3):
            clicked = await _try_click(page, extra_selectors, timeout=1500)
            if not clicked:
                break
            await page.wait_for_timeout(1500)

        # ── Step 8: Verify account was actually created ───────────────────
        _update_progress(progress, account_task, completed=95, description="Verifying account creation...")

        account_verified = False
        try:
            current_url = page.url.lower()
            verified_urls = [
                "myaccount.google.com", "mail.google.com",
                "accounts.google.com/signin/continue",
                "youtube.com", "workspace.google.com",
                "gds.google.com",
            ]
            if any(v in current_url for v in verified_urls):
                account_verified = True
                logger.info(f"Account verified via URL: {current_url}")

            if not account_verified:
                content = await page.content()
                content_lower = content.lower()
                verified_signals = [
                    "welcome to google", "مرحبًا بك في google",
                    "your new account", "حسابك الجديد",
                    "your google account is ready", "حسابك في google جاهز",
                    "inbox", "primary", "promotions",
                    "search mail", "compose",
                ]
                if any(s in content_lower for s in verified_signals):
                    account_verified = True
                    logger.info("Account verified via page content signals")

            if not account_verified:
                try:
                    await page.goto("https://myaccount.google.com/", timeout=15000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(3000)
                    my_url = page.url.lower()
                    my_content = await page.content()
                    if "myaccount.google.com" in my_url and "sign in" not in my_content.lower():
                        account_verified = True
                        logger.info("Account verified via myaccount.google.com navigation")
                except Exception:
                    pass

        except Exception as verify_err:
            logger.debug(f"Verification check error (non-fatal): {verify_err}")

        if not account_verified:
            logger.warning("Could not verify account creation — account may not have been created")
            _update_progress(progress, account_task, completed=100,
                            description="[bold yellow]Unverified — account may not exist[/]")
            return False, CreationError.UNKNOWN

        # ── Done ──────────────────────────────────────────────────────────
        _update_progress(progress, account_task, completed=100,
                        description=f"[bold green]SUCCESS: {username}@gmail.com[/]")
        logger.info(f"Account VERIFIED and created: {username}@gmail.com")

        # Save to database
        try:
            from core.account_manager import account_manager
            account_manager.save(
                email=f"{username}@gmail.com",
                password=password,
                first_name=first_name,
                last_name=last_name,
                proxy=proxy or "",
                strategy=flow_mode,
                sms_service=method if "sms" in method else "",
                birthday=f"{month}/{day}/{year}",
                gender=gender,
            )
        except Exception as db_err:
            logger.error(f"Failed to save account: {db_err}")

        # Print credentials to console
        from core.ui import print_success
        print_success(f"CREATED: {username}@gmail.com | Password: {password}")

        # Send Telegram notification
        try:
            from core.telegram_notifier import notifier
            notifier.notify_account_created(
                email=f"{username}@gmail.com",
                password=password,
                strategy=flow_mode,
                proxy=proxy or "",
            )
        except Exception:
            pass

        # Post-creation account warming
        try:
            if Config.ENABLE_SESSION_WARMING:
                from core.account_warmer import warm_account_playwright
                _update_progress(progress, account_task, completed=98,
                                description="Warming new account...")
                await warm_account_playwright(f"{username}@gmail.com", password, duration_minutes=2)
        except Exception as warm_err:
            logger.debug(f"Account warming (non-fatal): {_redact_runtime_value(warm_err)}")

        return True, "success"

    except Exception as e:
        logger.error(f"Playwright flow failed: {_redact_runtime_value(e)}")
        return False, CreationError.UNKNOWN
    finally:
        await manager.close()


def run_playwright_flow(i, num_accounts, username, first_name, last_name, password,
                        progress, account_task, proxy,
                        month=None, day=None, year=None, gender=None,
                        use_sms_api=False, flow_mode="standard"):
    if PlaywrightStealthManager is None:
        logger.error("Playwright is not installed. Install with: pip install playwright && playwright install")
        return False
    if month is None or day is None or year is None:
        b = getattr(Config, "YOUR_BIRTHDAY", "2 4 1990")
        month, day, year = _parse_birthday(b)
    if gender is None:
        gender = str(getattr(Config, "YOUR_GENDER", "1"))

    result = asyncio.run(async_playwright_flow(
        i, num_accounts, username, first_name, last_name, password,
        progress, account_task, proxy, month, day, year, gender,
        use_sms_api, flow_mode,
    ))

    if isinstance(result, tuple):
        success, error_or_method = result
        if success:
            retry_engine.record_attempt(flow_mode, True)
        else:
            retry_engine.record_attempt(flow_mode, False, error_or_method)
        return success
    return result
