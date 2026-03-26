import json
import os
import random

DATA_PATH = "data/bingo.json"

MIN_NUMBER = 1
MAX_NUMBER = 20
NUMBERS_PER_PLAYER = 6


def _ensure():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(DATA_PATH):
        _save({
            "players": {},
            "drawn": [],
            "active": False,
            "started": False,
            "winner": None,
            "notified": []
        })


def _load():
    _ensure()
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def generate_numbers():
    return sorted(random.sample(range(MIN_NUMBER, MAX_NUMBER + 1), NUMBERS_PER_PLAYER))


def register_player(uid, name):
    data = _load()

    if data["started"]:
        return None, "started"

    if str(uid) in data["players"]:
        return data["players"][str(uid)]["numbers"], "exists"

    nums = generate_numbers()

    data["players"][str(uid)] = {
        "name": name,
        "numbers": nums
    }

    _save(data)
    return nums, "ok"


def start_bingo():
    data = _load()
    if data["active"]:
        return False

    data["active"] = True
    data["started"] = True
    data["drawn"] = []
    data["winner"] = None
    data["notified"] = []

    _save(data)
    return True


def draw_number():
    data = _load()

    if not data["active"]:
        return None

    available = list(set(range(MIN_NUMBER, MAX_NUMBER + 1)) - set(data["drawn"]))
    if not available:
        return None

    n = random.choice(available)
    data["drawn"].append(n)
    data["drawn"].sort()

    _save(data)
    return n


def get_data():
    return _load()


def get_ranking():
    data = _load()
    ranking = []

    for uid, p in data["players"].items():
        hits = sum(1 for n in p["numbers"] if n in data["drawn"])
        ranking.append((p["name"], hits, p["numbers"]))

    ranking.sort(key=lambda x: (-x[1], x[0]))
    return ranking[:3]


def get_almost():
    data = _load()
    result = []

    for uid, p in data["players"].items():
        hits = sum(1 for n in p["numbers"] if n in data["drawn"])

        if hits == len(p["numbers"]) - 1 and uid not in data["notified"]:
            result.append((uid, p["name"]))
            data["notified"].append(uid)

    _save(data)
    return result


def check_winner():
    data = _load()

    for uid, p in data["players"].items():
        if all(n in data["drawn"] for n in p["numbers"]):
            data["winner"] = uid
            data["active"] = False
            _save(data)
            return p

    return None


def reset():
    _save({
        "players": {},
        "drawn": [],
        "active": False,
        "started": False,
        "winner": None,
        "notified": []
    })