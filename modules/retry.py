import json
import random

from configs import BACKOFF_BASE_DELAY, BACKOFF_MAX_DELAY

# 指数退避与随机抖动
def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: base * 2^attempt + random(0, 1)."""
    delay = min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY)
    jitter = random.uniform(0, 1)   # 随机抖动
    return delay + jitter
