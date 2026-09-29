import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.audit import make_entry
from src.domain import ConflictError, PermissionDenied
from src.http_api import make_handler
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service

MGR = "compliance_manager"
INSP = "inspector"


def headers(actor="u1", role=MGR):
    return {"X-Actor": actor, "X-Role": role, "Content-Type": "application/json"}


def post(url, payload, actor="u1", role=MGR):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers(actor, role), method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class LinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.repo = Repository(self.db_path)
        self.service = Service(self.repo)
        self.o1 = self.service.create_item(
            {"title": "outfall-1", "description": "first stack",
             "severity": "high", "quantity": 5, "threshold": 10,
             "external_ref": "O-1"}, "applicant-user", "applicant")
        self.o2 = self.service.create_item(
            {"title": "outfall-2", "description": "second stack",
             "severity": "medium", "quantity": 1, "threshold": 10,
             "external_ref": "O-2"}, "applicant-user", "applicant")
        self.device = self.service.create_device(
            {"name": "scrubber-A", "code": "SA-01"}, "manager-user", MGR)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _submit_inspection(self, item):
        current = self.service.get_item(item["id"], "viewer")
        current = self.service.transition(
            current["id"], "submitted", current["version"], "applicant-user",
            TRANSITION_ROLES["submitted"][0])
        current = self.service.transition(
            current["id"], "inspection", current["version"], "inspector-user",
            TRANSITION_ROLES["inspection"][0])
        return current

    def test_one_device_serves_multiple_outfalls_and_views(self):
        l1 = self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        l2 = self.service.attach_outfall(
            self.device["id"], {"item_id": self.o2["id"]}, "manager-user", MGR)
        view = self.service.get_device(self.device["id"], "viewer")
        self.assertEqual(set(view["outfalls"]), {self.o1["id"], self.o2["id"]})
        self.assertEqual({int(k) for k in view["link_versions"]},
                         {l1["link_id"], l2["link_id"]})
        item_view = self.service.get_item(self.o1["id"], "viewer")
        self.assertEqual(item_view["devices"][0]["id"], self.device["id"])
        self.assertEqual(item_view["devices"][0]["status"], "in_use")

    def test_stop_blocked_by_open_records(self):
        self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        self.service.add_record(
            self.o1["id"], {"kind": "inspection", "detail": "open finding"},
            "inspector-user", INSP)
        with self.assertRaises(ConflictError):
            self.service.transition_device(
                self.device["id"], {"target": "stopped",
                                    "expected_version": self.device["version"]},
                "manager-user", MGR)

    def test_inspection_outfall_cannot_lose_sole_in_use_device(self):
        self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        self._submit_inspection(self.o1)
        with self.assertRaises(ConflictError):
            self.service.transition_device(
                self.device["id"], {"target": "stopped",
                                    "expected_version": self.device["version"]},
                "manager-user", MGR)
        # 第二台在用设备兜底后允许停用
        backup = self.service.create_device(
            {"name": "scrubber-B"}, "manager-user", MGR)
        self.service.attach_outfall(
            backup["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        stopped = self.service.transition_device(
            self.device["id"], {"target": "stopped",
                                "expected_version": self.device["version"]},
            "manager-user", MGR)
        self.assertEqual(stopped["status"], "stopped")

    def test_inspection_blocked_without_in_use_device(self):
        # 无关联设备的历史排放口不受约束
        self._submit_inspection(self.o2)
        # 关联了设备但设备全部停用 -> 禁止进入检查
        self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        current = self.service.get_item(self.o1["id"], "viewer")
        current = self.service.transition(
            current["id"], "submitted", current["version"], "applicant-user",
            "applicant")
        # 先制造"设备停用"：给一台备用在用设备让首台可停
        backup = self.service.create_device(
            {"name": "scrubber-B"}, "manager-user", MGR)
        self.service.attach_outfall(
            backup["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        self.service.transition_device(
            self.device["id"], {"target": "stopped",
                                "expected_version": self.device["version"]},
            "manager-user", MGR)
        self.service.transition_device(
            backup["id"], {"target": "stopped",
                           "expected_version": backup["version"]},
            "manager-user", MGR)
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], "inspection", current["version"],
                "inspector-user", INSP)

    def test_stale_device_version_conflicts(self):
        self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        payload = {"target": "stopped", "expected_version": self.device["version"]}
        first = self.service.transition_device(
            self.device["id"], payload, "manager-user", MGR)
        self.assertEqual(first["status"], "stopped")
        with self.assertRaises(ConflictError):
            self.service.transition_device(
                self.device["id"], payload, "manager-user", MGR)

    def test_restart_verifies_link_snapshot(self):
        link = self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        stopped = self.service.transition_device(
            self.device["id"], {"target": "stopped",
                                "expected_version": self.device["version"]},
            "manager-user", MGR)
        # 停用期间有人解除并重新建立关联 -> 关联版本前进
        self.service.detach_outfall(
            self.device["id"],
            {"link_id": link["link_id"], "expected_version": link["version"]},
            "manager-user", MGR)
        relink = self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        # 用停用提交时的旧关联快照恢复 -> 冲突
        with self.assertRaises(ConflictError):
            self.service.transition_device(
                self.device["id"],
                {"target": "in_use", "expected_version": stopped["version"],
                 "expected_links": {str(link["link_id"]): link["version"]}},
                "manager-user", MGR)
        # 用最新设备版本+最新关联版本快照才允许恢复
        started = self.service.transition_device(
            self.device["id"],
            {"target": "in_use", "expected_version": stopped["version"],
             "expected_links": {str(relink["link_id"]): relink["version"]}},
            "manager-user", MGR)
        self.assertEqual(started["status"], "in_use")

    def test_detach_version_conflict_and_inspection_guard(self):
        link = self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        with self.assertRaises(ConflictError):
            self.service.detach_outfall(
                self.device["id"],
                {"link_id": link["link_id"], "expected_version": 99},
                "manager-user", MGR)
        self._submit_inspection(self.o1)
        with self.assertRaises(ConflictError):
            self.service.detach_outfall(
                self.device["id"],
                {"link_id": link["link_id"], "expected_version": link["version"]},
                "manager-user", MGR)
        # 已停用的备用设备不贡献在用数量，解除其关联不应误拦
        backup = self.service.create_device(
            {"name": "scrubber-B"}, "manager-user", MGR)
        backup_link = self.service.attach_outfall(
            backup["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        self.service.transition_device(
            backup["id"], {"target": "stopped",
                           "expected_version": backup["version"]},
            "manager-user", MGR)
        detached = self.service.detach_outfall(
            backup["id"],
            {"link_id": backup_link["link_id"],
             "expected_version": backup_link["version"]},
            "manager-user", MGR)
        self.assertFalse(detached["active"])

    def test_permissions_and_audit_trail(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_device({"name": "x"}, "applicant-user", "applicant")
        link = self.service.attach_outfall(
            self.device["id"], {"item_id": self.o1["id"]}, "manager-user", MGR)
        self.service.transition_device(
            self.device["id"], {"target": "stopped",
                                "expected_version": self.device["version"]},
            "manager-user", MGR)
        self.service.transition_device(
            self.device["id"],
            {"target": "in_use", "expected_version": self.device["version"] + 1,
             "expected_links": {str(link["link_id"]): link["version"]}},
            "manager-user", MGR)
        self.assertTrue(self.repo.verify_audit_chain())
        device_events = self.service.audit(MGR, entity_type="治理设备")
        actions = [event["action"] for event in device_events]
        self.assertIn("device_create", actions)
        self.assertIn("device_attach", actions)
        self.assertIn("device_stop", actions)
        self.assertIn("device_start", actions)
        # 启停审计记录了操作人和提交版本
        stop_event = next(e for e in device_events if e["action"] == "device_stop")
        self.assertEqual(stop_event["actor"], "manager-user")
        self.assertEqual(stop_event["detail"]["expected_version"], 1)

    def test_legacy_database_is_upgraded_with_data_intact(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.executescript("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                description TEXT NOT NULL, severity TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0, threshold REAL NOT NULL DEFAULT 1,
                status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                external_ref TEXT, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
                kind TEXT NOT NULL, detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', external_ref TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
                actor TEXT NOT NULL, detail TEXT NOT NULL,
                previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL);
        """)
        conn.execute(
            """INSERT INTO items(title,description,severity,quantity,threshold,
               status,version,created_by,created_at,updated_at)
               VALUES('legacy','legacy permit','low',1,10,'draft',1,'old','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""")
        event = make_entry("create", "排污许可", 1, "old", {"legacy": True}, "GENESIS")
        conn.execute(
            """INSERT INTO audit_events(action,entity_type,entity_id,actor,detail,
               previous_hash,entry_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"],
             event["actor"], json.dumps(event["detail"], ensure_ascii=False),
             event["previous_hash"], event["entry_hash"], event["created_at"]))
        conn.commit()
        conn.close()

        migrated = Repository(str(legacy))
        svc = Service(migrated)
        items = svc.list_items("viewer")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "legacy")
        self.assertEqual(items[0]["devices"], [])
        device = svc.create_device({"name": "new-device"}, "manager-user", MGR)
        svc.attach_outfall(device["id"], {"item_id": items[0]["id"]},
                           "manager-user", MGR)
        self.assertTrue(migrated.verify_audit_chain())
        migrated.close()

    def test_http_returns_409_on_stale_version(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.service, str(Path("static").resolve())))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            status, _ = post(base + "/api/devices",
                             {"name": "http-device"}, "manager-user", MGR)
            self.assertEqual(status, 201)
            payload = {"target": "stopped", "expected_version": 1}
            status, body = post(base + "/api/devices/1/transition", payload)
            self.assertEqual(status, 200)
            status, body = post(base + "/api/devices/1/transition", payload)
            self.assertEqual(status, 409)
            self.assertEqual(body["error"], "ConflictError")
            status, body = post(base + "/api/devices",
                                {"name": "forbidden"}, "a", "viewer")
            self.assertEqual(status, 403)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
