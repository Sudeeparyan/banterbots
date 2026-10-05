"""Durable run history. Reopening records never regenerates model output."""
import json

import aiosqlite

from backend.config import RUNTIME


class Journal:
    async def open(self):
        self.db = await aiosqlite.connect(RUNTIME / "journal.sqlite")
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(session_id TEXT, seq INTEGER, event TEXT NOT NULL,
                PRIMARY KEY(session_id, seq));
        """)
        await self.db.commit()

    async def save(self, state):
        state = {**state, "events": []}  # Events have their own durable table.
        await self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)",
                              (state["id"], json.dumps(state)))
        await self.db.commit()

    async def append(self, event):
        await self.db.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?)",
                              (event.session_id, event.seq, event.model_dump_json()))
        await self.db.commit()

    async def get(self, sid):
        async with self.db.execute("SELECT state FROM sessions WHERE id=?", (sid,)) as cur:
            row = await cur.fetchone()
        return json.loads(row[0]) if row else None

    async def list(self):
        async with self.db.execute("SELECT state FROM sessions ORDER BY rowid DESC LIMIT 50") as cur:
            rows = await cur.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def events(self, sid, since=0):
        async with self.db.execute("SELECT event FROM events WHERE session_id=? AND seq>? ORDER BY seq",
                                   (sid, since)) as cur:
            rows = await cur.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def close(self):
        await self.db.close()
