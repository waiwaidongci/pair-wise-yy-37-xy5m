from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='空气污染源许可与合规检查'; ENTITY='排污许可'; ID_PREFIX='AQ'
SEVERITIES=['low', 'medium', 'high', 'critical']; STATES=['draft', 'submitted', 'inspection', 'correction', 'approved']; TRANSITIONS={'draft': ['submitted'], 'submitted': ['inspection'], 'inspection': ['correction'], 'correction': ['approved'], 'approved': []}; TRANSITION_ROLES={'submitted': ['applicant'], 'inspection': ['inspector'], 'correction': ['inspector'], 'approved': ['compliance_manager']}
CREATE_ROLES=set(['applicant']); RECORD_ROLES=set(['applicant', 'inspector']); AUDIT_ROLES=set(['compliance_manager', 'viewer']); VIEW_ROLES=set(['applicant', 'inspector', 'compliance_manager', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['approved'])
DEVICE_ENTITY='治理设备'; OUTLET_ENTITY='排放口'; LINK_ENTITY='设备排放口关联'; OUTLET_RECORD_ENTITY='排放口问题'
DEVICE_TRANSITIONS={'running': ['stopped'], 'stopped': ['running']}
OUTLET_TRANSITIONS={'active': ['inspection'], 'inspection': ['active']}
DEVICE_TRANSITION_ROLES={'stopped': ['applicant'], 'running': ['applicant']}
OUTLET_TRANSITION_ROLES={'inspection': ['inspector', 'compliance_manager'], 'active': ['inspector', 'compliance_manager']}
DEVICE_CREATE_ROLES=set(['applicant']); OUTLET_CREATE_ROLES=set(['applicant'])
LINK_ROLES=set(['applicant']); OUTLET_RECORD_ROLES=set(['applicant', 'inspector', 'compliance_manager'])
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
def can_transition_device(current,target): return target in DEVICE_TRANSITIONS.get(current,[])
def validate_device_transition(current,target):
    from .domain import DEVICE_STATES
    if current not in DEVICE_STATES or target not in DEVICE_STATES: raise ValidationError("未知设备状态")
    if not can_transition_device(current,target): raise ConflictError(f"设备不能从{current}转换到{target}")
def can_transition_outlet(current,target): return target in OUTLET_TRANSITIONS.get(current,[])
def validate_outlet_transition(current,target):
    from .domain import OUTLET_STATES
    if current not in OUTLET_STATES or target not in OUTLET_STATES: raise ValidationError("未知排放口状态")
    if not can_transition_outlet(current,target): raise ConflictError(f"排放口不能从{current}转换到{target}")
def device_transition_blockers(open_issue_outlets):
    return [f"关联排放口仍有未关闭问题：{', '.join(sorted(open_issue_outlets))}"] if open_issue_outlets else []
def outlet_losing_sole_running_device_blocker(other_running,outlet_status):
    # 检查中的排放口不能失去唯一在用设备
    if outlet_status=='inspection' and other_running==0:
        return "检查中的排放口不能失去唯一在用治理设备"
    return None
def outlet_inspection_blocker(running_devices):
    if running_devices==0:
        return "排放口没有在用治理设备，不能进入现场检查"
    return None
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
