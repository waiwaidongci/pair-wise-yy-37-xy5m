import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class DeviceLinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.repo = Repository(self.db_path)
        self.service = Service(self.repo)
        self.device = self.service.create_device(
            {"name": "RTO-1", "description": "蓄热式焚烧", "external_ref": "DEV-1"},
            "owner", "applicant")
        self.outlet_a = self.service.create_outlet(
            {"name": "DA001", "external_ref": "OUT-1"}, "owner", "applicant")
        self.outlet_b = self.service.create_outlet(
            {"name": "DA002", "external_ref": "OUT-2"}, "owner", "applicant")
        self.link_a = self.service.link_device(
            self.device["id"], {"outlet_id": self.outlet_a["id"]}, "owner", "applicant")
        self.link_b = self.service.link_device(
            self.device["id"], {"outlet_id": self.outlet_b["id"]}, "owner", "applicant")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_one_device_serves_multiple_outlets(self):
        detail = self.service.get_device(self.device["id"], "viewer")
        self.assertEqual({o["outlet_id"] for o in detail["outlets"]},
                         {self.outlet_a["id"], self.outlet_b["id"]})
        self.assertEqual(self.service.get_outlet(self.outlet_a["id"], "viewer")
                         ["running_device_count"], 1)

    def test_open_issue_blocks_stop(self):
        self.service.add_outlet_record(
            self.outlet_a["id"], {"kind": "现场检查", "detail": "超标"},
            "ins", "inspector")
        with self.assertRaises(ConflictError):
            self.service.stop_device(self.device["id"],
                                     {"expected_version": self.device["version"]},
                                     "owner", "applicant")
        # 关闭问题后可以停用
        records = self.service.list_outlet_records(self.outlet_a["id"], "viewer")
        self.service.close_outlet_record(
            self.outlet_a["id"], records[0]["id"], "ins", "inspector")
        stopped = self.service.stop_device(
            self.device["id"], {"expected_version": self.device["version"]},
            "owner", "applicant")
        self.assertEqual(stopped["status"], "stopped")

    def test_inspection_outlet_cannot_lose_sole_running_device(self):
        in_inspection = self.service.transition_outlet(
            self.outlet_a["id"],
            {"target": "inspection", "expected_version": self.outlet_a["version"]},
            "ins", "inspector")
        self.assertEqual(in_inspection["status"], "inspection")
        # 停用唯一在用设备被拒
        with self.assertRaises(ConflictError):
            self.service.stop_device(self.device["id"],
                                     {"expected_version": self.device["version"]},
                                     "owner", "applicant")
        # 解除关联同样被拒
        with self.assertRaises(ConflictError):
            self.service.unlink_device(
                self.device["id"],
                {"outlet_id": self.outlet_a["id"],
                 "expected_version": self.link_a["link_version"]},
                "owner", "applicant")
        # 补一台在用设备后即可停用
        device2 = self.service.create_device({"name": "RTO-2"}, "owner", "applicant")
        self.service.link_device(
            device2["id"], {"outlet_id": self.outlet_a["id"]}, "owner", "applicant")
        stopped = self.service.stop_device(
            self.device["id"], {"expected_version": self.device["version"]},
            "owner", "applicant")
        self.assertEqual(stopped["status"], "stopped")
        # 检查中的排放口仍有一台在用设备
        self.assertEqual(self.service.get_outlet(self.outlet_a["id"], "viewer")
                         ["running_device_count"], 1)

    def test_outlet_without_running_device_cannot_enter_inspection(self):
        # 先停用设备（此时两个排放口都在 active，可以停）
        stopped = self.service.stop_device(
            self.device["id"], {"expected_version": self.device["version"]},
            "owner", "applicant")
        with self.assertRaises(ConflictError):
            self.service.transition_outlet(
                self.outlet_b["id"], {"target": "inspection", "expected_version": 1},
                "ins", "inspector")
        # 恢复启用后才能进入检查
        self.service.start_device(
            stopped["id"],
            {"expected_version": stopped["version"],
             "link_versions": {self.outlet_a["id"]: self.link_a["link_version"],
                               self.outlet_b["id"]: self.link_b["link_version"]}},
            "owner", "applicant")
        entered = self.service.transition_outlet(
            self.outlet_b["id"], {"target": "inspection", "expected_version": 1},
            "ins", "inspector")
        self.assertEqual(entered["status"], "inspection")

    def test_stale_versions_conflict(self):
        # 设备版本过期
        with self.assertRaises(ConflictError):
            self.service.stop_device(self.device["id"], {"expected_version": 99},
                                     "owner", "applicant")
        self.service.stop_device(
            self.device["id"], {"expected_version": self.device["version"]},
            "owner", "applicant")
        # 恢复启用：设备版本过期
        with self.assertRaises(ConflictError):
            self.service.start_device(
                self.device["id"],
                {"expected_version": 99, "link_versions":
                 {self.outlet_a["id"]: self.link_a["link_version"],
                  self.outlet_b["id"]: self.link_b["link_version"]}},
                "owner", "applicant")
        # 关联版本过期
        with self.assertRaises(ConflictError):
            self.service.start_device(
                self.device["id"],
                {"expected_version": 2, "link_versions":
                 {self.outlet_a["id"]: 99, self.outlet_b["id"]: self.link_b["link_version"]}},
                "owner", "applicant")
        # 提交的关联集合与当前不一致（缺少一个排放口）
        with self.assertRaises(ConflictError):
            self.service.start_device(
                self.device["id"],
                {"expected_version": 2, "link_versions":
                 {self.outlet_a["id"]: self.link_a["link_version"]}},
                "owner", "applicant")
        # 排放口状态版本过期
        with self.assertRaises(ConflictError):
            self.service.transition_outlet(
                self.outlet_a["id"],
                {"target": "inspection", "expected_version": 99},
                "ins", "inspector")
        # 非法版本号返回校验错误
        with self.assertRaises(ValidationError):
            self.service.stop_device(self.device["id"], {"expected_version": "0"},
                                     "owner", "applicant")

    def test_restart_after_link_change_is_detected(self):
        stopped = self.service.stop_device(
            self.device["id"], {"expected_version": self.device["version"]},
            "owner", "applicant")
        # 停用期间新增第三个排放口关联，提交时的关联快照已过期
        outlet_c = self.service.create_outlet({"name": "DA003"}, "owner", "applicant")
        self.service.link_device(
            self.device["id"], {"outlet_id": outlet_c["id"]}, "owner", "applicant")
        with self.assertRaises(ConflictError):
            self.service.start_device(
                self.device["id"],
                {"expected_version": stopped["version"], "link_versions":
                 {self.outlet_a["id"]: self.link_a["link_version"],
                  self.outlet_b["id"]: self.link_b["link_version"]}},
                "owner", "applicant")

    def test_change_log_identifies_actor_from_both_sides(self):
        self.service.stop_device(
            self.device["id"], {"expected_version": 1, "reason": "年度检修"},
            "alice", "applicant")
        self.service.start_device(
            self.device["id"],
            {"expected_version": 2, "link_versions":
             {self.outlet_a["id"]: 1, self.outlet_b["id"]: 1}},
            "bob", "applicant")
        device_log = self.service.device_change_log(self.device["id"], "viewer")
        actions = [(e["action"], e["actor"]) for e in device_log]
        self.assertIn(("device_stop", "alice"), actions)
        self.assertIn(("device_start", "bob"), actions)
        # 排放口侧也能看到是谁停用/启用了关联设备、谁建立/解除了关联
        outlet_log = self.service.outlet_change_log(self.outlet_a["id"], "viewer")
        outlet_actions = [(e["action"], e["actor"]) for e in outlet_log]
        self.assertIn(("device_link", "owner"), outlet_actions)
        self.assertIn(("device_stop", "alice"), outlet_actions)
        self.assertIn(("device_start", "bob"), outlet_actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_device({"name": "x"}, "v", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.stop_device(self.device["id"], {"expected_version": 1},
                                     "v", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.transition_outlet(
                self.outlet_a["id"],
                {"target": "inspection", "expected_version": 1},
                "a", "applicant")
        # viewer 可以查看但不能登记问题
        with self.assertRaises(PermissionDenied):
            self.service.add_outlet_record(
                self.outlet_a["id"], {"kind": "现场检查", "detail": "x"},
                "v", "viewer")

    def test_duplicate_link_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.link_device(
                self.device["id"], {"outlet_id": self.outlet_a["id"]},
                "owner", "applicant")


class LegacyDatabaseMigrationTest(unittest.TestCase):
    """旧数据库只有 items/records/audit_events，启动后补齐新表，旧数据继续可用。"""

    def test_legacy_schema_is_upgraded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / "legacy.db")
        conn = sqlite3.connect(db_path)
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
        conn.execute("""INSERT INTO items(title, description, severity, quantity,
            threshold, status, version, external_ref, created_by, created_at, updated_at)
            VALUES('old permit','legacy','low',1,1,'draft',1,NULL,'legacy','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""")
        from src.audit import make_entry
        legacy_event = make_entry("create", "排污许可", 1, "legacy",
                                  {"title": "old permit"}, "GENESIS")
        conn.execute("""INSERT INTO audit_events(action, entity_type, entity_id, actor,
            detail, previous_hash, entry_hash, created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (legacy_event["action"], legacy_event["entity_type"],
             legacy_event["entity_id"], legacy_event["actor"],
             json.dumps(legacy_event["detail"], ensure_ascii=False),
             legacy_event["previous_hash"], legacy_event["entry_hash"],
             legacy_event["created_at"]))
        conn.commit()
        conn.close()

        repo = Repository(db_path)
        self.addCleanup(repo.close)
        service = Service(repo)
        # 旧许可数据仍可读取
        items = service.list_items("viewer")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "old permit")
        # 审计链未被破坏
        self.assertTrue(repo.verify_audit_chain())
        # 新表可用：完整走一遍联动
        device = service.create_device({"name": "new device"}, "owner", "applicant")
        outlet = service.create_outlet({"name": "new outlet"}, "owner", "applicant")
        service.link_device(device["id"], {"outlet_id": outlet["id"]},
                            "owner", "applicant")
        service.transition_outlet(
            outlet["id"], {"target": "inspection", "expected_version": 1},
            "ins", "inspector")
        with self.assertRaises(ConflictError):
            service.stop_device(device["id"], {"expected_version": 1},
                                "owner", "applicant")
        # 再次启动（幂等建表不报错、不丢数据）
        Repository(db_path).close()


if __name__ == "__main__":
    unittest.main()
