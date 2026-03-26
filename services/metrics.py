import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "metrics.sqlite3")


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_str() -> str:
    return _utc_now_dt().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_metrics_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metric_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                user_id TEXT,
                username TEXT,
                anime_id TEXT,
                anime_title TEXT,
                episode TEXT,
                query_text TEXT,
                result_count INTEGER,
                extra TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metric_events_type
            ON metric_events(event_type)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metric_events_user
            ON metric_events(user_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metric_events_created_at
            ON metric_events(created_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metric_events_anime
            ON metric_events(anime_id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS watched_episodes (
                user_id TEXT NOT NULL,
                anime_id TEXT NOT NULL,
                anime_title TEXT,
                episode INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, anime_id, episode)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_watched_episodes_user
            ON watched_episodes(user_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_watched_episodes_anime
            ON watched_episodes(anime_id)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_watched_episodes_updated_at
            ON watched_episodes(updated_at)
        """)


def log_event(
    event_type: str,
    user_id: int | str | None = None,
    username: str | None = None,
    anime_id: str | None = None,
    anime_title: str | None = None,
    episode: str | int | None = None,
    query_text: str | None = None,
    result_count: int | None = None,
    extra: str | None = None,
):
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO metric_events (
                event_type,
                user_id,
                username,
                anime_id,
                anime_title,
                episode,
                query_text,
                result_count,
                extra,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(event_type or "").strip(),
            str(user_id) if user_id is not None else None,
            (username or "").strip(),
            (anime_id or "").strip(),
            (anime_title or "").strip(),
            str(episode).strip() if episode is not None else "",
            (query_text or "").strip(),
            result_count,
            (extra or "").strip(),
            _utc_now_str(),
        ))


def mark_user_seen(user_id: int | str, username: str | None = None):
    user_id = str(user_id).strip()
    username = (username or "").strip()

    with _get_conn() as conn:
        exists = conn.execute("""
            SELECT 1
            FROM metric_events
            WHERE event_type = 'new_user' AND user_id = ?
            LIMIT 1
        """, (user_id,)).fetchone()

        if not exists:
            conn.execute("""
                INSERT INTO metric_events (
                    event_type, user_id, username, created_at
                )
                VALUES (?, ?, ?, ?)
            """, ("new_user", user_id, username, _utc_now_str()))

        conn.execute("""
            INSERT INTO metric_events (
                event_type, user_id, username, created_at
            )
            VALUES (?, ?, ?, ?)
        """, ("active_user", user_id, username, _utc_now_str()))


def mark_episode_watched(
    user_id: int | str,
    anime_id: str,
    episode: int | str,
    anime_title: str | None = None,
    username: str | None = None,
):
    user_id = str(user_id).strip()
    anime_id = str(anime_id).strip()
    anime_title = (anime_title or "").strip()
    username = (username or "").strip()

    try:
        episode_int = int(str(episode).strip())
    except Exception as e:
        raise ValueError(f"episode inválido para marcar como visto: {episode!r}") from e

    now = _utc_now_str()

    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO watched_episodes (
                user_id,
                anime_id,
                anime_title,
                episode,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, anime_id, episode)
            DO UPDATE SET
                anime_title = excluded.anime_title,
                updated_at = excluded.updated_at
        """, (
            user_id,
            anime_id,
            anime_title,
            episode_int,
            now,
            now,
        ))

    log_event(
        event_type="episode_mark_watched",
        user_id=user_id,
        username=username,
        anime_id=anime_id,
        anime_title=anime_title,
        episode=str(episode_int),
    )


def unmark_episode_watched(
    user_id: int | str,
    anime_id: str,
    episode: int | str,
    anime_title: str | None = None,
    username: str | None = None,
):
    user_id = str(user_id).strip()
    anime_id = str(anime_id).strip()
    anime_title = (anime_title or "").strip()
    username = (username or "").strip()

    try:
        episode_int = int(str(episode).strip())
    except Exception as e:
        raise ValueError(f"episode inválido para desmarcar: {episode!r}") from e

    with _get_conn() as conn:
        conn.execute("""
            DELETE FROM watched_episodes
            WHERE user_id = ? AND anime_id = ? AND episode = ?
        """, (user_id, anime_id, episode_int))

    log_event(
        event_type="episode_unmark_watched",
        user_id=user_id,
        username=username,
        anime_id=anime_id,
        anime_title=anime_title,
        episode=str(episode_int),
    )


def is_episode_watched(
    user_id: int | str,
    anime_id: str,
    episode: int | str,
) -> bool:
    user_id = str(user_id).strip()
    anime_id = str(anime_id).strip()

    try:
        episode_int = int(str(episode).strip())
    except Exception:
        return False

    with _get_conn() as conn:
        row = conn.execute("""
            SELECT 1
            FROM watched_episodes
            WHERE user_id = ? AND anime_id = ? AND episode = ?
            LIMIT 1
        """, (user_id, anime_id, episode_int)).fetchone()

    return row is not None


def get_last_watched_episode(
    user_id: int | str,
    anime_id: str,
) -> int | None:
    user_id = str(user_id).strip()
    anime_id = str(anime_id).strip()

    with _get_conn() as conn:
        row = conn.execute("""
            SELECT episode
            FROM watched_episodes
            WHERE user_id = ? AND anime_id = ?
            ORDER BY episode DESC
            LIMIT 1
        """, (user_id, anime_id)).fetchone()

    if not row:
        return None

    return int(row["episode"])


def get_watched_episodes(
    user_id: int | str,
    anime_id: str,
) -> list[int]:
    user_id = str(user_id).strip()
    anime_id = str(anime_id).strip()

    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT episode
            FROM watched_episodes
            WHERE user_id = ? AND anime_id = ?
            ORDER BY episode ASC
        """, (user_id, anime_id)).fetchall()

    return [int(row["episode"]) for row in rows]


def count_watched_episodes(
    user_id: int | str,
    anime_id: str,
) -> int:
    user_id = str(user_id).strip()
    anime_id = str(anime_id).strip()

    with _get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS total
            FROM watched_episodes
            WHERE user_id = ? AND anime_id = ?
        """, (user_id, anime_id)).fetchone()

    return int(row["total"] if row else 0)


def get_recently_watched(
    user_id: int | str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    user_id = str(user_id).strip()
    limit = max(1, int(limit))

    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT
                anime_id,
                anime_title,
                episode,
                updated_at
            FROM watched_episodes
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()

    return [
        {
            "anime_id": row["anime_id"],
            "anime_title": row["anime_title"] or "",
            "episode": int(row["episode"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _range_start(period: str | None) -> str | None:
    period = (period or "total").lower().strip()
    now = _utc_now_dt()

    if period == "total":
        return None

    if period == "30d":
        return (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    if period == "7d":
        return (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    if period == "hoje":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%d %H:%M:%S")

    return None


def _top_rows(
    event_type: str,
    field_expr: str,
    limit: int = 10,
    period: str = "total",
):
    since = _range_start(period)

    sql = f"""
        SELECT
            {field_expr} AS label,
            COUNT(*) AS total
        FROM metric_events
        WHERE event_type = ?
          AND COALESCE({field_expr}, '') <> ''
    """
    params = [event_type]

    if since:
        sql += " AND created_at >= ?"
        params.append(since)

    sql += f"""
        GROUP BY {field_expr}
        ORDER BY total DESC, {field_expr} ASC
        LIMIT ?
    """
    params.append(limit)

    with _get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def _count(event_type: str, period: str = "total") -> int:
    since = _range_start(period)

    sql = """
        SELECT COUNT(*) AS total
        FROM metric_events
        WHERE event_type = ?
    """
    params = [event_type]

    if since:
        sql += " AND created_at >= ?"
        params.append(since)

    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()

    return int(row["total"] if row else 0)


def _count_distinct_users(event_type: str, period: str = "total") -> int:
    since = _range_start(period)

    sql = """
        SELECT COUNT(DISTINCT user_id) AS total
        FROM metric_events
        WHERE event_type = ?
          AND COALESCE(user_id, '') <> ''
    """
    params = [event_type]

    if since:
        sql += " AND created_at >= ?"
        params.append(since)

    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()

    return int(row["total"] if row else 0)


def _top_watched_animes(limit: int = 10):
    with _get_conn() as conn:
        return conn.execute("""
            SELECT
                COALESCE(NULLIF(anime_title, ''), anime_id) AS label,
                COUNT(*) AS total
            FROM watched_episodes
            GROUP BY anime_id, anime_title
            ORDER BY total DESC, label ASC
            LIMIT ?
        """, (limit,)).fetchall()


def _count_total_watched_marks() -> int:
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS total
            FROM watched_episodes
        """).fetchone()

    return int(row["total"] if row else 0)


def _count_distinct_watchers() -> int:
    with _get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(DISTINCT user_id) AS total
            FROM watched_episodes
        """).fetchone()

    return int(row["total"] if row else 0)


def clear_metrics():
    with _get_conn() as conn:
        conn.execute("DELETE FROM metric_events")


def clear_watched_history():
    with _get_conn() as conn:
        conn.execute("DELETE FROM watched_episodes")


def clear_all_metrics_data():
    with _get_conn() as conn:
        conn.execute("DELETE FROM metric_events")
        conn.execute("DELETE FROM watched_episodes")


def get_metrics_report(limit: int = 7, period: str = "total") -> dict:
    return {
        "period": period,
        "top_searches": _top_rows("search", "query_text", limit, period),
        "top_opened_animes": _top_rows("anime_open", "anime_title", limit, period),
        "top_watch_clicks": _top_rows("watch_click", "anime_title", limit, period),
        "top_episodes": _top_rows(
            "episode_open",
            "anime_title || ' - EP ' || episode",
            limit,
            period,
        ),
        "top_marked_watched": _top_rows(
            "episode_mark_watched",
            "anime_title || ' - EP ' || episode",
            limit,
            period,
        ),
        "searches_without_result": _count("search_no_result", period),
        "new_users": _count_distinct_users("new_user", period),
        "active_users": _count_distinct_users("active_user", period),
        "watched_marks_total": _count_total_watched_marks(),
        "distinct_watchers": _count_distinct_watchers(),
        "top_watched_animes": _top_watched_animes(limit),
    }