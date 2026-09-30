"""Atomic, indexed comparisons: keep current membership plus change history."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .model import DataError, Member, Snapshot, username

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
  id INTEGER PRIMARY KEY, account TEXT NOT NULL, kind TEXT NOT NULL,
  subject TEXT NOT NULL, identity TEXT NOT NULL, captured_at TEXT NOT NULL,
  digest TEXT NOT NULL, member_count INTEGER NOT NULL,
  complete INTEGER NOT NULL DEFAULT 1, expected_count INTEGER,
  UNIQUE(account, kind, subject)
);
CREATE TABLE IF NOT EXISTS members (
  stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  member_key TEXT NOT NULL, username TEXT NOT NULL,
  PRIMARY KEY(stream_id, member_key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  captured_at TEXT NOT NULL, member_count INTEGER NOT NULL, source TEXT NOT NULL,
  added INTEGER NOT NULL, removed INTEGER NOT NULL, baseline INTEGER NOT NULL,
  complete INTEGER NOT NULL DEFAULT 1, expected_count INTEGER,
  UNIQUE(stream_id, captured_at)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  account TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
  direction TEXT NOT NULL, member_key TEXT NOT NULL, username TEXT NOT NULL,
  since TEXT NOT NULL, until TEXT NOT NULL, identity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_account_kind ON events(account, kind, direction, id DESC);
"""


