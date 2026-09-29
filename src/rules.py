from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
DEVICE_ENTITY='治理设备'; DEVICE_STATES=['in_use','stopped']; DEVICE_TRANSITIONS={'in_use':['stopped'],'stopped':['in_use']}; DEVICE_MANAGE_ROLES=set(['compliance_manager']); INSPECTION_STATE='inspection'
def validate_device_transition(current,target):
    if current not in DEVICE_STATES or target not in DEVICE_STATES: raise ValidationError("未知设备状态")
    if target not in DEVICE_TRANSITIONS.get(current,[]): raise ConflictError(f"设备不能从{current}转换到{target}")
def device_stop_blockers(links):
    blockers=[]
    for link in links:
        if int(link['open_count'])>0: blockers.append(f"排放口#{link['item_id']}存在未关闭问题，不能停用设备")
        if link['item_status']==INSPECTION_STATE and int(link['active_device_count'])<=1:
            blockers.append(f"排放口#{link['item_id']}正在检查中，该设备是其唯一在用设备，不能停用")
    return blockers
def inspection_device_blockers(link_count,active_device_count):
    return ["排放口已关联治理设备但无在用设备，不能进入检查"] if int(link_count)>0 and int(active_device_count)==0 else []
def verify_link_snapshot(links,expected_links):
    current={int(link['link_id']):int(link['link_version']) for link in links}
    if current!=expected_links: raise ConflictError("设备关联关系已变更，请刷新后重试")
    return True
