from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import DEVICE_STATES, ID_PREFIX, INSPECTION_STATE, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    code TEXT,
                    status TEXT NOT NULL CHECK(status IN ('in_use','stopped')),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_devices_external_ref
                    ON devices(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS device_outfalls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    removed_at TEXT,
                    UNIQUE(device_id, item_id, active)
                );
                CREATE INDEX IF NOT EXISTS ix_device_outfalls_item
                    ON device_outfalls(item_id);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if target == INSPECTION_STATE:
                counts = self.conn.execute(
                    """SELECT
                           SUM(CASE WHEN l.active=1 THEN 1 ELSE 0 END) AS link_count,
                           SUM(CASE WHEN l.active=1 AND d.status='in_use' THEN 1 ELSE 0 END) AS active_device_count
                       FROM device_outfalls l JOIN devices d ON d.id=l.device_id
                       WHERE l.item_id=?""",
                    (item_id,),
                ).fetchone()
                if int(counts["link_count"] or 0) > 0 and int(counts["active_device_count"] or 0) == 0:
                    raise ConflictError("排放口已关联治理设备但无在用设备，不能进入检查")
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_device(self, name: str, code: Optional[str],
                      external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO devices(name, code, status, version, external_ref,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (name, code, DEVICE_STATES[0], 1, external_ref, actor, now, now),
                )
                device_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_device(device_id)

    def get_device(self, device_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            raise NotFoundError("治理设备不存在")
        return dict(row)

    def list_devices(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM devices"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def link_snapshot(self, device_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.id AS link_id, l.version AS link_version, l.item_id,
                          i.status AS item_status,
                          (SELECT COUNT(*) FROM records r WHERE r.item_id=i.id AND r.status='open') AS open_count,
                          (SELECT COUNT(*) FROM device_outfalls l2 JOIN devices d2 ON d2.id=l2.device_id
                           WHERE l2.item_id=i.id AND l2.active=1 AND d2.status='in_use') AS active_device_count
                   FROM device_outfalls l JOIN items i ON i.id=l.item_id
                   WHERE l.device_id=? AND l.active=1""",
                (device_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def transition_device(self, device_id: int, target: str, expected_version: int,
                          expected_links: Optional[Dict[int, int]] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise NotFoundError("治理设备不存在")
            if int(row["version"]) != int(expected_version):
                raise ConflictError("设备版本冲突，请刷新后重试")
            if target == "in_use" and expected_links is not None:
                current = {
                    int(r["id"]): int(r["version"])
                    for r in self.conn.execute(
                        "SELECT id, version FROM device_outfalls WHERE device_id=? AND active=1",
                        (device_id,),
                    ).fetchall()
                }
                if current != expected_links:
                    raise ConflictError("设备关联关系已变更，请刷新后重试")
            cur = self.conn.execute(
                """UPDATE devices SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, device_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("设备版本冲突，请刷新后重试")
        return self.get_device(device_id)

    def attach_outfall(self, device_id: int, item_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_device(device_id)
        self.get_item(item_id)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id, active FROM device_outfalls WHERE device_id=? AND item_id=?",
                (device_id, item_id),
            ).fetchone()
            if row is not None and int(row["active"]) == 1:
                raise ConflictError("该排放口已关联此设备")
            if row is None:
                cur = self.conn.execute(
                    """INSERT INTO device_outfalls(device_id, item_id, active, version,
                       created_by, created_at) VALUES(?,?,1,1,?,?)""",
                    (device_id, item_id, actor, now),
                )
                link_id = int(cur.lastrowid)
            else:
                cur = self.conn.execute(
                    """UPDATE device_outfalls SET active=1, version=version+1
                       WHERE id=? AND active=0""",
                    (int(row["id"]),),
                )
                if cur.rowcount == 0:
                    raise ConflictError("该排放口已关联此设备")
                link_id = int(row["id"])
        return self.get_link(link_id)

    def detach_outfall(self, link_id: int, expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM device_outfalls WHERE id=?", (link_id,)
            ).fetchone()
            if row is None or int(row["active"]) == 0:
                raise NotFoundError("设备关联关系不存在")
            counts = self.conn.execute(
                """SELECT i.status AS item_status, d.status AS device_status,
                          (SELECT COUNT(*) FROM device_outfalls l2 JOIN devices d2 ON d2.id=l2.device_id
                           WHERE l2.item_id=i.id AND l2.active=1 AND d2.status='in_use') AS active_device_count
                   FROM items i JOIN devices d ON d.id=?
                   WHERE i.id=?""",
                (int(row["device_id"]), int(row["item_id"])),
            ).fetchone()
            remaining = int(counts["active_device_count"]) - (
                1 if counts["device_status"] == "in_use" else 0)
            if counts["item_status"] == INSPECTION_STATE and remaining <= 0:
                raise ConflictError("检查中的排放口不能失去唯一在用设备")
            cur = self.conn.execute(
                "UPDATE device_outfalls SET active=0, version=version+1, removed_at=? WHERE id=? AND version=? AND active=1",
                (now, link_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("关联关系版本冲突，请刷新后重试")
        removed = dict(row)
        removed["active"] = 0
        removed["version"] = int(row["version"]) + 1
        return removed

    def get_link(self, link_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM device_outfalls WHERE id=?", (link_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("设备关联关系不存在")
        return dict(row)

    def device_links(self, device_id: int, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM device_outfalls WHERE device_id=?"
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, (device_id,)).fetchall()
        return [dict(row) for row in rows]

    def item_devices(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT d.id, d.name, d.code, d.status, d.version,
                          l.id AS link_id, l.version AS link_version
                   FROM device_outfalls l JOIN devices d ON d.id=l.device_id
                   WHERE l.item_id=? AND l.active=1 ORDER BY d.id""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def item_active_device_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM device_outfalls l JOIN devices d ON d.id=l.device_id
                   WHERE l.item_id=? AND l.active=1 AND d.status='in_use'""",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        sql = "SELECT * FROM audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
