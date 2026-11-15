"""Durable state: a small document store on SQLite, plus a hash-chained audit log so every
agent action and human decision is tamper-evident."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS docs (kind TEXT, id TEXT, body TEXT, PRIMARY KEY (kind, id))")
            self.db.execute("CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, "
                            "actor TEXT, actor_type TEXT, action TEXT, ref TEXT, detail TEXT, prev TEXT, hash TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER)")
            self.db.commit()

    # -- documents ------------------------------------------------------------------------
    def put(self, kind: str, doc: dict) -> dict:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?)", (kind, doc["id"], json.dumps(doc, default=str)))
            self.db.commit()
        return doc

    def get(self, kind: str, id_: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute("SELECT body FROM docs WHERE kind = ? AND id = ?", (kind, id_)).fetchone()
        return json.loads(row[0]) if row else None

    def need(self, kind: str, id_: str) -> dict:
        doc = self.get(kind, id_)
        if doc is None:
            raise KeyError(f"{kind} {id_}")
        return doc

    def all(self, kind: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT body FROM docs WHERE kind = ? ORDER BY id", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def next_number(self, name: str, start: int) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
            value = (row[0] if row else start) + 1
            self.db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?)", (name, value))
            self.db.commit()
        return value

    def kv(self, key: str, value: Any = None) -> Any:
        if value is not None:
            self.put("kv", {"id": key, "value": value})
            return value
        doc = self.get("kv", key)
        return doc["value"] if doc else None

    # -- audit ----------------------------------------------------------------------------
    def audit(self, *, at: datetime, actor: str, actor_type: str, action: str, ref: str = "", detail: str = "") -> None:
        with self.lock:
            row = self.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else "0" * 64
            body = json.dumps([at.isoformat(), actor, actor_type, action, ref, detail, prev])
            h = hashlib.sha256(body.encode()).hexdigest()
            self.db.execute("INSERT INTO audit (at, actor, actor_type, action, ref, detail, prev, hash) VALUES (?,?,?,?,?,?,?,?)",
                            (at.isoformat(), actor, actor_type, action, ref, detail, prev, h))
            self.db.commit()

    def audit_log(self, ref: Optional[str] = None, limit: int = 300) -> list[dict]:
        q = "SELECT at, actor, actor_type, action, ref, detail FROM audit"
        args: tuple = ()
        if ref:
            q += " WHERE ref = ?"
            args = (ref,)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY seq DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(zip(("at", "actor", "actor_type", "action", "ref", "detail"), r)) for r in rows]

    def audit_verify(self) -> bool:
        prev = "0" * 64
        with self.lock:
            rows = self.db.execute("SELECT at, actor, actor_type, action, ref, detail, prev, hash FROM audit ORDER BY seq").fetchall()
        for at, actor, atype, action, ref, detail, p, h in rows:
            if p != prev or hashlib.sha256(json.dumps([at, actor, atype, action, ref, detail, p]).encode()).hexdigest() != h:
                return False
            prev = h
        return True
