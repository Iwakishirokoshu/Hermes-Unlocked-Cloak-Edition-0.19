"""
Batch Runner - Multi-threaded account creation with progress tracking
Uses ThreadPoolExecutor for parallel browser instances.
"""
import time
import random
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from config.settings import Config
from core.proxy_manager import proxy_manager
from core.account_manager import account_manager
from core.retry_engine import retry_engine
from core.database import DatabaseManager

logger = logging.getLogger('gmail_creator_batch')


def _generate_username():
    from core._legacy import generate_name
    name = generate_name()
    parts = name.split()
    first = parts[0].lower() if parts else "user"
    last = parts[-1].lower() if len(parts) > 1 else "gmail"
    username = f"{first}{last}{random.randint(1000, 9999)}"
    return username, parts[0] if parts else "User", parts[-1] if len(parts) > 1 else "User"


def _load_fixed_password() -> str | None:
    """Return a shared password from env/file, or None to auto-generate per account."""
    password = (Config.YOUR_PASSWORD or "").strip()
    if password:
        return password
    try:
        with open("config/password.txt", "r", encoding="utf-8") as f:
            password = f.read().strip()
            if password:
                return password
    except FileNotFoundError:
        pass
    return None


def _create_single_account(index, total, engine, fixed_password, warmup_minutes,
                            flow_mode, use_sms_api):
    """Create a single account. Designed to run in a thread."""
    from core._legacy import generate_password

    username, first_name, last_name = _generate_username()
    password = fixed_password or generate_password()
    proxy = proxy_manager.get_best() or proxy_manager.get_next()

    success = False
    error_type = None

    try:
        if engine == 'playwright':
            from core.runners import run_playwright_flow
            success = run_playwright_flow(
                index, total, username, first_name, last_name,
                password, None, None, proxy,
                use_sms_api=use_sms_api, flow_mode=flow_mode,
            )
        elif engine == 'appium':
            from core.runners import run_appium_flow
            month, day, year = Config.YOUR_BIRTHDAY.split() if Config.YOUR_BIRTHDAY else ("1", "1", "1990")
            success = run_appium_flow(
                index, total, username, first_name, last_name,
                password, month, day, year, str(Config.YOUR_GENDER),
                None, None,
            )
        else:
            # Hermes vendor build is Playwright-only; selenium/appium engines
            # were dropped. Fail loudly rather than silently launching nothing.
            logger.error(
                "engine=%r is not bundled in the Hermes vendor build "
                "(Playwright-only). Set ENGINE_MODE=playwright in /etc/gmail-factory/runner.env.",
                engine,
            )
            success = False
    except Exception as e:
        logger.error(f"Thread {index}: {e}")
        error_type = str(e)

    if success and proxy:
        proxy_manager.mark_success(proxy)
    elif not success and proxy:
        proxy_manager.mark_failure(proxy)

    return {
        "index": index,
        "username": username,
        "email": f"{username}@gmail.com",
        "password": password,
        "success": success,
        "error_type": error_type,
        "proxy": proxy,
    }


def run_batch(num_accounts, max_threads=3, warmup_minutes=5,
              flow_mode='standard', use_sms_api=False, on_result=None):
    """
    Run account creation in parallel using a thread pool.

    Args:
        num_accounts: Total accounts to create
        max_threads: Maximum concurrent browser instances (1-5)
        warmup_minutes: Trust building duration per account
        flow_mode: Signup URL route (standard/youtube/workspace)
        use_sms_api: Whether to use SMS API for verification
        on_result: Callback(result_dict) called after each account attempt

    Returns:
        dict with summary stats
    """
    max_threads = max(1, min(5, max_threads))
    engine = getattr(Config, 'ENGINE_MODE', 'playwright').lower()
    fixed_password = _load_fixed_password()

    if fixed_password:
        logger.info("Using fixed YOUR_PASSWORD / config/password.txt for all accounts")
    else:
        logger.info(
            "YOUR_PASSWORD unset — generating a unique random password per account"
        )

    start_time = time.time()
    results = []

    logger.info(f"Starting batch: {num_accounts} accounts, {max_threads} threads, "
                f"engine={engine}, mode={flow_mode}")

    with ThreadPoolExecutor(max_workers=max_threads) as executor:
        futures = {}
        for i in range(num_accounts):
            future = executor.submit(
                _create_single_account,
                i, num_accounts, engine, fixed_password,
                warmup_minutes, flow_mode, use_sms_api,
            )
            futures[future] = i

            if i < num_accounts - 1 and len(futures) >= max_threads:
                time.sleep(random.uniform(2, 5))

        for future in as_completed(futures):
            try:
                result = future.result(timeout=600)
                results.append(result)
                if on_result:
                    on_result(result)
            except Exception as e:
                idx = futures[future]
                results.append({
                    "index": idx, "username": "unknown",
                    "email": "unknown", "success": False,
                    "error_type": str(e), "proxy": None,
                })

    duration = time.time() - start_time
    successes = sum(1 for r in results if r["success"])
    failures = len(results) - successes

    db = DatabaseManager()
    db.save_session_stats(
        total_attempts=num_accounts, successes=successes, failures=failures,
        strategies_used={flow_mode: num_accounts},
        errors={r["error_type"]: 1 for r in results if r["error_type"]},
        duration_seconds=duration,
    )

    logger.info(f"Batch complete: {successes}/{num_accounts} success, {duration:.0f}s")

    return {
        "total": num_accounts,
        "successes": successes,
        "failures": failures,
        "duration": duration,
        "results": results,
    }
