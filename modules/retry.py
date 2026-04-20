import random


BACKOFF_BASE_DELAY = 1.0           # 基础延迟时间
BACKOFF_MAX_DELAY = 30.0           # 最大延迟时间

# 指数退避与随机抖动
def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter: base * 2^attempt + random(0, 1)."""
    delay = min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY)
    jitter = random.uniform(0, 1)   # 随机抖动
    return delay + jitter
