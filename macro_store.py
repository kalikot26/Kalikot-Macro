#!/usr/bin/env python3
"""
macro_store.py - portable SQLite storage for the macro tool.

Everything lives in a single file, ``macros.db``, sitting next to this script,
so the whole tool stays portable: copy the folder (or just the .db) and your
macros + saved play settings come along.

Tables
------
macros   : one row per macro (events stored as JSON text)
configs  : per-macro play settings (loops / speed / loop delay / skip moves)
settings : app-wide key/value settings (theme, etc.)

On first use the DB is created and any legacy ``macros/*.json`` files are
imported automatically, so existing recordings are not lost.
"""

import json
import os
import sqlite3
import sys
import time

# Keep data next to the program so the tool stays portable. When frozen into a
# PyInstaller .exe, __file__ points at a temp unpack dir, so use the exe's
# folder instead; otherwise use this script's folder.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(BASE_DIR, "local.db")
LEGACY_JSON_DIR = os.path.join(BASE_DIR, "macros")

DEFAULT_CONFIG = {
    "loops": 1, "speed": 1.0, "loop_delay": 0.0, "skip_move": 0,
    "bg_mode": 0, "bg_lock": 0, "win_title": "",
}


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables if needed and import any legacy JSON macros once."""
    fresh = not os.path.exists(DB_PATH)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS macros (
                name            TEXT PRIMARY KEY,
                events          TEXT NOT NULL,
                recorded_events INTEGER NOT NULL DEFAULT 0,
                duration        REAL    NOT NULL DEFAULT 0,
                created_at      REAL,
                updated_at      REAL
            );
            CREATE TABLE IF NOT EXISTS macro_cfg (
                name TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
    if fresh:
        migrate_json_macros()


def _duration(events):
    last_t = events[-1]["t"] if events else 0.0
    waits = sum(e.get("d", 0.0) for e in events if e.get("type") == "wait")
    return last_t + waits


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------
def save_macro(name, events):
    now = time.time()
    payload = json.dumps(events)
    with _connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM macros WHERE name = ?", (name,)
        ).fetchone()
        created = existing["created_at"] if existing else now
        conn.execute(
            """INSERT INTO macros (name, events, recorded_events, duration,
                                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   events=excluded.events,
                   recorded_events=excluded.recorded_events,
                   duration=excluded.duration,
                   updated_at=excluded.updated_at""",
            (name, payload, len(events), _duration(events), created, now),
        )
    return name


def load_macro(name):
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, events, recorded_events, duration FROM macros WHERE name = ?",
            (name,),
        ).fetchone()
    if row is None:
        raise KeyError(f"no macro named {name!r}")
    return {
        "name": row["name"],
        "events": json.loads(row["events"]),
        "recorded_events": row["recorded_events"],
        "duration": row["duration"],
    }


def macro_exists(name):
    with _connect() as conn:
        return (
            conn.execute("SELECT 1 FROM macros WHERE name = ?", (name,)).fetchone()
            is not None
        )


def list_macros():
    """Return a list of (name, event_count, duration) tuples, sorted by name."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, recorded_events, duration FROM macros ORDER BY name"
        ).fetchall()
    return [(r["name"], r["recorded_events"], r["duration"]) for r in rows]


def macro_names():
    return [name for name, _c, _d in list_macros()]


def delete_macro(name):
    with _connect() as conn:
        conn.execute("DELETE FROM macros WHERE name = ?", (name,))
        conn.execute("DELETE FROM macro_cfg WHERE name = ?", (name,))


def rename_macro(old, new):
    with _connect() as conn:
        conn.execute("UPDATE macros SET name = ? WHERE name = ?", (new, old))
        conn.execute("UPDATE macro_cfg SET name = ? WHERE name = ?", (new, old))


# ---------------------------------------------------------------------------
# Per-macro play config (stored as a JSON blob so it's easy to extend)
# ---------------------------------------------------------------------------
def get_config(name):
    cfg = dict(DEFAULT_CONFIG)
    with _connect() as conn:
        row = conn.execute(
            "SELECT data FROM macro_cfg WHERE name = ?", (name,)
        ).fetchone()
    if row:
        try:
            cfg.update(json.loads(row["data"]))
        except Exception:
            pass
    return cfg


def set_config(name, cfg):
    """cfg is a dict; unknown keys are ignored, missing keys keep defaults."""
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    with _connect() as conn:
        conn.execute(
            """INSERT INTO macro_cfg (name, data) VALUES (?, ?)
               ON CONFLICT(name) DO UPDATE SET data=excluded.data""",
            (name, json.dumps(merged)),
        )


# ---------------------------------------------------------------------------
# App settings (key/value)
# ---------------------------------------------------------------------------
def get_setting(key, default=None):
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with _connect() as conn:
        conn.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )


# ---------------------------------------------------------------------------
# Legacy import
# ---------------------------------------------------------------------------
def migrate_json_macros(json_dir=LEGACY_JSON_DIR):
    """Import any macros/*.json files that aren't already in the DB."""
    if not os.path.isdir(json_dir):
        return 0
    imported = 0
    for fname in sorted(os.listdir(json_dir)):
        if not fname.endswith(".json"):
            continue
        name = fname[:-5]
        if macro_exists(name):
            continue
        try:
            with open(os.path.join(json_dir, fname), encoding="utf-8") as fh:
                data = json.load(fh)
            save_macro(name, data.get("events", []))
            imported += 1
        except Exception:
            pass
    return imported


# Make sure the DB exists as soon as the module is imported.
init_db()
