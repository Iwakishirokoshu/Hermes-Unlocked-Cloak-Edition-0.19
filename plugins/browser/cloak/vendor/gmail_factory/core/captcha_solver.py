"""
Captcha Solver - Unified interface for captcha solving services (2Captcha, Anti-Captcha, CapMonster)
"""
import time
import logging
import requests
from config.settings import Config

logger = logging.getLogger('gmail_creator_captcha')


class CaptchaSolver:
    @staticmethod
    def solve(site_key, page_url):
        if Config.TWOCAPTCHA_API_KEY:
            result = CaptchaSolver._solve_2captcha(site_key, page_url)
            if result:
                return result

        if Config.ANTICAPTCHA_API_KEY:
            result = CaptchaSolver._solve_anticaptcha(site_key, page_url)
            if result:
                return result

        if Config.CAPMONSTER_API_KEY:
            result = CaptchaSolver._solve_capmonster(site_key, page_url)
            if result:
                return result

        logger.warning("No captcha service available or all failed")
        return None

    @staticmethod
    def _solve_2captcha(site_key, page_url):
        try:
            submit_resp = requests.post("http://2captcha.com/in.php", data={
                "key": Config.TWOCAPTCHA_API_KEY,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": page_url,
                "json": 1,
            }, timeout=30)

            if submit_resp.status_code != 200:
                return None

            data = submit_resp.json()
            if data.get("status") != 1:
                return None

            captcha_id = data["request"]
            logger.info(f"2Captcha task submitted: {captcha_id}")
            time.sleep(20)

            for _ in range(30):
                result_resp = requests.get("http://2captcha.com/res.php", params={
                    "key": Config.TWOCAPTCHA_API_KEY,
                    "action": "get",
                    "id": captcha_id,
                    "json": 1,
                }, timeout=10)

                if result_resp.status_code == 200:
                    result = result_resp.json()
                    if result.get("status") == 1:
                        logger.info("2Captcha solved successfully")
                        return result["request"]
                    if "CAPCHA_NOT_READY" in str(result.get("request", "")):
                        time.sleep(5)
                        continue
                time.sleep(5)

        except Exception as e:
            logger.error(f"2Captcha error: {e}")
        return None

    @staticmethod
    def _solve_anticaptcha(site_key, page_url):
        try:
            create_resp = requests.post("https://api.anti-captcha.com/createTask", json={
                "clientKey": Config.ANTICAPTCHA_API_KEY,
                "task": {
                    "type": "RecaptchaV2TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                },
            }, timeout=30)

            if create_resp.status_code != 200:
                return None

            data = create_resp.json()
            if data.get("errorId") != 0:
                return None

            task_id = data["taskId"]
            logger.info(f"Anti-Captcha task submitted: {task_id}")
            time.sleep(20)

            for _ in range(30):
                result_resp = requests.post("https://api.anti-captcha.com/getTaskResult", json={
                    "clientKey": Config.ANTICAPTCHA_API_KEY,
                    "taskId": task_id,
                }, timeout=10)

                if result_resp.status_code == 200:
                    result = result_resp.json()
                    if result.get("status") == "ready":
                        token = result.get("solution", {}).get("gRecaptchaResponse")
                        if token:
                            logger.info("Anti-Captcha solved successfully")
                            return token
                    elif result.get("status") == "processing":
                        time.sleep(5)
                        continue
                time.sleep(5)

        except Exception as e:
            logger.error(f"Anti-Captcha error: {e}")
        return None

    @staticmethod
    def _solve_capmonster(site_key, page_url):
        try:
            create_resp = requests.post("https://api.capmonster.cloud/createTask", json={
                "clientKey": Config.CAPMONSTER_API_KEY,
                "task": {
                    "type": "RecaptchaV2TaskProxyless",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                },
            }, timeout=30)

            if create_resp.status_code != 200:
                return None

            data = create_resp.json()
            if data.get("errorId") != 0:
                return None

            task_id = data["taskId"]
            logger.info(f"CapMonster task submitted: {task_id}")
            time.sleep(15)

            for _ in range(30):
                result_resp = requests.post("https://api.capmonster.cloud/getTaskResult", json={
                    "clientKey": Config.CAPMONSTER_API_KEY,
                    "taskId": task_id,
                }, timeout=10)

                if result_resp.status_code == 200:
                    result = result_resp.json()
                    if result.get("status") == "ready":
                        token = result.get("solution", {}).get("gRecaptchaResponse")
                        if token:
                            logger.info("CapMonster solved successfully")
                            return token
                    elif result.get("status") == "processing":
                        time.sleep(5)
                        continue
                time.sleep(5)

        except Exception as e:
            logger.error(f"CapMonster error: {e}")
        return None
