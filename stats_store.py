"""Part H option 2: log every transcription's confidences and retry decision
to a local SQLite database, and summarize them for GET /stats.

On App Service the file lives under /home, which is persistent storage shared
across restarts and redeploys; locally it goes to ./data/. Override with
STATS_DB_PATH (the tests use a temporary file).
"""
import json
import os
import sqlite3
from contextlib import closing
from collections import Counter
from datetime import datetime, timezone

ALERT_THRESHOLD = 0.80  # same threshold as the G7 low-stt-confidence alert

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcriptions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT    NOT NULL,
    endpoint           TEXT    NOT NULL,
    audio_format       TEXT,
    duration_seconds   REAL,
    primary_language   TEXT    NOT NULL,
    primary_confidence REAL    NOT NULL,
    retried            INTEGER NOT NULL,
    attempts_json      TEXT    NOT NULL,
    chosen_language    TEXT    NOT NULL,
    final_confidence   REAL    NOT NULL,
    improved           INTEGER NOT NULL,
    word_count         INTEGER NOT NULL,
    low_word_count     INTEGER NOT NULL,
    low_words_json     TEXT    NOT NULL,
    session_id         TEXT
)"""


class StatsStoreError(Exception):
    """SQLite failed. Logging must never break the pipeline."""


def db_path():
    if os.environ.get("STATS_DB_PATH"):
        return os.environ["STATS_DB_PATH"]
    if os.environ.get("WEBSITE_SITE_NAME"):  # running on App Service
        return "/home/data/confidence.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "confidence.db")


def _connect():
    path = db_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute(_SCHEMA)
        return conn
    except (OSError, sqlite3.Error) as e:
        raise StatsStoreError(f"SQLite unavailable at {path}: {e}") from e


def record(stt, endpoint="/process", audio_format=None, session_id=None):
    """Store one transcription. `stt` is transcribe_with_retry()'s result."""
    r = stt["retry"]
    row = (datetime.now(timezone.utc).isoformat(timespec="seconds"), endpoint,
           audio_format, stt.get("duration_seconds"),
           r["attempts"][0]["language"], r["primary_confidence"], int(r["retried"]),
           json.dumps(r["attempts"]), r["chosen_language"], stt["confidence"],
           int(r["improved"]), len(stt["words"]), len(stt["low_confidence_words"]),
           json.dumps(stt["low_confidence_words"]), session_id)
    try:
        with closing(_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO transcriptions (created_at, endpoint, audio_format, "
                "duration_seconds, primary_language, primary_confidence, retried, "
                "attempts_json, chosen_language, final_confidence, improved, "
                "word_count, low_word_count, low_words_json, session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
    except sqlite3.Error as e:
        raise StatsStoreError(str(e)) from e


def _avg(values):
    return round(sum(values) / len(values), 3) if values else None


def summary(recent=5):
    """Confidence summary over every logged transcription (GET /stats)."""
    try:
        with closing(_connect()) as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM transcriptions ORDER BY id")]
    except sqlite3.Error as e:
        raise StatsStoreError(str(e)) from e

    final = [r["final_confidence"] for r in rows]
    primary = [r["primary_confidence"] for r in rows]
    retried = [r for r in rows if r["retried"]]
    improved = [r for r in retried if r["improved"]]
    low_words = Counter()
    for r in rows:
        low_words.update(w["word"].lower() for w in json.loads(r["low_words_json"]))
    word_total = sum(r["word_count"] for r in rows)
    low_total = sum(r["low_word_count"] for r in rows)

    return {
        "storage": "sqlite",
        "transcriptions": len(rows),
        "first_at": rows[0]["created_at"] if rows else None,
        "last_at": rows[-1]["created_at"] if rows else None,
        "thresholds": {"word_flag": 0.70, "retry": 0.85, "alert": ALERT_THRESHOLD},
        "confidence": {
            "avg_final": _avg(final),
            "avg_primary": _avg(primary),
            "min_final": min(final) if final else None,
            "max_final": max(final) if final else None,
            "below_alert_threshold": sum(c < ALERT_THRESHOLD for c in final),
        },
        "retry": {
            "retried": len(retried),
            "retry_rate": round(len(retried) / len(rows), 3) if rows else None,
            "improved": len(improved),
            "avg_gain_when_improved": _avg(
                [r["final_confidence"] - r["primary_confidence"] for r in improved]),
            "chosen_language": dict(Counter(r["chosen_language"] for r in rows)),
        },
        "low_confidence_words": {
            "total": low_total,
            "share_of_words": round(low_total / word_total, 3) if word_total else None,
            "most_common": low_words.most_common(5),
        },
        "recent": [
            {k: r[k] for k in ("created_at", "endpoint", "audio_format",
                               "primary_confidence", "retried", "chosen_language",
                               "final_confidence", "low_word_count")}
            for r in rows[-recent:][::-1]
        ],
    }
