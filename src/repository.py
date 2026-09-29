from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES
from .domain import DEVICE_STATES, OUTLET_STATES


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
        device_statuses = ",".join("'" + s + "'" for s in DEVICE_STATES)
        outlet_statuses = ",".join("'" + s + "'" for s in OUTLET_STATES)
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
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ({device_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_devices_external_ref
                    ON devices(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS outlets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ({outlet_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_outlets_external_ref
                    ON outlets(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS device_outlets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                    outlet_id INTEGER NOT NULL REFERENCES outlets(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(device_id, outlet_id)
                );
                CREATE INDEX IF NOT EXISTS ix_device_outlets_outlet
                    ON device_outlets(outlet_id);
                CREATE TABLE IF NOT EXISTS outlet_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    outlet_id INTEGER NOT NULL REFERENCES outlets(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(outlet_id, external_ref)
                );
                CREATE INDEX IF NOT EXISTS ix_outlet_records_outlet
                    ON outlet_records(outlet_id, status);
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

    # ---------- 治理设备 ----------

    def create_device(self, name: str, description: str, external_ref: Optional[str],
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO devices(name, description, status, version, external_ref,
                       created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (name, description, DEVICE_STATES[0], 1, external_ref, actor, now, now),
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
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def stop_device(self, device_id: int, expected_version: int, actor: str) -> Dict[str, Any]:
        del actor
        now = utc_now()
        with self._lock, self.conn:
            device = self.conn.execute(
                "SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if device is None:
                raise NotFoundError("治理设备不存在")
            if device["status"] != DEVICE_STATES[0]:
                raise ConflictError(f"设备不能从{device['status']}转换到{DEVICE_STATES[1]}")
            # 停用前确认关联排放口没有未关闭问题
            open_rows = self.conn.execute(
                """SELECT o.id AS id, o.name AS name FROM outlets o
                   JOIN device_outlets l ON l.outlet_id=o.id
                   WHERE l.device_id=? AND EXISTS(
                       SELECT 1 FROM outlet_records r
                       WHERE r.outlet_id=o.id AND r.status='open')
                   ORDER BY o.id""",
                (device_id,),
            ).fetchall()
            if open_rows:
                names = "、".join(f"{r['id']}:{r['name']}" for r in open_rows)
                raise ConflictError(f"关联排放口仍有未关闭问题，不能停用：{names}")
            # 检查中的排放口不能失去唯一在用设备
            sole_rows = self.conn.execute(
                """SELECT o.id AS id FROM outlets o
                   JOIN device_outlets l ON l.outlet_id=o.id
                   WHERE l.device_id=? AND o.status='inspection'
                     AND (SELECT COUNT(*) FROM device_outlets l2
                          JOIN devices d2 ON d2.id=l2.device_id
                          WHERE l2.outlet_id=o.id AND d2.status='running')=1""",
                (device_id,),
            ).fetchall()
            if sole_rows:
                ids = "、".join(str(r["id"]) for r in sole_rows)
                raise ConflictError(f"检查中的排放口将失去唯一在用设备，不能停用：{ids}")
            cur = self.conn.execute(
                """UPDATE devices SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (DEVICE_STATES[1], now, device_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_device(device_id)

    def start_device(self, device_id: int, expected_version: int,
                     link_versions: Dict[int, int], actor: str) -> Dict[str, Any]:
        del actor
        now = utc_now()
        with self._lock, self.conn:
            device = self.conn.execute(
                "SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if device is None:
                raise NotFoundError("治理设备不存在")
            if device["status"] != DEVICE_STATES[1]:
                raise ConflictError(f"设备不能从{device['status']}转换到{DEVICE_STATES[0]}")
            links = self.conn.execute(
                "SELECT outlet_id, version FROM device_outlets WHERE device_id=?",
                (device_id,),
            ).fetchall()
            current = {int(row["outlet_id"]): int(row["version"]) for row in links}
            submitted = {int(k): int(v) for k, v in (link_versions or {}).items()}
            # 恢复启用时核验设备和关联关系仍是提交时的版本
            if set(submitted) != set(current):
                raise ConflictError("关联排放口与提交时不一致（关联已新增或解除），请刷新后重试")
            stale = [oid for oid, ver in current.items() if submitted[oid] != ver]
            if stale:
                raise ConflictError(
                    f"关联关系版本已变更，请刷新后重试：{ '、'.join(map(str, sorted(stale)))}")
            cur = self.conn.execute(
                """UPDATE devices SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (DEVICE_STATES[0], now, device_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_device(device_id)

    # ---------- 排放口 ----------

    def create_outlet(self, name: str, description: str, external_ref: Optional[str],
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO outlets(name, description, status, version, external_ref,
                       created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (name, description, OUTLET_STATES[0], 1, external_ref, actor, now, now),
                )
                outlet_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_outlet(outlet_id)

    def get_outlet(self, outlet_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM outlets WHERE id=?", (outlet_id,)).fetchone()
        if row is None:
            raise NotFoundError("排放口不存在")
        return dict(row)

    def list_outlets(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM outlets"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def transition_outlet(self, outlet_id: int, target: str, expected_version: int,
                          actor: str) -> Dict[str, Any]:
        del actor
        now = utc_now()
        with self._lock, self.conn:
            outlet = self.conn.execute(
                "SELECT * FROM outlets WHERE id=?", (outlet_id,)).fetchone()
            if outlet is None:
                raise NotFoundError("排放口不存在")
            if target == OUTLET_STATES[1]:
                # 进入现场检查时必须至少有一台在用治理设备，设备停用后不能继续进入检查
                row = self.conn.execute(
                    """SELECT COUNT(*) AS n FROM device_outlets l
                       JOIN devices d ON d.id=l.device_id
                       WHERE l.outlet_id=? AND d.status='running'""",
                    (outlet_id,),
                ).fetchone()
                if int(row["n"]) == 0:
                    raise ConflictError("排放口没有在用治理设备，不能进入现场检查")
            cur = self.conn.execute(
                """UPDATE outlets SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, outlet_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_outlet(outlet_id)

    # ---------- 设备-排放口关联（多对多，关联自身带版本） ----------

    def link_device_outlet(self, device_id: int, outlet_id: int,
                           actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            if self.conn.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone() is None:
                raise NotFoundError("治理设备不存在")
            if self.conn.execute("SELECT 1 FROM outlets WHERE id=?", (outlet_id,)).fetchone() is None:
                raise NotFoundError("排放口不存在")
            try:
                cur = self.conn.execute(
                    """INSERT INTO device_outlets(device_id, outlet_id, version, created_by,
                       created_at, updated_at) VALUES(?,?,1,?,?,?)""",
                    (device_id, outlet_id, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该设备与排放口已关联") from exc
            link_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM device_outlets WHERE id=?", (link_id,)).fetchone()
        return dict(row)

    def unlink_device_outlet(self, device_id: int, outlet_id: int,
                             expected_version: int, actor: str) -> None:
        del actor
        with self._lock, self.conn:
            link = self.conn.execute(
                "SELECT * FROM device_outlets WHERE device_id=? AND outlet_id=?",
                (device_id, outlet_id),
            ).fetchone()
            if link is None:
                raise NotFoundError("设备与排放口的关联不存在")
            outlet = self.conn.execute(
                "SELECT * FROM outlets WHERE id=?", (outlet_id,)).fetchone()
            if outlet is not None and outlet["status"] == "inspection":
                row = self.conn.execute(
                    """SELECT COUNT(*) AS n FROM device_outlets l
                       JOIN devices d ON d.id=l.device_id
                       WHERE l.outlet_id=? AND l.device_id<>? AND d.status='running'""",
                    (outlet_id, device_id),
                ).fetchone()
                if int(row["n"]) == 0:
                    raise ConflictError("检查中的排放口不能失去唯一在用治理设备")
            cur = self.conn.execute(
                "DELETE FROM device_outlets WHERE id=? AND version=?",
                (link["id"], expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("关联版本冲突，请刷新后重试")

    def list_device_links(self, device_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.id, l.device_id, l.outlet_id, l.version AS link_version,
                          l.created_at, l.updated_at,
                          o.name AS outlet_name, o.status AS outlet_status,
                          o.version AS outlet_version
                   FROM device_outlets l JOIN outlets o ON o.id=l.outlet_id
                   WHERE l.device_id=? ORDER BY l.outlet_id""",
                (device_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_outlet_links(self, outlet_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT l.id, l.device_id, l.outlet_id, l.version AS link_version,
                          l.created_at, l.updated_at,
                          d.name AS device_name, d.status AS device_status,
                          d.version AS device_version
                   FROM device_outlets l JOIN devices d ON d.id=l.device_id
                   WHERE l.outlet_id=? ORDER BY l.device_id""",
                (outlet_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 排放口问题记录（现场检查/整改/检修） ----------

    def add_outlet_record(self, outlet_id: int, kind: str, detail: str, status: str,
                          external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_outlet(outlet_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO outlet_records(outlet_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (outlet_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM outlet_records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def close_outlet_record(self, outlet_id: int, record_id: int, actor: str) -> Dict[str, Any]:
        del actor
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE outlet_records SET status='closed'
                   WHERE id=? AND outlet_id=? AND status='open'""",
                (record_id, outlet_id),
            )
            if cur.rowcount == 0:
                row = self.conn.execute(
                    "SELECT 1 FROM outlet_records WHERE id=? AND outlet_id=?",
                    (record_id, outlet_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("排放口问题记录不存在")
                raise ConflictError("问题记录已关闭")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM outlet_records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_outlet_records(self, outlet_id: int) -> List[Dict[str, Any]]:
        self.get_outlet(outlet_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM outlet_records WHERE outlet_id=? ORDER BY id",
                (outlet_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def open_outlet_issue_count(self, outlet_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM outlet_records WHERE outlet_id=? AND status='open'",
                (outlet_id,),
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
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: List[Any] = []
        if entity_id is not None:
            sql += " AND entity_id=?"
            params.append(entity_id)
        if entity_type:
            sql += " AND entity_type=?"
            params.append(entity_type)
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
