from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEVICE_ENTITY,
                    DEVICE_MANAGE_ROLES, DEVICE_STATES, ENTITY, RECORD_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers,
                    device_stop_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_device_transition, validate_transition)


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
        return self._enrich_item(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
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
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "active_devices": self.repository.item_active_device_count(item_id),
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self._enrich_item(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_item(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self._enrich_item(item) for item in self.repository.list_items(status)]

    def _enrich_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = self.enrich(item)
        result["devices"] = self.repository.item_devices(int(item["id"]))
        return result

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    # ---- 治理设备联动 ----

    def create_device(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DEVICE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        code = payload.get("code")
        if code is not None:
            code = require_text(code, "code", 100)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        device = self.repository.create_device(name, code, external_ref, actor)
        self.repository.append_audit("device_create", DEVICE_ENTITY, device["id"], actor, {
            "name": name, "code": code, "status": device["status"],
        })
        return self.enrich_device(device)

    def list_devices(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in DEVICE_STATES:
            from .domain import ValidationError
            raise ValidationError("status不在允许范围内")
        return [self.enrich_device(device) for device in self.repository.list_devices(status)]

    def get_device(self, device_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_device(self.repository.get_device(device_id))

    def transition_device(self, device_id: int, payload: Dict[str, Any],
                          actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DEVICE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        target = payload.get("target")
        expected_version = payload.get("expected_version")
        if target not in DEVICE_STATES:
            from .domain import ValidationError
            raise ValidationError("target必须是in_use或stopped")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        device = self.repository.get_device(device_id)
        validate_device_transition(device["status"], target)
        expected_links: Optional[Dict[int, int]] = None
        if target == "in_use":
            expected_links = self._parse_link_versions(payload.get("expected_links"))
            links = self.repository.link_snapshot(device_id)
            current = {int(link["link_id"]): int(link["link_version"]) for link in links}
            if current != expected_links:
                raise ConflictError("设备关联关系已变更，请刷新后重试")
        else:
            blockers = device_stop_blockers(self.repository.link_snapshot(device_id))
            if blockers:
                raise ConflictError("；".join(blockers))
        updated = self.repository.transition_device(
            device_id, target, expected_version, expected_links)
        self.repository.append_audit(
            "device_stop" if target == "stopped" else "device_start",
            DEVICE_ENTITY, device_id, actor, {
                "from": device["status"], "to": target,
                "expected_version": expected_version,
                "expected_links": expected_links,
                "outfalls": [link["item_id"]
                             for link in self.repository.link_snapshot(device_id)],
            })
        return self.enrich_device(updated)

    def attach_outfall(self, device_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DEVICE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        item_id = payload.get("item_id")
        if not isinstance(item_id, int) or item_id < 1:
            raise ValueError("item_id必须是正整数")
        link = self.repository.attach_outfall(device_id, item_id, actor)
        self.repository.append_audit("device_attach", DEVICE_ENTITY, device_id, actor, {
            "link_id": link["id"], "item_id": item_id, "link_version": link["version"],
        })
        return self._link_view(link)

    def detach_outfall(self, device_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DEVICE_MANAGE_ROLES)
        actor = require_text(actor, "actor", 100)
        link_id = payload.get("link_id")
        expected_version = payload.get("expected_version")
        if not isinstance(link_id, int) or link_id < 1:
            raise ValueError("link_id必须是正整数")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        self.repository.get_device(device_id)
        link = self.repository.get_link(link_id)
        if int(link["device_id"]) != device_id or int(link["active"]) != 1:
            from .domain import NotFoundError
            raise NotFoundError("设备关联关系不存在")
        removed = self.repository.detach_outfall(link_id, expected_version)
        self.repository.append_audit("device_detach", DEVICE_ENTITY, device_id, actor, {
            "link_id": link_id, "item_id": int(link["item_id"]),
            "expected_version": expected_version,
        })
        return self._link_view(removed)

    @staticmethod
    def _parse_link_versions(value: Any) -> Dict[int, int]:
        if not isinstance(value, dict):
            raise ValueError("expected_links必须是{link_id: version}对象")
        result: Dict[int, int] = {}
        for raw_id, raw_version in value.items():
            try:
                link_id = int(raw_id)
                link_version = int(raw_version)
            except (TypeError, ValueError) as exc:
                raise ValueError("expected_links的键和值必须是正整数") from exc
            if link_id < 1 or link_version < 1:
                raise ValueError("expected_links的键和值必须是正整数")
            result[link_id] = link_version
        return result

    def _link_view(self, link: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "link_id": int(link["id"]), "device_id": int(link["device_id"]),
            "item_id": int(link["item_id"]), "active": bool(link["active"]),
            "version": int(link["version"]),
            "created_by": link["created_by"], "created_at": link["created_at"],
        }

    def enrich_device(self, device: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(device)
        links = self.repository.device_links(int(device["id"]))
        result["links"] = [
            {"link_id": int(link["id"]), "item_id": int(link["item_id"]),
             "version": int(link["version"])}
            for link in links
        ]
        result["outfalls"] = [int(link["item_id"]) for link in links]
        result["link_versions"] = {
            str(int(link["id"])): int(link["version"]) for link in links
        }
        return result

    def audit(self, role: str, item_id: Optional[int] = None,
              entity_type: Optional[str] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id, entity_type)

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
