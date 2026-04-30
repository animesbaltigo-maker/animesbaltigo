from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import DATA_DIR

DB_PATH = DATA_DIR / "offline_subscriptions.sqlite3"

APPROVED_EVENTS = {
    "purchase_approved",
    "compra_aprovada",
    "payment_approved",
    "order_paid",
    "paid",
    "subscription_approved",
    "subscription_renewed",
}

CANCEL_EVENTS = {
    "purchase_refused",
    "compra_recusada",
    "payment_refused",
    "subscription_canceled",
    "subscription_cancelled",
    "canceled",
    "cancelled",
    "refunded",
    "refund",
    "chargeback",
}

PLAN_DAYS = {
    "bronze": 7,
    "semanal": 7,
    "ouro": 30,
    "mensal": 30,
    "diamante": 365,
    "anual": 365,
    "rubi": 36500,
    "vitalicio": 36500,
    "vitalício": 36500,
}


def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_subscriptions_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_intents (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                source TEXT NOT NULL DEFAULT 'offline',
                created_at INTEGER NOT NULL,
                used_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'active',
                plan_code TEXT NOT NULL,
                plan_name TEXT NOT NULL,
                starts_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                cakto_order_id TEXT,
                cakto_subscription_id TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                event_type TEXT,
                user_id INTEGER,
                token TEXT,
                payload_json TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def _dict(row):
    return dict(row) if row else None


def create_subscription_intent(user_id: int, username: str = "", full_name: str = "") -> dict:
    init_subscriptions_db()
    token = f"anime_{user_id}_{secrets.token_urlsafe(10)}"
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO subscription_intents (token, user_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token, int(user_id), username, full_name, now),
        )
        conn.commit()
    return {"token": token, "user_id": int(user_id)}


def get_intent(token: str) -> dict | None:
    init_subscriptions_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM subscription_intents WHERE token = ?",
            (str(token or "").strip(),),
        ).fetchone()
    return _dict(row)


def get_subscription(user_id: int) -> dict | None:
    init_subscriptions_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_subscriptions WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return _dict(row)


def get_active_subscription(user_id: int) -> dict | None:
    sub = get_subscription(user_id)
    if not sub or sub.get("status") != "active":
        return None
    if int(sub.get("expires_at") or 0) <= int(time.time()):
        return None
    return sub


def is_active_subscriber(user_id: int) -> bool:
    return get_active_subscription(user_id) is not None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _walk_values(payload: Any):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield str(key), value
            yield from _walk_values(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_values(value)


def extract_event_type(payload: dict) -> str:
    return _text(
        payload.get("event")
        or payload.get("type")
        or payload.get("event_type")
        or payload.get("event_name")
    ).lower()


def extract_token(payload: dict) -> str:
    for key, value in _walk_values(payload):
        if key.lower() in {"external_reference", "ref", "reference", "utm_content", "src", "sck"}:
            text = _text(value)
            match = re.search(r"anime_\d+_[A-Za-z0-9_-]+", text)
            if match:
                return match.group(0)
    blob = json.dumps(payload, ensure_ascii=False)
    match = re.search(r"anime_\d+_[A-Za-z0-9_-]+", blob)
    return match.group(0) if match else ""


def extract_user_id(payload: dict, token: str = "") -> int | None:
    intent = get_intent(token) if token else None
    if intent:
        return int(intent["user_id"])
    for key, value in _walk_values(payload):
        if key.lower() in {"telegram_user_id", "telegram_id", "tg_id", "user_id"}:
            match = re.search(r"\d{5,20}", _text(value))
            if match:
                return int(match.group(0))
    return None


def extract_plan(payload: dict) -> tuple[str, str, int]:
    names = []
    for key, value in _walk_values(payload):
        if key.lower() in {"name", "title", "plan", "plan_name", "product_name", "offer_name"}:
            text = _text(value)
            if text:
                names.append(text)
    blob = " ".join(names).lower()
    for code, days in PLAN_DAYS.items():
        if code in blob:
            return code, " ".join(names)[:120] or "BaltigoFlix", days
    return "mensal", "BaltigoFlix", 30


def _extract_order_ids(payload: dict) -> tuple[str, str]:
    order_id = ""
    subscription_id = ""
    for key, value in _walk_values(payload):
        key_l = key.lower()
        if not order_id and key_l in {"order_id", "orderid", "id"}:
            order_id = _text(value)
        if not subscription_id and key_l in {"subscription_id", "subscriptionid"}:
            subscription_id = _text(value)
    return order_id[:120], subscription_id[:120]


def activate_from_cakto(payload: dict) -> dict:
    init_subscriptions_db()
    token = extract_token(payload)
    user_id = extract_user_id(payload, token)
    if not user_id:
        raise ValueError("telegram_id_nao_encontrado")

    plan_code, plan_name, days = extract_plan(payload)
    order_id, subscription_id = _extract_order_ids(payload)
    now = int(time.time())
    current = get_active_subscription(user_id)
    base = max(now, int((current or {}).get("expires_at") or 0))
    expires_at = base + (days * 86400)
    event_id = _text(payload.get("event_id") or payload.get("id") or order_id or token)
    event_type = extract_event_type(payload)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_subscriptions (
                user_id, status, plan_code, plan_name, starts_at, expires_at,
                cakto_order_id, cakto_subscription_id, updated_at
            )
            VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                status = 'active',
                plan_code = excluded.plan_code,
                plan_name = excluded.plan_name,
                expires_at = excluded.expires_at,
                cakto_order_id = excluded.cakto_order_id,
                cakto_subscription_id = excluded.cakto_subscription_id,
                updated_at = excluded.updated_at
            """,
            (user_id, plan_code, plan_name, now, expires_at, order_id, subscription_id, now),
        )
        if token:
            conn.execute("UPDATE subscription_intents SET used_at = ? WHERE token = ?", (now, token))
        conn.execute(
            """
            INSERT OR IGNORE INTO subscription_events (
                event_id, event_type, user_id, token, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id or None, event_type, user_id, token, json.dumps(payload, ensure_ascii=False), now),
        )
        conn.commit()
    return get_subscription(user_id) or {}


def deactivate_from_cakto(payload: dict) -> dict | None:
    init_subscriptions_db()
    token = extract_token(payload)
    user_id = extract_user_id(payload, token)
    if not user_id:
        raise ValueError("telegram_id_nao_encontrado")
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "UPDATE user_subscriptions SET status = 'canceled', updated_at = ? WHERE user_id = ?",
            (now, user_id),
        )
        conn.commit()
    return get_subscription(user_id)
