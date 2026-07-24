"""
Post-Creation Account Warmer - Login and warm up newly created accounts
Makes accounts appear more legitimate by simulating real user activity.
"""
import time
import random
import logging

logger = logging.getLogger('gmail_creator_postwarmer')


async def warm_account_playwright(email, password, duration_minutes=3):
    """
    Warm a newly created account using Playwright.
    Logs in and simulates natural activity.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — skipping account warming")
        return False

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            # Login to Gmail
            await page.goto("https://accounts.google.com/signin", timeout=30000)
            await page.wait_for_timeout(2000)

            email_input = await page.query_selector('input[type="email"]')
            if email_input:
                await email_input.fill(email)
                await page.wait_for_timeout(500)
                await page.click('button:has-text("Next"), button:has-text("التالي")')
                await page.wait_for_timeout(3000)

            pw_input = await page.query_selector('input[type="password"]')
            if pw_input:
                await pw_input.fill(password)
                await page.wait_for_timeout(500)
                await page.click('button:has-text("Next"), button:has-text("التالي")')
                await page.wait_for_timeout(5000)

            # Check if logged in
            if "myaccount" in page.url or "mail.google" in page.url:
                logger.info(f"Successfully logged in: {email}")
            else:
                logger.warning(f"Login may have failed for {email}: {page.url}")
                await browser.close()
                return False

            start = time.time()
            target = duration_minutes * 60

            # Visit Google services
            services = [
                "https://mail.google.com/",
                "https://www.youtube.com/",
                "https://drive.google.com/",
                "https://www.google.com/",
                "https://maps.google.com/",
            ]

            while time.time() - start < target:
                url = random.choice(services)
                try:
                    await page.goto(url, timeout=15000)
                    await page.wait_for_timeout(random.randint(3000, 8000))

                    # Scroll randomly
                    await page.evaluate(f"window.scrollBy(0, {random.randint(100, 500)})")
                    await page.wait_for_timeout(random.randint(1000, 3000))
                except Exception:
                    continue

            await browser.close()
            logger.info(f"Account warming complete for {email}")
            return True

    except Exception as e:
        logger.error(f"Account warming failed for {email}: {e}")
        return False


def warm_account_selenium(email, password, duration_minutes=3):  # noqa: ARG001
    """Selenium-based warmer — disabled in Hermes vendor build.

    The Selenium engine was dropped from this slim vendor to avoid the
    ~50MB of selenium + webdriver-manager + Chromedriver deps. Callers
    should use ``warm_account_playwright`` instead.
    """
    logger.warning(
        "warm_account_selenium is not available in the Hermes vendor build; "
        "use warm_account_playwright(%r, ...) instead",
        email,
    )
    return False
