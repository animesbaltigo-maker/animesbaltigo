import time
from config import ANTI_FLOOD_SECONDS

_LAST = {}


def allow_action(user_id: int, action: str) -> bool:
    now = time.time()
    key = (user_id, action)
    last = _LAST.get(key, 0.0)
    if now - last < ANTI_FLOOD_SECONDS:
        return False
    _LAST[key] = now
    return True
