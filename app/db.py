"""SQLite storage. One file, no ORM, so the app is easy to self-host."""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
    shop_id INTEGER PRIMARY KEY,
    user_id INTEGER,
    shop_name TEXT,
    access_token TEXT,
    refresh_token TEXT,
    expires_at REAL,
    last_synced_at REAL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS listings (
    listing_id INTEGER PRIMARY KEY,
    shop_id INTEGER,
    state TEXT,
    title TEXT,
    last_modified INTEGER,
    score INTEGER,
    data TEXT
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER,
    listing_id INTEGER,
    kind TEXT,
    summary TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS shop_meta (
    shop_id INTEGER,
    kind TEXT,
    data TEXT,
    PRIMARY KEY (shop_id, kind)
);
CREATE TABLE IF NOT EXISTS taxonomy (
    id INTEGER PRIMARY KEY,
    name TEXT,
    path TEXT,
    level INTEGER
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER,
    kind TEXT,
    source_listing_id INTEGER,
    status TEXT,
    notes TEXT,
    fields TEXT,
    etsy_listing_id INTEGER,
    error TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS draft_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER,
    path TEXT,
    media_type TEXT
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    verifier TEXT,
    created_at REAL
);
"""


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple = ()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    # Shops -----------------------------------------------------------------
    def save_shop(self, shop_id: int, user_id: int, shop_name: str, tokens: dict, data: dict) -> None:
        self.run(
            """INSERT INTO shops (shop_id, user_id, shop_name, access_token, refresh_token, expires_at, data)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(shop_id) DO UPDATE SET user_id=excluded.user_id, shop_name=excluded.shop_name,
                 access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                 expires_at=excluded.expires_at, data=excluded.data""",
            (
                shop_id,
                user_id,
                shop_name,
                tokens["access_token"],
                tokens.get("refresh_token"),
                time.time() + int(tokens.get("expires_in", 3600)),
                json.dumps(data),
            ),
        )

    def update_tokens(self, shop_id: int, tokens: dict) -> None:
        self.run(
            "UPDATE shops SET access_token=?, refresh_token=?, expires_at=? WHERE shop_id=?",
            (
                tokens["access_token"],
                tokens.get("refresh_token"),
                time.time() + int(tokens.get("expires_in", 3600)),
                shop_id,
            ),
        )

    def shop(self, shop_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM shops WHERE shop_id=?", (shop_id,))

    def shops(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM shops ORDER BY shop_name")

    # Meta ------------------------------------------------------------------
    def set_meta(self, shop_id: int, kind: str, data: Any) -> None:
        self.run(
            "INSERT OR REPLACE INTO shop_meta (shop_id, kind, data) VALUES (?, ?, ?)",
            (shop_id, kind, json.dumps(data)),
        )

    def meta(self, shop_id: int, kind: str) -> Any:
        row = self.one("SELECT data FROM shop_meta WHERE shop_id=? AND kind=?", (shop_id, kind))
        return json.loads(row["data"]) if row else []

    # Drafts ----------------------------------------------------------------
    def create_draft(self, shop_id: int, kind: str, notes: str, fields: dict, source_listing_id=None) -> int:
        now = time.time()
        return self.run(
            """INSERT INTO drafts (shop_id, kind, source_listing_id, status, notes, fields, created_at, updated_at)
               VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)""",
            (shop_id, kind, source_listing_id, notes, json.dumps(fields), now, now),
        )

    def draft(self, draft_id: int) -> dict | None:
        row = self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))
        if not row:
            return None
        d = dict(row)
        d["fields"] = json.loads(d["fields"] or "{}")
        d["images"] = [dict(r) for r in self.q("SELECT * FROM draft_images WHERE draft_id=? ORDER BY id", (draft_id,))]
        return d

    def update_draft(self, draft_id: int, **cols: Any) -> None:
        if "fields" in cols:
            cols["fields"] = json.dumps(cols["fields"])
        cols["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.run(f"UPDATE drafts SET {sets} WHERE id=?", (*cols.values(), draft_id))

    def add_draft_image(self, draft_id: int, path: str, media_type: str) -> None:
        self.run("INSERT INTO draft_images (draft_id, path, media_type) VALUES (?, ?, ?)", (draft_id, path, media_type))

    def add_change(self, shop_id: int, listing_id: int | None, kind: str, summary: str) -> None:
        self.run(
            "INSERT INTO changes (shop_id, listing_id, kind, summary, created_at) VALUES (?, ?, ?, ?, ?)",
            (shop_id, listing_id, kind, summary, time.time()),
        )
