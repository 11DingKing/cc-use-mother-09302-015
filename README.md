# 非遗材料捐赠隔离

本项目维护非遗材料捐赠隔离的领域约定、角色边界与样例数据，并提供完整的服务端实现：为每批及每件捐赠记录来源、用途限制、验收项目、保管位置和放行决定，只有批准数量才能转入可用库存，任何时刻都可从捐赠入口追踪到领用、归还或处置结果。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/quarantine_service/`：捐赠隔离服务端（仅标准库，SQLite 持久化）。
  - `db.py`：表结构与 `BEGIN IMMEDIATE` 写事务。
  - `service.py`：领域操作与事务分录。
  - `api.py`：HTTP JSON 接口。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动服务端。
- `tests/`：契约、领域行为、并发与接口回归测试。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

## 服务端

启动：`python3 tools/run_server.py --db data/quarantine.db --port 8000`

### 角色与权限

| 操作 | 捐赠方 | 学校资产管理员 | 使用教师 |
| --- | --- | --- | --- |
| 登记批次 | ✔ | ✔ | |
| 部分接收 / 验收 / 放行 / 退回 / 处置 / 限制变更 | | ✔ | |
| 领用 / 归还 | | ✔ | ✔ |

变更请求在 JSON 体内携带 `actor`（操作人）与 `role`（角色）；缺失返回 401，越权返回 403。

### 接口一览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/batches` | 登记捐赠批次（含物品、来源、用途限制、保管位置）；凭证号唯一，重复凭证返回原批次 |
| `GET /api/batches` / `GET /api/batches/{id}` | 批次列表 / 详情 |
| `GET /api/batches/{id}/trace` | 从捐赠入口到领用、归还或处置的完整分录链 |
| `POST /api/items/{id}/receipts` | 部分接收（累计不得超过登记数量） |
| `POST /api/items/{id}/acceptance` | 记录验收项目（合格 / 不合格） |
| `POST /api/items/{id}/release` | 放行决定：批准数量从隔离转入可用 |
| `POST /api/items/{id}/return-to-donor` | 退回捐赠方（隔离或可用库存） |
| `POST /api/items/{id}/dispose` | 处置（隔离或可用库存） |
| `POST /api/items/{id}/restriction` | 限制变更（记录旧值、新值与原因） |
| `POST /api/items/{id}/checkouts` | 领用（校验用途限制，并发安全） |
| `POST /api/checkouts/{id}/return` | 归还（可部分归还；不可再用部分计入处置） |
| `GET /api/items/{id}` / `GET /api/items/{id}/trace` | 物品详情 / 单件去向追踪 |
| `GET /api/inventory` | 当前可用库存（含来源批次） |
| `GET /api/ledger?batch_id=&item_id=` | 事务分录查询 |

### 一致性约定

- 每件物品的数量在「隔离 / 可用 / 领用 / 退回捐赠方 / 处置」五个桶之间迁移，总和恒等于已接收数量，数据库 `CHECK` 约束兜底。
- 只有验收合格且经放行的批准数量才能从隔离转入可用库存；领用只扣可用库存。
- 存在不合格验收记录的物品禁止放行，只能退回捐赠方或处置。
- 所有变更在单个 `BEGIN IMMEDIATE` 事务内完成「条件更新 → 业务记录 → 事务分录」，并发领用不会超发。
- 所有变更接口支持可选的 `request_key` 幂等键：重复提交返回首个响应，不产生副作用；批次凭证号唯一，重复凭证返回原批次。
- 每次变更写一条事务分录（操作人、角色、动作、数量、来源桶、目标桶、明细），任何时刻可按批次或物品追踪去向。
