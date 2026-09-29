# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

治理设备与排放口联动：

- `GET/POST /api/devices`、`GET/POST /api/outlets`（`GET`支持`?status=`过滤）
- `GET /api/devices/{id}`：设备状态、版本、关联排放口（含关联版本）、仍有未关闭问题的排放口
- `GET /api/outlets/{id}`：排放口状态、版本、关联设备及在用设备数、未关闭问题数
- `POST /api/devices/{id}/stop`：`{expected_version, reason?}`
- `POST /api/devices/{id}/start`：`{expected_version, link_versions: {排放口ID: 关联版本}}`，恢复启用时同时核验设备与全部关联关系仍是提交时的版本
- `POST /api/devices/{id}/links`：`{outlet_id}`建立关联；`{action:"unlink", outlet_id, expected_version}`解除关联（关联自身带版本）
- `POST /api/outlets/{id}/transition`：`{target, expected_version}`，`active`/`inspection`互转
- `GET/POST /api/outlets/{id}/records`、`POST /api/outlets/{id}/records/{rid}/close`：现场检查、整改、治理设备检修等问题记录
- `GET /api/devices/{id}/change-log`、`GET /api/outlets/{id}/change-log`：设备侧与排放口侧的变更记录（排放口视角含设备启停和关联/解除事件，可追溯操作人）

联动不变量：

- 停用设备前，所有关联排放口必须没有未关闭问题；
- 检查中（inspection）的排放口不能失去唯一在用设备（停用设备、解除关联都会被拒）；
- 排放口没有在用设备时不能进入现场检查（设备停用后无法继续进入检查）；
- 设备启停、排放口状态提交、解除关联均为条件更新，版本过期返回 409 冲突，不会覆盖他人变更。

角色：设备与关联的建立、启停、解除限 `applicant`；排放口状态转换限 `inspector`/`compliance_manager`；排放口问题可由 `applicant`、`inspector`、`compliance_manager` 登记和关闭；所有角色可查看。

旧数据库启动时自动幂等补齐 `devices`、`outlets`、`device_outlets`、`outlet_records` 表，已有许可与审计数据继续可用，审计链不断裂。

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
