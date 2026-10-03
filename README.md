# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 处置链一致性设计

网络抖动下，故障记录、审计时间线、备缆余量不再各自更新：三者与基线快照、动作日志在**同一SQLite事务**内提交，任何一步失败整体回滚，库中只存在"上次完整处置"。

- **同一处置链**：每次处置写入记录状态、审计事件、备缆库存、基线快照于一个事务；审计事件携带`action_id`、`basis_id`、`snapshot_id`和库存快照，时间线即权威依据。
- **后到提交待复核**：同一`reference`的故障记录被两人同时提交时，先到者生效，后到的一版写入`record_revisions`（`pending_review`），不覆盖已生效状态；`repair_manager`复核后接受（记录仍在`detected`时替换申报内容）或拒绝留档。
- **失效重算**：海况（`metocean_officer`）或备缆总量（`depot_keeper`）变化、以及其他记录批准/核销/取消引起余量变化时，`detected`/`approved`的未执行方案按最新依据重算——不可行则置`invalidated`（已批准的退回`detected`并释放备缆），恢复后自动重新生效；`mobilized`及以后的执行中方案只补一条`basis_kept`审计，**沿用原依据**不重算。
- **断点恢复**：动作先落`action_journal`（`started`）再执行，提交后同事务标记`committed`。启动时与`POST /api/recover`会对账：无落库效果的标记`rolled_back`，可安全续做。
- **幂等重放**：动作请求携带`action_id`，已提交的重放直接返回存档结果，不重复执行、不重复占用备缆；同一`action_id`携带不同请求返回409。

备缆占用规则：批准（`approve`）按`required_spare_km`锁定库存，接续（`splice`）按实际使用核销并释放差额，取消（`cancel`）退回预留。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动（启动即对账恢复）。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、方案重算和冲突检查。
- `src/repository.py`：SQLite建表、单事务原语、动作日志、库存与基线快照。
- `src/service.py`：用例编排、权限检查、乐观并发、失效重算、幂等与恢复。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和处置链一致性测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表并对账恢复。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（`payload.basis_id`/`plan_status`为当前依据与方案状态）。
- `GET /api/records/{id}/audit`：审计时间线（含库存快照与基线引用）。
- `GET /api/records/{id}/revisions`：后到版本列表。
- `GET /api/environment`：当前海况、最新基线与快照历史。
- `GET /api/inventory`：备缆总量、预留、使用、余量与预留明细。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`；同`reference`的后到提交返回`202`与`pending_review`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"action_id":"可选幂等键","data":{...}}`。
- `POST /api/records/{id}/revisions/{rid}/review`：复核后到版本，请求体为`{"decision":"accept|reject","note":"..."}`。
- `POST /api/environment`：登记海况或备缆总量变化，请求体为`{"sea_state":8,"spare_total_km":100,"reason":"..."}`（至少一项），触发未执行方案失效重算。
- `POST /api/recover`：动作日志对账恢复。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`noc_operator`、`repair_manager`、`vessel_master`、`cable_engineer`、`metocean_officer`（海况）、`depot_keeper`（备缆）、`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、权限拒绝、版本冲突、后到提交待复核、海况/备缆变化失效重算、执行中沿用原依据、断点恢复续做和幂等重放不重复占缆。
