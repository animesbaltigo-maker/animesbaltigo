import json
from pathlib import Path
from threading import Lock

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

USERS_JSON_PATH = DATA_DIR / "users.json"
LEGACY_USERS_PATH = DATA_DIR / "users"

_lock = Lock()


def _ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_user_list(data) -> list[int]:
    if not isinstance(data, list):
        return []

    result = []
    seen = set()

    for item in data:
        try:
            user_id = int(item)
            if user_id > 0 and user_id not in seen:
                seen.add(user_id)
                result.append(user_id)
        except Exception:
            pass

    return result


def _read_json_file(path: Path) -> list[int]:
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return _normalize_user_list(data)
    except Exception:
        return []


def _load_users() -> list[int]:
    users_new = _read_json_file(USERS_JSON_PATH)
    users_legacy = _read_json_file(LEGACY_USERS_PATH)

    merged = []
    seen = set()

    for user_id in users_new + users_legacy:
        if user_id not in seen:
            seen.add(user_id)
            merged.append(user_id)

    return merged


def _save_users(users: list[int]):
    _ensure_data_dir()

    normalized = sorted(set(_normalize_user_list(users)))

    with open(USERS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)


def get_all_users() -> list[int]:
    with _lock:
        users = _load_users()

        # migra automaticamente pro users.json
        if users:
            _save_users(users)

        return users


def add_user(user_id: int):
    try:
        user_id = int(user_id)
    except Exception:
        return

    if user_id <= 0:
        return

    with _lock:
        users = _load_users()
        if user_id not in users:
            users.append(user_id)
            _save_users(users)


def remove_user(user_id: int):
    try:
        user_id = int(user_id)
    except Exception:
        return

    with _lock:
        users = _load_users()
        new_users = [u for u in users if u != user_id]

        if len(new_users) != len(users):
            _save_users(new_users)


# ✅ COMPATIBILIDADE COM CÓDIGO ANTIGO
def register_user(user_id: int):
    add_user(user_id)

def get_total_users() -> int:
    return len(get_all_users())
