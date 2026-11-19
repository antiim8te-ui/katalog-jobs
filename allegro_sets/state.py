"""SQLite: Verlauf der Käuferzahlen und Status jedes Kandidaten."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    offer_id TEXT, seller TEXT, buyers INTEGER, price REAL, taken_at TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    offer_id TEXT PRIMARY KEY, seller TEXT, url TEXT, title TEXT, price REAL,
    buyers INTEGER, ean TEXT, mpn TEXT, main_image TEXT,
    status TEXT DEFAULT 'neu',      -- neu, freigegeben, abgelehnt, in_base, erstellt, fehler, nicht_gefunden
    single_id TEXT, set_id TEXT, note TEXT, updated_at TEXT
);
"""


class State:
    def __init__(self, path: Path):
        # Mehrere Großhändler-Läufe können gleichzeitig laufen und teilen sich die Datenbank:
        # jede Änderung sofort speichern (keine langen Sperren) und bei Sperre bis zu 10 Minuten warten.
        self.db = sqlite3.connect(path, timeout=600, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def add_snapshot(self, o) -> None:
        self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)",
                        (o.offer_id, o.seller, o.buyers, o.price, self.now()))

    def upsert_candidate(self, o) -> None:
        self.db.execute(
            """INSERT INTO candidates (offer_id, seller, url, title, price, buyers, ean, mpn, main_image, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(offer_id) DO UPDATE SET buyers=excluded.buyers, price=excluded.price,
                 ean=COALESCE(excluded.ean, candidates.ean), mpn=COALESCE(excluded.mpn, candidates.mpn),
                 updated_at=excluded.updated_at""",
            (o.offer_id, o.seller, o.url, o.title, o.price, o.buyers, o.ean, o.mpn, o.main_image, self.now()),
        )

    def ean_done(self, ean: str) -> bool:
        r = self.db.execute("SELECT 1 FROM candidates WHERE ean=? AND status='erstellt'", (ean,)).fetchone()
        return r is not None

    def by_status(self, *statuses: str) -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.db.execute(
            f"SELECT * FROM candidates WHERE status IN ({q}) ORDER BY buyers DESC", statuses).fetchall()

    def set_status(self, offer_id: str, status: str, **extra) -> None:
        cols = ", ".join(f"{k}=?" for k in extra)
        sql = f"UPDATE candidates SET status=?, updated_at=?{', ' + cols if cols else ''} WHERE offer_id=?"
        self.db.execute(sql, (status, self.now(), *extra.values(), offer_id))

    def commit(self) -> None:
        self.db.commit()
