"""
Proxy Manager - Advanced proxy rotation and health checking
"""
import os
import time
import random
import logging
import requests
from config.settings import Config
from core.redaction import redact_proxy_for_log, redact_runtime_value

logger = logging.getLogger('gmail_creator_proxy')


class ProxyManager:
    def __init__(self):
        self._proxies = []
        self._current_index = 0
        self._health = {}
        self._scores = {}
        self._load_proxies()

    def _load_proxies(self):
        proxy_file = Config.PROXY_FILE
        if not os.path.exists(proxy_file):
            logger.warning(f"Proxy file not found: {proxy_file}")
            return
        with open(proxy_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    self._proxies.append(line)
                    self._health[line] = True
                    self._scores[line] = 50
        if self._proxies:
            logger.info(f"Loaded {len(self._proxies)} proxies")

    @property
    def count(self):
        return len(self._proxies)

    @property
    def healthy_count(self):
        return sum(1 for p in self._proxies if self._health.get(p, True))

    def get_random(self):
        healthy = [p for p in self._proxies if self._health.get(p, True)]
        if not healthy:
            healthy = self._proxies
        if not healthy:
            return None
        return random.choice(healthy)

    def get_next(self):
        if not self._proxies:
            return None
        healthy = [p for p in self._proxies if self._health.get(p, True)]
        if not healthy:
            healthy = self._proxies
        proxy = healthy[self._current_index % len(healthy)]
        self._current_index += 1
        return proxy

    def get_best(self):
        if not self._proxies:
            return None
        healthy = [p for p in self._proxies if self._health.get(p, True)]
        if not healthy:
            healthy = self._proxies
        return max(healthy, key=lambda p: self._scores.get(p, 50))

    def mark_success(self, proxy):
        if proxy in self._scores:
            self._scores[proxy] = min(100, self._scores[proxy] + 10)
            self._health[proxy] = True

    def mark_failure(self, proxy, fatal=False):
        if proxy in self._scores:
            self._scores[proxy] = max(0, self._scores[proxy] - (30 if fatal else 10))
            if self._scores[proxy] <= 10:
                self._health[proxy] = False
                logger.warning(f"Proxy marked unhealthy: {redact_proxy_for_log(proxy)}")

    def check_health(self, proxy, timeout=10):
        parsed = self.parse(proxy)
        if not parsed:
            return False
        try:
            proxies_dict = {}
            if parsed["user"]:
                proxy_url = f"http://{parsed['user']}:{parsed['pass']}@{parsed['host']}:{parsed['port']}"
            else:
                proxy_url = f"http://{parsed['host']}:{parsed['port']}"
            proxies_dict = {"http": proxy_url, "https": proxy_url}
            resp = requests.get("https://httpbin.org/ip", proxies=proxies_dict, timeout=timeout)
            if resp.status_code == 200:
                self._health[proxy] = True
                return True
        except Exception as e:
            logger.debug(
                f"Proxy health check failed for {redact_proxy_for_log(proxy)}: "
                f"{redact_runtime_value(e)}"
            )
        self._health[proxy] = False
        return False

    def check_all_health(self):
        results = {"healthy": 0, "unhealthy": 0}
        for proxy in self._proxies:
            if self.check_health(proxy):
                results["healthy"] += 1
            else:
                results["unhealthy"] += 1
        return results

    def get_ip_info(self, proxy=None):
        try:
            proxies_dict = {}
            if proxy:
                parsed = self.parse(proxy)
                if parsed:
                    if parsed["user"]:
                        url = f"http://{parsed['user']}:{parsed['pass']}@{parsed['host']}:{parsed['port']}"
                    else:
                        url = f"http://{parsed['host']}:{parsed['port']}"
                    proxies_dict = {"http": url, "https": url}

            ip_resp = requests.get("https://api.ipify.org?format=json", proxies=proxies_dict, timeout=10)
            ip = ip_resp.json().get("ip", "Unknown")

            info_resp = requests.get(f"https://ipinfo.io/{ip}/json", timeout=10)
            info = info_resp.json()

            is_datacenter = "hosting" in str(info.get("org", "")).lower()
            return {
                "ip": ip,
                "city": info.get("city", "N/A"),
                "country": info.get("country", "N/A"),
                "org": info.get("org", "N/A"),
                "is_datacenter": is_datacenter,
            }
        except Exception as e:
            logger.warning(f"IP info check failed: {redact_runtime_value(e)}")
            return None

    def rotate_mobile_ip(self):
        url = getattr(Config, 'MOBILE_PROXY_IP_CHANGE_URL', '')
        if not url:
            return False
        try:
            logger.info("Rotating mobile proxy IP...")
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                wait_time = getattr(Config, 'PROXY_CHANGE_WAIT_TIME', 10)
                logger.info(f"IP changed. Waiting {wait_time}s for propagation...")
                time.sleep(wait_time)
                return True
            logger.warning(f"IP rotation returned status {resp.status_code}")
        except Exception as e:
            logger.error(f"Mobile IP rotation failed: {redact_runtime_value(e)}")
        return False

    @staticmethod
    def parse(proxy_string):
        if not proxy_string:
            return None
        parts = proxy_string.split(":")
        if len(parts) == 2:
            return {"host": parts[0], "port": parts[1], "user": None, "pass": None}
        elif len(parts) == 4:
            return {"host": parts[0], "port": parts[1], "user": parts[2], "pass": parts[3]}
        return None

    @staticmethod
    def format_for_playwright(proxy_string):
        parsed = ProxyManager.parse(proxy_string)
        if not parsed:
            return None
        result = {"server": f"http://{parsed['host']}:{parsed['port']}"}
        if parsed["user"]:
            result["username"] = parsed["user"]
            result["password"] = parsed["pass"]
        return result

    @staticmethod
    def format_for_selenium(proxy_string, proxy_type="http"):
        parsed = ProxyManager.parse(proxy_string)
        if not parsed:
            return None
        if parsed["user"]:
            return f"{proxy_type}://{parsed['user']}:{parsed['pass']}@{parsed['host']}:{parsed['port']}"
        return f"{parsed['host']}:{parsed['port']}"

    def get_stats(self):
        return {
            "total": len(self._proxies),
            "healthy": self.healthy_count,
            "unhealthy": len(self._proxies) - self.healthy_count,
            "scores": {p: self._scores.get(p, 0) for p in self._proxies[:10]},
        }


proxy_manager = ProxyManager()