@dataclass(frozen=True, slots=True)
class ImportResult:
    baselines: int = 0
    compared: int = 0
    unchanged_imports: int = 0
    added: int = 0
    removed: int = 0


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise DataError("База создана более новой версией приложения.")
            db.executescript(SCHEMA)
            if version < 2:
                for table in ("streams", "observations"):
                    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                    if "complete" not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN complete INTEGER NOT NULL DEFAULT 1")
                    if "expected_count" not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN expected_count INTEGER")
                    db.execute(f"UPDATE {table} SET expected_count=member_count")
                # Version 0.7.2 mixed old hidden IDs with new observations. Keep
                # history, but require a fresh collection before using that list.
                db.execute("""UPDATE streams SET complete=0,expected_count=NULL
                    WHERE kind='following' AND EXISTS (
                      SELECT 1 FROM observations o WHERE o.stream_id=streams.id
                      AND o.captured_at=streams.captured_at AND o.source LIKE '%local-reconciled%'
                    )""")
                db.execute("""UPDATE observations SET complete=0,expected_count=NULL
                    WHERE source LIKE '%local-reconciled%' AND stream_id IN
                    (SELECT id FROM streams WHERE kind='following')""")
            db.execute("PRAGMA user_version=2")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=NORMAL")
        try:
            yield db
        finally:
            db.close()

    def ingest(self, snapshot: Snapshot) -> ImportResult:
        counts = dict(baselines=0, compared=0, unchanged_imports=0, added=0, removed=0)
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TEMP TABLE incoming(member_key TEXT PRIMARY KEY, username TEXT NOT NULL) WITHOUT ROWID")
            for sample in snapshot.samples:
                # Sorting makes equivalent files idempotent regardless of their order.
                members = sorted(sample.members, key=lambda member: member.key)
                digest = hashlib.sha256()
                for member in members:
                    digest.update(f"{member.key}\0{member.username}\n".encode())
                checksum = digest.hexdigest()
                old = db.execute("SELECT * FROM streams WHERE account=? AND kind=? AND subject=?", (snapshot.account, sample.kind, sample.subject)).fetchone()
                if old:
                    coverage_matches = (
                        bool(old["complete"]) == sample.complete
                        and old["expected_count"] == sample.expected_count
                    )
                    if old["identity"] != sample.identity:
                        raise DataError("Тип идентификаторов изменился (имя / ID). Используйте один формат для всей истории этого списка.")
                    if snapshot.captured_at < old["captured_at"]:
                        raise DataError("Этот снимок старее последнего. Импортируйте снимки от старых к новым.")
                    if snapshot.captured_at == old["captured_at"]:
                        if checksum != old["digest"] or not coverage_matches:
                            raise DataError("На эту дату уже сохранён другой список. Проверьте дату снимка.")
                        counts["unchanged_imports"] += 1
                        continue
                    stream_id = old["id"]
                    if checksum == old["digest"] and coverage_matches:
                        # The observed list and its coverage are identical.
                        # Keep the new observation time without rewriting memberships.
                        db.execute("UPDATE streams SET captured_at=? WHERE id=?", (snapshot.captured_at, stream_id))
                        db.execute("INSERT INTO observations(stream_id,captured_at,member_count,source,added,removed,baseline,complete,expected_count) VALUES(?,?,?,?,0,0,0,?,?)", (stream_id, snapshot.captured_at, len(members), snapshot.source, int(sample.complete), sample.expected_count))
                        counts["compared"] += 1
                        continue
                else:
                    cursor = db.execute("INSERT INTO streams(account,kind,subject,identity,captured_at,digest,member_count,complete,expected_count) VALUES(?,?,?,?,?,?,?,?,?)", (snapshot.account, sample.kind, sample.subject, sample.identity, snapshot.captured_at, checksum, len(members), int(sample.complete), sample.expected_count))
                    stream_id = cursor.lastrowid
                db.execute("DELETE FROM incoming")
                db.executemany("INSERT INTO incoming VALUES(?,?)", ((m.key, m.username) for m in members))
                added = removed = 0
                if old and old["complete"] and sample.complete:
                    event_sql = """INSERT INTO events(stream_id,account,kind,subject,direction,member_key,username,since,until,identity)
                        SELECT ?,?,?,?,?,x.member_key,x.username,?,?,? FROM {left} x
                        WHERE {scope} NOT EXISTS (SELECT 1 FROM {right} y WHERE {other_scope} y.member_key=x.member_key)"""
                    args = (stream_id, snapshot.account, sample.kind, sample.subject)
                    tail = (old["captured_at"], snapshot.captured_at, sample.identity)
                    removed = db.execute(event_sql.format(left="members", right="incoming", scope="x.stream_id=? AND", other_scope=""), args + ("removed",) + tail + (stream_id,)).rowcount
                    added = db.execute(event_sql.format(left="incoming", right="members", scope="", other_scope="y.stream_id=? AND"), args + ("added",) + tail + (stream_id,)).rowcount
                    counts["compared"] += 1
                elif not old:
                    counts["baselines"] += 1
                else:
                    # Missing rows in a partial list are unknown, not removals.
                    counts["compared"] += 1
                db.execute("DELETE FROM members WHERE stream_id=?", (stream_id,))
                db.execute("INSERT INTO members SELECT ?,member_key,username FROM incoming", (stream_id,))
                db.execute("UPDATE streams SET captured_at=?,digest=?,member_count=?,complete=?,expected_count=? WHERE id=?", (snapshot.captured_at, checksum, len(members), int(sample.complete), sample.expected_count, stream_id))
                db.execute("INSERT INTO observations(stream_id,captured_at,member_count,source,added,removed,baseline,complete,expected_count) VALUES(?,?,?,?,?,?,?,?,?)", (stream_id, snapshot.captured_at, len(members), snapshot.source, added, removed, int(old is None), int(sample.complete), sample.expected_count))
                counts["added"] += added
                counts["removed"] += removed
        return ImportResult(**counts)

    def current_members(self, account: str, kind: str) -> tuple[Member, ...]:
        if kind not in {"followers", "following"}:
            raise DataError("Неизвестный тип списка.")
        account = username(account)
        with self.connect() as db:
            stream = db.execute(
                "SELECT id,identity FROM streams WHERE account=? AND kind=? AND subject=''",
                (account, kind),
            ).fetchone()
            if not stream:
                return ()
            rows = db.execute(
                "SELECT member_key,username FROM members WHERE stream_id=? ORDER BY member_key",
                (stream["id"],),
            )
            if stream["identity"] == "id":
                return tuple(Member(row["username"], row["member_key"]) for row in rows)
            return tuple(Member(row["username"]) for row in rows)

    def accounts(self) -> list[str]:
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT account FROM streams ORDER BY account")]

    def summary(self, account: str) -> dict:
        account = username(account)
        with self.connect() as db:
            streams = [dict(r) for r in db.execute("SELECT kind,subject,member_count,captured_at FROM streams WHERE account=?", (account,))]
            totals = {(r["kind"], r["direction"]): r["n"] for r in db.execute("SELECT kind,direction,COUNT(*) AS n FROM events WHERE account=? GROUP BY kind,direction", (account,))}
            latest_removed = db.execute("SELECT COUNT(*) FROM events e JOIN streams s ON s.id=e.stream_id WHERE e.account=? AND e.kind='followers' AND e.direction='removed' AND e.until=s.captured_at", (account,)).fetchone()[0]
            latest_added = db.execute("SELECT COUNT(*) FROM events e JOIN streams s ON s.id=e.stream_id WHERE e.account=? AND e.kind='followers' AND e.direction='added' AND e.until=s.captured_at", (account,)).fetchone()[0]
        reciprocal = self.nonreciprocal(account, limit=1)
        return {"followers": next((s["member_count"] for s in streams if s["kind"] == "followers"), None), "posts": sum(s["kind"] == "likes" for s in streams), "last_seen": max((s["captured_at"] for s in streams), default=None), "unfollowers": totals.get(("followers", "removed"), 0), "unlikes": totals.get(("likes", "removed"), 0), "new_followers": totals.get(("followers", "added"), 0), "latest_unfollowers": latest_removed, "latest_new_followers": latest_added, "nonreciprocal": reciprocal["total"], "nonreciprocal_complete": reciprocal["complete"], "following_hidden": reciprocal["hidden_count"]}

    def events(self, account: str, kind: str, direction: str, *, before: int | None = None, limit: int = 80, latest: bool = False) -> list[dict]:
        if kind not in {"followers", "likes"} or direction not in {"added", "removed"}:
            raise DataError("Неизвестный фильтр истории.")
        if not 1 <= limit <= 200:
            raise DataError("Размер страницы должен быть от 1 до 200.")
        sql = "SELECT * FROM events WHERE account=? AND kind=? AND direction=?"
        args = [username(account), kind, direction]
        if latest:
            sql += " AND until=(SELECT captured_at FROM streams WHERE id=events.stream_id)"
        if before is not None:
            sql += " AND id<?"
            args.append(before)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def nonreciprocal(self, account: str, *, after: str | None = None, limit: int = 80) -> dict:
        """Observed following minus complete followers from the same capture."""
        if not 1 <= limit <= 200:
            raise DataError("Размер страницы должен быть от 1 до 200.")
        with self.connect() as db:
            pair = {r["kind"]: r for r in db.execute("SELECT id,kind,identity,captured_at,complete,expected_count,member_count FROM streams WHERE account=? AND kind IN ('followers','following') AND subject=''", (username(account),))}
            missing = {"ready": False, "total": None, "captured_at": None, "users": [], "complete": False, "hidden_count": None}
            if set(pair) != {"followers", "following"}:
                return missing
            followers, following = pair["followers"], pair["following"]
            if followers["identity"] != following["identity"] or followers["captured_at"] != following["captured_at"]:
                return missing
            if not followers["complete"] or following["expected_count"] is None:
                return missing
            where = "FROM members f WHERE f.stream_id=? AND NOT EXISTS (SELECT 1 FROM members r WHERE r.stream_id=? AND r.member_key=f.member_key)"
            args = [following["id"], followers["id"]]
            total = db.execute("SELECT COUNT(*) " + where, args).fetchone()[0]
            if after is not None:
                where += " AND f.member_key>?"
                args.append(str(after))
            rows = db.execute("SELECT f.member_key,f.username " + where + " ORDER BY f.member_key LIMIT ?", args + [limit])
            return {"ready": True, "total": total, "captured_at": following["captured_at"], "users": [dict(row) for row in rows], "complete": bool(following["complete"]), "hidden_count": following["expected_count"] - following["member_count"]}

    def observations(self, account: str, limit: int = 80) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT o.*,s.kind,s.subject FROM observations o JOIN streams s ON s.id=o.stream_id WHERE s.account=? ORDER BY o.id DESC LIMIT ?", (username(account), min(max(limit, 1), 200)))]
