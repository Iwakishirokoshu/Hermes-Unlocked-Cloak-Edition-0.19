import random
import logging
import time
from core.behavior import HumanBehavior

logger = logging.getLogger('gmail_creator_warmup')

class WarmupEngine:
    """Pre-browsing engine to build Google trust by visiting Google ecosystem sites"""

    GOOGLE_SITES = [
        "https://www.google.com",
        "https://www.youtube.com",
        "https://news.google.com",
        "https://maps.google.com",
        "https://play.google.com",
        "https://store.google.com",
        "https://translate.google.com",
        "https://scholar.google.com",
        "https://photos.google.com",
        "https://drive.google.com",
    ]

    GOOGLE_SEARCHES = [
        "best free email 2025", "how to create new email account",
        "google workspace features", "gmail tips and tricks",
        "weather today", "best restaurants near me",
        "latest technology news", "how to learn programming",
        "free online courses", "travel destinations 2025",
        "healthy recipes easy", "best movies this year",
    ]

    @staticmethod
    async def run_warmup(page, duration_minutes=3):
        """Build Google trust through real Google ecosystem browsing"""
        logger.info(f"Starting Google trust warmup for {duration_minutes} minutes...")

        end_time = time.time() + (duration_minutes * 60)

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

        try:
            await page.goto("https://www.google.com", timeout=30000, wait_until="domcontentloaded")
            await _accept_cookies()
            await page.wait_for_timeout(random.randint(1000, 2000))
        except Exception:
            pass

        while time.time() < end_time:
            try:
                action = random.random()

                if action < 0.4:
                    query = random.choice(WarmupEngine.GOOGLE_SEARCHES)
                    logger.info(f"Warmup: Google search '{query}'")
                    try:
                        await page.goto("https://www.google.com", timeout=20000, wait_until="domcontentloaded")
                        await _accept_cookies()
                        search_box = await page.query_selector("textarea[name='q'], input[name='q']")
                        if search_box:
                            await search_box.click()
                            await page.wait_for_timeout(300)
                            for ch in query:
                                await page.keyboard.type(ch)
                                await page.wait_for_timeout(random.randint(40, 110))
                            await page.wait_for_timeout(random.randint(500, 1000))
                            await page.keyboard.press("Enter")
                            await page.wait_for_timeout(random.randint(3000, 6000))
                            await HumanBehavior.human_scroll(page, 2, 5)

                            try:
                                results = await page.query_selector_all("a h3")
                                if results and len(results) > 1:
                                    target = random.choice(results[:5])
                                    await target.click(timeout=3000)
                                    await page.wait_for_timeout(random.randint(3000, 7000))
                                    await HumanBehavior.human_scroll(page, 1, 3)
                            except Exception:
                                pass
                    except Exception:
                        pass

                elif action < 0.7:
                    site = random.choice(WarmupEngine.GOOGLE_SITES)
                    logger.info(f"Warmup: Visiting {site}")
                    try:
                        await page.goto(site, timeout=20000, wait_until="domcontentloaded")
                        await _accept_cookies()
                        await HumanBehavior.human_scroll(page, 2, 5)
                        await page.wait_for_timeout(random.randint(3000, 7000))
                    except Exception:
                        pass

                else:
                    logger.info("Warmup: YouTube browse")
                    try:
                        await page.goto("https://www.youtube.com", timeout=20000, wait_until="domcontentloaded")
                        await _accept_cookies()
                        await HumanBehavior.human_scroll(page, 2, 4)
                        await page.wait_for_timeout(random.randint(3000, 6000))

                        try:
                            thumbnails = await page.query_selector_all("a#thumbnail")
                            if thumbnails and len(thumbnails) > 2:
                                target = random.choice(thumbnails[:8])
                                await target.click(timeout=3000)
                                await page.wait_for_timeout(random.randint(8000, 20000))
                        except Exception:
                            pass
                    except Exception:
                        pass

                await page.wait_for_timeout(random.randint(3000, 8000))
            except Exception as e:
                logger.warning(f"Warmup error: {e}")
                continue

        try:
            await page.goto("https://accounts.google.com", timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(random.randint(1500, 2500))
        except Exception:
            pass

        logger.info("Google trust warmup complete.")
        return True
