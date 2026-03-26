import json
import os
from threading import Lock

USERS_JSON_PATH = "data/users.json"

_lock = Lock()


def _ensure_parent_dir(filepath: str):
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _load_users() -> list[int]:
    if not os.path.exists(USERS_JSON_PATH):
        return []

    try:
        with open(USERS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, list):
            return []

        result = []
        for item in data:
            try:
                result.append(int(item))
            except Exception:
                pass
        return result
    except Exception:
        return []


def _save_users(users: list[int]):
    _ensure_parent_dir(USERS_JSON_PATH)
    with open(USERS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(set(users))), f, ensure_ascii=False, indent=2)


def register_user(user_id: int | None):
    if not user_id:
        return

    with _lock:
        users = _load_users()
        if user_id not in users:
            users.append(int(user_id))
            _save_users(users)


def remove_user(user_id: int | None):
    if not user_id:
        return

    with _lock:
        users = _load_users()
        users = [u for u in users if int(u) != int(user_id)]
        _save_users(users)


def get_all_users() -> list[int]:
    with _lock:
        return _load_users()


def get_total_users() -> int:
    return len(get_all_users())