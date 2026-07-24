"""
Trust Builder - Session warming and trust building for Google accounts
Works with Selenium WebDriver. For Playwright, use core/warmup.py.
"""
import time
import random
import logging
import requests

logger = logging.getLogger('gmail_creator_trust')


TRUST_SITES = [
    ("Google Search", "https://www.google.com/", 15, 30),
    ("YouTube", "https://www.youtube.com/", 30, 60),
    ("Google Maps", "https://www.google.com/maps", 15, 25),
    ("Google News", "https://news.google.com/", 15, 25),
    ("Google Drive", "https://drive.google.com/", 10, 20),
    ("Google Play", "https://play.google.com/", 10, 20),
    ("Google Translate", "https://translate.google.com/", 10, 15),
    ("Google Images", "https://images.google.com/", 10, 20),
    ("Google Photos", "https://photos.google.com/", 10, 15),
]

SEARCH_QUERIES = [
    "weather today", "latest news", "best restaurants near me",
    "best coffee shops near me", "weather forecast this week",
    "latest technology news", "how to learn programming",
    "popular movies 2025", "healthy recipes dinner",
    "travel destinations summer", "online courses free",
]


def _random_scroll(driver, min_px=100, max_px=500):
    try:
        driver.execute_script(
            f"window.scrollBy(0, {random.randint(min_px, max_px)})"
        )
        time.sleep(random.uniform(0.5, 1.5))
    except Exception:
        pass


def _human_typing(element, text, delay_range=(0.05, 0.12)):
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(*delay_range))


def network_trust_check():
    """Check if IP is residential or datacenter."""
    try:
        ip_resp = requests.get("https://api.ipify.org?format=json", timeout=5)
        ip = ip_resp.json().get("ip", "Unknown")
        info_resp = requests.get(f"https://ipinfo.io/{ip}/json", timeout=5)
        info = info_resp.json()
        is_datacenter = "hosting" in str(info.get("org", "")).lower()
        logger.info(f"IP: {ip}, Location: {info.get('city', 'N/A')}, Datacenter: {is_datacenter}")
        return {
            "ip": ip,
            "city": info.get("city", "N/A"),
            "country": info.get("country", "N/A"),
            "is_datacenter": is_datacenter,
        }
    except Exception:
        return {"ip": "Unknown", "city": "N/A", "country": "N/A", "is_datacenter": False}


def profile_aging_simulation(driver):
    """Inject browsing history patterns and cookie age markers."""
    try:
        driver.execute_script("""
            const now = Date.now();
            const dayMs = 86400000;
            localStorage.setItem('_gp_aging', JSON.stringify({
                firstVisit: now - (dayMs * 30),
                visits: Math.floor(Math.random() * 50) + 20,
                lastVisit: now - (dayMs * 1)
            }));
            sessionStorage.setItem('_session_trust', 'established');
        """)
        logger.info("Profile aging markers set")
        return True
    except Exception as e:
        logger.warning(f"Profile aging failed: {e}")
        return False


def warm_up_session(driver):
    """Basic session warming - visit Google and do natural searches."""
    logger.info("Starting session warming")
    try:
        driver.get("https://www.google.com/")
        time.sleep(random.uniform(3, 5))

        _accept_cookies(driver)

        searches = random.sample(SEARCH_QUERIES, min(3, len(SEARCH_QUERIES)))
        for query in searches:
            try:
                from selenium.webdriver.common.by import By
                search_box = driver.find_element(By.NAME, "q")
                search_box.clear()
                _human_typing(search_box, query)
                search_box.submit()
                time.sleep(random.uniform(3, 6))

                results = driver.find_elements(By.XPATH, "//h3")
                if results:
                    random.choice(results[:5]).click()
                    time.sleep(random.uniform(5, 10))
                    _random_scroll(driver, 100, 400)
                    driver.back()
                    time.sleep(random.uniform(2, 4))
            except Exception:
                continue

        logger.info("Session warming complete")
        return True
    except Exception as e:
        logger.warning(f"Session warming error: {e}")
        return False


def deep_trust_builder(driver, duration_minutes=5):
    """Extended browsing simulation to build Google trust."""
    logger.info(f"Starting Deep Trust Builder ({duration_minutes} min)")
    from selenium.webdriver.common.by import By

    start_time = time.time()
    target_duration = duration_minutes * 60
    visited = 0

    while time.time() - start_time < target_duration:
        action = random.choice(TRUST_SITES)
        name, url, min_time, max_time = action

        try:
            driver.get(url)
            time.sleep(random.uniform(2, 4))

            for _ in range(random.randint(2, 5)):
                _random_scroll(driver)

            if "youtube" in url.lower():
                try:
                    videos = driver.find_elements(By.XPATH, "//a[@id='video-title']")
                    if videos and len(videos) > 3:
                        random.choice(videos[:8]).click()
                        watch_time = random.randint(30, 60)
                        time.sleep(watch_time)
                except Exception:
                    pass

            elif "google.com" in url.lower():
                try:
                    search_box = driver.find_element(By.NAME, "q")
                    queries = ["weather today", "news", "restaurants near me", "movies 2025"]
                    search_box.clear()
                    _human_typing(search_box, random.choice(queries))
                    search_box.submit()
                    time.sleep(random.uniform(3, 6))
                    results = driver.find_elements(By.XPATH, "//h3")
                    if results:
                        random.choice(results[:5]).click()
                        time.sleep(random.uniform(5, 10))
                        driver.back()
                except Exception:
                    pass

            time.sleep(random.uniform(min_time, max_time))
            visited += 1
        except Exception:
            continue

    logger.info(f"Deep Trust Builder complete: {visited} sites visited")
    return visited


