# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

审计时间线、故障记录、备缆余量收在同一条处置链上：每个业务动作在同一SQLite事务内
落"记录状态 + 审计事件 + 备缆占用 + 链步骤"，要么全部生效、要么全部不生效，不再出现
"三份数据各自更新、断在半路说不清哪份为准"。

## 处置链语义

1. **重复申报只留待复核，不覆盖已生效状态**
   两位值班员用同一`reference`提交故障时，第一版正常创建（201），后到一版进入
   `record_reviews`待复核队列（202 `parked_for_review`），记录版本、状态完全不变，
   审计时间线记一条`revision_parked`。复核人可`accept`（按当前海况/备缆重算后采纳）
   或`reject`，记录从创建起始终只有一条。

2. **海况/备缆变化：未执行失效重算，执行中沿用原依据**
   `sea_state`或备缆总余量变化时，环境台账版本递增，所有未执行（detected/approved）
   方案标记`basis_status=invalidated`并记`basis_invalidated`事件；继续推进（批准/动员）
   会收到409 `plan_invalidated`，必须先`replan`按新依据重算。
   动员(mobilize)那一刻冻结依据（`basis_status=frozen`并留快照），之后海况再变
   不影响执行中方案，已占用的备缆也不释放。

3. **保存一半失败可续做，重放不重复占缆**
   每个动作分两阶段：先以`idem_key`登记prepared检查点（独立事务提交），再在一个
   `BEGIN IMMEDIATE`事务内完成 记录+审计+备缆占用+步骤done。第二阶段前崩溃时，
   - 新动作会被409 `recovery_required`挡住，要求先续做；
   - `POST /api/records/{id}/resume`（或带同一`idem_key`重发原请求）即从检查点继续；
   - 已完成动作带同一`idem_key`重放只返回当时结果（`replayed=true`），
     备缆占用表以record_id为主键并有台账余额校验，不会重复占用。

   不带`idem_key`时默认键为`r{record}-v{expected_version}-{action}`，网络抖动下
   原样重发天然幂等。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误（含`PlanInvalidated`/`RecoveryRequired`）和基础校验。
- `src/rules.py`：状态转换、依据快照与失效、备缆占用指令、冲突检查。
- `src/repository.py`：SQLite建表、环境台账、待复核、处置链两阶段事务和备缆占用。
- `src/service.py`：用例编排、权限、幂等执行/恢复/重放、环境失效广播。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、重复申报并发、失效/冻结、断点恢复与幂等占用测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表并初始化环境台账
（海况3、备缆总量100km）。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/environment`：海况、备缆总量/已占用/可用余量、环境版本。
- `POST /api/environment`：更新海况/备缆（部分字段即可），敏感字段变化时同事务失效未执行方案。
- `GET /api/records`：记录列表，可带`state`和`limit`。
- `GET /api/records/{id}`：记录详情（payload含`basis_status`、`basis_env_version`）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/chain`：处置链全景（步骤序列+备缆占用+依据版本）。
- `POST /api/records`：创建记录；同reference重复提交返回202待复核。
- `POST /api/records/{id}/actions/{action}`：执行动作，请求体
  `{"expected_version":1,"idem_key":"可选","data":{...}}`，动作含
  approve/mobilize/survey/splice/test/restore/cancel/replan；响应含`replayed`/`resumed`。
- `POST /api/records/{id}/resume`：从断在半路的检查点续做，可带`idem_key`。
- `GET /api/reviews?status=pending`：待复核版本列表。
- `POST /api/reviews/{id}/accept|reject`：值班长复核后到版本。
- `GET /api/stats`：状态统计。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复/并发申报挂起、权限拒绝、版本冲突、环境失效与
执行中冻结、跨进程断点恢复、幂等重放不重复占用备缆。
