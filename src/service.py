from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                    require_number, require_text, require_version)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEVICE_CREATE_ROLES, DEVICE_ENTITY,
                    DEVICE_TRANSITION_ROLES, ENTITY, LINK_ROLES, OUTLET_CREATE_ROLES,
                    OUTLET_ENTITY, OUTLET_RECORD_ENTITY, OUTLET_RECORD_ROLES,
                    OUTLET_TRANSITION_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_device_transition, validate_outlet_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            from .domain import ValidationError
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        require_version(expected_version)
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---------- 治理设备 ----------

    def create_device(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DEVICE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        description = payload.get("description", "")
        description = require_text(description, "description") if str(description).strip() else ""
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        device = self.repository.create_device(name, description, external_ref, actor)
        self.repository.append_audit("device_create", DEVICE_ENTITY, device["id"], actor, {
            "name": name, "status": device["status"], "version": device["version"],
        })
        return self.enrich_device(device)

    def get_device(self, device_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_device(self.repository.get_device(device_id))

    def list_devices(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status:
            self._ensure_device_status(status)
        return [self.enrich_device(d) for d in self.repository.list_devices(status)]

    def stop_device(self, device_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, set(DEVICE_TRANSITION_ROLES["stopped"]))
        actor = require_text(actor, "actor", 100)
        expected_version = require_version(payload.get("expected_version"))
        reason = payload.get("reason")
        if reason is not None:
            reason = require_text(reason, "reason")
        before = self.repository.get_device(device_id)
        validate_device_transition(before["status"], "stopped")
        outlets = [l["outlet_id"] for l in self.repository.list_device_links(device_id)]
        updated = self.repository.stop_device(device_id, expected_version, actor)
        self.repository.append_audit("device_stop", DEVICE_ENTITY, device_id, actor, {
            "from": before["status"], "to": updated["status"],
            "version": updated["version"], "outlet_ids": outlets,
            **({"reason": reason} if reason is not None else {}),
        })
        return self.enrich_device(updated)

    def start_device(self, device_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, set(DEVICE_TRANSITION_ROLES["running"]))
        actor = require_text(actor, "actor", 100)
        expected_version = require_version(payload.get("expected_version"))
        raw_versions = payload.get("link_versions", {})
        if not isinstance(raw_versions, dict):
            from .domain import ValidationError
            raise ValidationError("link_versions必须是{排放口ID: 关联版本}的对象")
        link_versions: Dict[int, int] = {}
        for key, value in raw_versions.items():
            try:
                outlet_id = int(key)
            except (TypeError, ValueError):
                from .domain import ValidationError
                raise ValidationError("link_versions的键必须是排放口ID")
            link_versions[outlet_id] = require_version(value, f"关联({outlet_id})版本")
        before = self.repository.get_device(device_id)
        validate_device_transition(before["status"], "running")
        updated = self.repository.start_device(
            device_id, expected_version, link_versions, actor)
        self.repository.append_audit("device_start", DEVICE_ENTITY, device_id, actor, {
            "from": before["status"], "to": updated["status"],
            "version": updated["version"], "link_versions": link_versions,
        })
        return self.enrich_device(updated)

    def link_device(self, device_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, LINK_ROLES)
        actor = require_text(actor, "actor", 100)
        outlet_id = require_version(payload.get("outlet_id"), "outlet_id")
        link = self.repository.link_device_outlet(device_id, outlet_id, actor)
        result = dict(link)
        result["link_version"] = link["version"]
        self.repository.append_audit("device_link", DEVICE_ENTITY, device_id, actor, {
            "link_id": link["id"], "outlet_id": outlet_id, "link_version": link["version"],
        })
        return result

    def unlink_device(self, device_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, LINK_ROLES)
        actor = require_text(actor, "actor", 100)
        outlet_id = require_version(payload.get("outlet_id"), "outlet_id")
        expected_version = require_version(payload.get("expected_version"))
        links = self.repository.list_device_links(device_id)
        if not any(l["outlet_id"] == outlet_id for l in links):
            from .domain import NotFoundError
            raise NotFoundError("设备与排放口的关联不存在")
        self.repository.unlink_device_outlet(device_id, outlet_id, expected_version, actor)
        self.repository.append_audit("device_unlink", DEVICE_ENTITY, device_id, actor, {
            "outlet_id": outlet_id, "link_version": expected_version,
        })
        return {"unlinked": True, "device_id": device_id, "outlet_id": outlet_id}

    # ---------- 排放口 ----------

    def create_outlet(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OUTLET_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        description = payload.get("description", "")
        description = require_text(description, "description") if str(description).strip() else ""
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        outlet = self.repository.create_outlet(name, description, external_ref, actor)
        self.repository.append_audit("outlet_create", OUTLET_ENTITY, outlet["id"], actor, {
            "name": name, "status": outlet["status"], "version": outlet["version"],
        })
        return self.enrich_outlet(outlet)

    def get_outlet(self, outlet_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_outlet(self.repository.get_outlet(outlet_id))

    def list_outlets(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status:
            self._ensure_outlet_status(status)
        return [self.enrich_outlet(o) for o in self.repository.list_outlets(status)]

    def transition_outlet(self, outlet_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        target = payload.get("target")
        expected_version = require_version(payload.get("expected_version"))
        before = self.repository.get_outlet(outlet_id)
        validate_outlet_transition(before["status"], target)
        ensure_role(role, set(OUTLET_TRANSITION_ROLES[target]))
        updated = self.repository.transition_outlet(
            outlet_id, target, expected_version, actor)
        self.repository.append_audit("outlet_transition", OUTLET_ENTITY, outlet_id, actor, {
            "from": before["status"], "to": target, "version": updated["version"],
        })
        return self.enrich_outlet(updated)

    def add_outlet_record(self, outlet_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OUTLET_RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            from .domain import ValidationError
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_outlet_record(
            outlet_id, kind, detail, status, external_ref, actor)
        self.repository.append_audit(
            "outlet_record", OUTLET_RECORD_ENTITY, record["id"], actor, {
                "outlet_id": outlet_id, "record_id": record["id"],
                "kind": kind, "status": status,
            })
        return record

    def close_outlet_record(self, outlet_id: int, record_id: int,
                            actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OUTLET_RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        record = self.repository.close_outlet_record(outlet_id, record_id, actor)
        self.repository.append_audit(
            "outlet_record_close", OUTLET_RECORD_ENTITY, record["id"], actor, {
                "outlet_id": outlet_id, "record_id": record["id"], "status": "closed",
            })
        return record

    def list_outlet_records(self, outlet_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_outlet_records(outlet_id)

    def device_change_log(self, device_id: int, role: str) -> list:
        """设备视角的完整变更记录：创建、启停、关联与解除。"""
        self._view(role)
        self.repository.get_device(device_id)
        return self.repository.list_audit(entity_type=DEVICE_ENTITY, entity_id=device_id)

    def outlet_change_log(self, outlet_id: int, role: str) -> list:
        """排放口视角的完整变更记录：自身事件 + 设备侧关联/启停事件。"""
        self._view(role)
        self.repository.get_outlet(outlet_id)
        events = self.repository.list_audit(entity_type=OUTLET_ENTITY,
                                            entity_id=outlet_id)
        linked = self.repository.list_audit(entity_type=DEVICE_ENTITY)

        def _touches_outlet(detail: Dict[str, Any]) -> bool:
            if detail.get("outlet_id") == outlet_id:
                return True
            ids = detail.get("outlet_ids") or []
            if outlet_id in ids:
                return True
            versions = detail.get("link_versions") or {}
            return outlet_id in versions or str(outlet_id) in versions

        linked = [e for e in linked if _touches_outlet(e["detail"])]
        return sorted(events + linked, key=lambda e: e["id"])

    @staticmethod
    def _ensure_device_status(status: str) -> None:
        from .domain import DEVICE_STATES, ValidationError
        if status not in DEVICE_STATES:
            raise ValidationError("设备状态不在允许范围内")

    @staticmethod
    def _ensure_outlet_status(status: str) -> None:
        from .domain import OUTLET_STATES, ValidationError
        if status not in OUTLET_STATES:
            raise ValidationError("排放口状态不在允许范围内")

    def enrich_device(self, device: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(device)
        links = self.repository.list_device_links(device["id"])
        result["outlets"] = links
        result["open_issue_outlet_ids"] = sorted({
            l["outlet_id"] for l in links
            if self.repository.open_outlet_issue_count(l["outlet_id"]) > 0
        })
        return result

    def enrich_outlet(self, outlet: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(outlet)
        links = self.repository.list_outlet_links(outlet["id"])
        result["devices"] = links
        result["running_device_count"] = sum(
            1 for l in links if l["device_status"] == "running")
        result["open_issue_count"] = self.repository.open_outlet_issue_count(outlet["id"])
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
