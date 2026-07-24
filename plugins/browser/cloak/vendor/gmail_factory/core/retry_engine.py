"""
Retry Engine - Smart retry with strategy rotation for account creation
"""
import logging
import random
import time

logger = logging.getLogger('gmail_creator_retry')


class CreationError:
    PHONE_REQUIRED = "phone_required"
    QR_BLOCKED = "qr_blocked"
    IP_FLAGGED = "ip_flagged"
    USERNAME_TAKEN = "username_taken"
    BROWSER_CRASH = "browser_crash"
    TIMEOUT = "timeout"
    CAPTCHA = "captcha"
    UNKNOWN = "unknown"


class RetryEngine:
    STRATEGIES = ["standard", "youtube", "workspace", "mobile_ua"]

    MAX_RETRIES = 3
    COOLDOWN_BASE = 15

    STRATEGY_MAP = {
        CreationError.QR_BLOCKED: ["youtube", "workspace", "mobile_ua"],
        CreationError.PHONE_REQUIRED: ["youtube", "mobile_ua", "workspace"],
        CreationError.IP_FLAGGED: ["standard", "youtube"],
        CreationError.USERNAME_TAKEN: None,
        CreationError.BROWSER_CRASH: ["standard"],
        CreationError.TIMEOUT: ["standard"],
        CreationError.CAPTCHA: ["youtube", "workspace"],
        CreationError.UNKNOWN: ["youtube", "standard"],
    }

    def __init__(self):
        self._attempt_history = []
        self._strategy_scores = {s: 50 for s in self.STRATEGIES}

    def should_retry(self, error_type, attempt_count):
        if attempt_count >= self.MAX_RETRIES:
            return False
        if error_type == CreationError.USERNAME_TAKEN:
            return True
        if error_type == CreationError.IP_FLAGGED and attempt_count >= 2:
            return False
        return True

    def get_next_strategy(self, failed_strategy, error_type):
        preferred = self.STRATEGY_MAP.get(error_type, self.STRATEGIES)
        if preferred is None:
            return failed_strategy

        candidates = [s for s in preferred if s != failed_strategy]
        if not candidates:
            candidates = [s for s in self.STRATEGIES if s != failed_strategy]
        if not candidates:
            return random.choice(self.STRATEGIES)

        return max(candidates, key=lambda s: self._strategy_scores.get(s, 50))

    def get_cooldown(self, attempt_count, error_type):
        base = self.COOLDOWN_BASE * (attempt_count + 1)
        if error_type == CreationError.IP_FLAGGED:
            base *= 3
        elif error_type == CreationError.QR_BLOCKED:
            base *= 2
        jitter = random.uniform(0.8, 1.5)
        return int(base * jitter)

    def record_attempt(self, strategy, success, error_type=None):
        self._attempt_history.append({
            "strategy": strategy,
            "success": success,
            "error_type": error_type,
            "timestamp": time.time(),
        })
        if success:
            self._strategy_scores[strategy] = min(100, self._strategy_scores[strategy] + 15)
        else:
            self._strategy_scores[strategy] = max(0, self._strategy_scores[strategy] - 10)

    def get_best_initial_strategy(self):
        return max(self.STRATEGIES, key=lambda s: self._strategy_scores.get(s, 50))

    def get_stats(self):
        total = len(self._attempt_history)
        successes = sum(1 for a in self._attempt_history if a["success"])
        failures = total - successes
        error_counts = {}
        for a in self._attempt_history:
            if a["error_type"]:
                error_counts[a["error_type"]] = error_counts.get(a["error_type"], 0) + 1

        return {
            "total_attempts": total,
            "successes": successes,
            "failures": failures,
            "success_rate": (successes / total * 100) if total > 0 else 0,
            "strategy_scores": dict(self._strategy_scores),
            "error_breakdown": error_counts,
        }

    def should_change_proxy(self, error_type):
        return error_type in (
            CreationError.IP_FLAGGED,
            CreationError.QR_BLOCKED,
        )


retry_engine = RetryEngine()
