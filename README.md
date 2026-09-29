# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量，以及治理设备启停联动规则。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：设备联动演示页（排放口、治理设备、变更记录三个页签）。
- `tests/`：完整流程、规则、失败和设备联动测试。

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

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 治理设备与排放口联动

一台治理设备可服务多个排放口（多对多，关联关系单独版本化）。设备启停和关联变更均为乐观锁，过期版本返回`409 conflict`，避免覆盖他人变更；所有变更写入审计哈希链并记录操作人。

- `POST /api/devices`：登记治理设备（`compliance_manager`），初始状态`in_use`、版本1。
- `GET /api/devices` / `GET /api/devices/{id}`：列表与详情，含`status`、`version`、`links`、`outfalls`和`link_versions`快照。
- `POST /api/devices/{id}/transition`：
  - `{"target":"stopped","expected_version":N}`停用。停用前校验：关联排放口没有未关闭问题；检查中的排放口不会因此失去唯一在用设备。
  - `{"target":"in_use","expected_version":N,"expected_links":{"关联ID":版本}}`恢复启用。同时核验设备版本和全部关联关系版本仍与提交时一致。
- `POST /api/devices/{id}/outfalls`：请求体带`item_id`为建立/恢复关联；带`link_id`+`expected_version`为解除关联（软删除，版本前进，可重新关联）。检查中的排放口不能失去唯一在用设备。
- 排放口提交进入`inspection`时校验：有关联设备就必须至少有一台在用（无关联设备的历史排放口不受约束）。
- 排放口接口返回`devices`字段展示在用/停用设备与关联版本。
- `GET /api/audit?entity_type=治理设备`：按实体类型过滤变更记录（也支持`entity_id`）。

旧版本数据库启动时自动补齐`devices`和`device_outfalls`表，已有许可、记录和审计数据继续可用，审计链不断。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