def ultra_deep_trust_builder(driver, duration_minutes=10):
    """Maximum trust building: YouTube engagement + services tour + natural search."""
    logger.info(f"Starting Ultra Deep Trust Builder ({duration_minutes} min)")
    from selenium.webdriver.common.by import By

    start_time = time.time()
    target_duration = duration_minutes * 60

    # Phase 1: YouTube engagement
    try:
        driver.get("https://www.youtube.com/")
        time.sleep(random.uniform(3, 5))

        for _ in range(random.randint(2, 4)):
            try:
                videos = driver.find_elements(By.XPATH, "//a[@id='video-title']")
                if videos and len(videos) > 5:
                    random.choice(videos[:10]).click()
                    watch_time = random.randint(45, 90)
                    for _ in range(watch_time // 10):
                        time.sleep(10)
                        _random_scroll(driver, 50, 200)
                    driver.back()
                    time.sleep(random.uniform(2, 4))
            except Exception:
                continue
    except Exception:
        pass

    # Phase 2: Google services tour
    services = [
        ("Google Search", "https://www.google.com/"),
        ("Google Maps", "https://www.google.com/maps"),
        ("Google News", "https://news.google.com/"),
        ("Google Translate", "https://translate.google.com/"),
        ("Google Drive", "https://drive.google.com/"),
        ("Google Photos", "https://photos.google.com/"),
        ("Google Play", "https://play.google.com/"),
    ]

    for name, url in services:
        if time.time() - start_time >= target_duration:
            break
        try:
            driver.get(url)
            time.sleep(random.uniform(5, 10))
            for _ in range(random.randint(3, 6)):
                _random_scroll(driver)
                time.sleep(random.uniform(1, 3))
            try:
                links = driver.find_elements(By.TAG_NAME, "a")
                safe_links = [l for l in links if l.is_displayed() and "google" in str(l.get_attribute("href") or "")]
                if safe_links:
                    random.choice(safe_links[:5]).click()
                    time.sleep(random.uniform(3, 6))
                    driver.back()
            except Exception:
                pass
        except Exception:
            continue

    # Phase 3: Natural search behavior
    try:
        driver.get("https://www.google.com/")
        time.sleep(2)

        for query in random.sample(SEARCH_QUERIES, min(4, len(SEARCH_QUERIES))):
            if time.time() - start_time >= target_duration:
                break
            try:
                search_box = driver.find_element(By.NAME, "q")
                search_box.clear()
                _human_typing(search_box, query)
                search_box.submit()
                time.sleep(random.uniform(3, 5))
                results = driver.find_elements(By.XPATH, "//h3")
                if results:
                    random.choice(results[:5]).click()
                    time.sleep(random.uniform(5, 10))
                    _random_scroll(driver, 100, 400)
                    driver.back()
                    time.sleep(random.uniform(2, 4))
            except Exception:
                continue
    except Exception:
        pass

    elapsed = (time.time() - start_time) / 60
    logger.info(f"Ultra Deep Trust Builder complete: {elapsed:.1f} min")
    return True


def ghost_mode_prepare(driver, warmup_minutes=10):
    """Full ghost mode preparation: network check + fingerprint + aging + trust building."""
    from core.fingerprint import inject_selenium_fingerprint, inject_selenium_poltergeist

    net_info = network_trust_check()
    if net_info["is_datacenter"]:
        logger.warning("Datacenter IP detected - residential proxy recommended")

    inject_selenium_fingerprint(driver)
    inject_selenium_poltergeist(driver)
    profile_aging_simulation(driver)
    ultra_deep_trust_builder(driver, warmup_minutes)

    logger.info("Ghost mode preparation complete")
    return True


def _accept_cookies(driver):
    """Try to accept Google cookie consent dialog."""
    try:
        from selenium.webdriver.common.by import By
        accept_buttons = [
            "//button[contains(text(), 'Accept')]",
            "//button[contains(text(), 'I agree')]",
            "//button[contains(text(), 'قبول')]",
            "//button[@id='L2AGLb']",
        ]
        for selector in accept_buttons:
            try:
                btn = driver.find_element(By.XPATH, selector)
                if btn.is_displayed():
                    btn.click()
                    time.sleep(1)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False
