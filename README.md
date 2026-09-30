# 非遗材料捐赠隔离

为社会机构捐赠物资（如非遗服饰）提供从登记、隔离、验收、放行到领用、归还与处置的完整服务端。针对“验收完成前被教师领走、事后说不清哪些物品仍在隔离”的问题，系统以**双分录台账 + 逐件全链路追踪**为核心，保证只有验收批准数量才能转入可用库存，所有写操作以单事务过账。

## 设计要点

- **逐件登记，来源可溯**：每批（批次号、捐赠凭证号、捐赠方、物资名称与申报数量）及每件物品（唯一编码、批次内序号、规格）均留源；登记时借记「待收捐赠」、贷记「捐赠来源」。
- **强制隔离**：接收入「隔离库存」并登记保管位置；未完成验收放行的物品无法领用，从机制上杜绝“验收前领走”。
- **验收项目与逐件批准**：每次验收记录验收项目（PASS/FAIL/NA）与逐件批准/拒收结论；批准件进「已批准待放行」，拒收件进「验收拒收」（仍受控，等待退回或处置）。
- **只有批准数量可放行**：放行把「已批准待放行」按数量转入「可用库存」，超发或放行拒收件一律拒绝。
- **部分接收 / 退回 / 处置**：支持分批接收；拒收件可退回社会机构；隔离中、拒收或可用物品可按结论处置核销（必须登记处置方式）。
- **用途限制变更**：限制按版本留痕（`batch_restrictions`），同步写入每件物品的追踪事件，不移动实物。
- **重复凭证 / 重复单号**：捐赠凭证号在批次表唯一；接收单、放行单、领用单号等写入事务表唯一键，重复提交整笔事务回滚。
- **并发领用安全**：所有写操作在 `BEGIN IMMEDIATE` 事务内进行，SQLite 写事务串行化；同一件物品并发领用只有一个成功，其余以 409 拒绝，绝不超发。
- **全链路追踪**：每件物品的登记、接收、验收、放行、领用、归还、退回、处置与限制变更都在 `item_trace` 有序留痕，任何时候可从捐赠入口追踪到最终去向。

### 台账账户

| 账户 | 含义 |
| --- | --- |
| 捐赠来源 | 唯一对冲账户（负），登记时确认 |
| 待收捐赠 | 已登记尚未实际接收 |
| 隔离库存 | 已接收、验收放行前强制隔离 |
| 验收拒收 | 不适合学生使用，待退回/处置 |
| 已批准待放行 | 验收批准、尚未转入可用库存 |
| 可用库存 | 已按批准数量放行，可领用 |
| 领用在外 | 教师领用未归还 |
| 退回捐赠方 / 处置核销 | 终态去向 |

**不变量**：每笔事务分录增量之和为 0；实物账户在每件物品维度不得为负；每件物品实物余额合计恒为登记数量（数量守恒）；触发器为负余额最后防线。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/donation_service/`：捐赠隔离服务端（零第三方依赖）。
  - `accounts.py`：台账账户与事务类型。
  - `database.py`：SQLite 表结构、触发器与连接管理。
  - `errors.py`：业务错误与 HTTP 状态映射。
  - `services.py`：双分录台账与全部业务用例。
  - `api.py`：标准库 JSON HTTP 接口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、领域服务（含并发）与 HTTP 端到端回归测试。

## 运行

```bash
python3 -m donation_service.api --db donations.db --host 127.0.0.1 --port 8000
```

需将 `src` 加入 Python 路径（如 `PYTHONPATH=src`）。启动后接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/donors` | 登记捐赠方 |
| GET | `/donors` | 捐赠方列表 |
| POST | `/batches` | 登记批次与逐件物品（`voucher_no` 唯一） |
| GET | `/batches` / `/batches/{batch_no}` | 批次列表 / 详情（含余额、验收、物品、事务） |
| POST | `/batches/{b}/receive` | 接收（`qty` 或 `item_codes`，必填 `ref_no`、`location`） |
| POST | `/batches/{b}/inspect` | 验收（`decisions` 或 `qty_approved` + `checks`） |
| POST | `/batches/{b}/release` | 仅批准数量放行入可用库存 |
| POST | `/batches/{b}/restriction` | 用途限制变更（版本留痕） |
| POST | `/batches/{b}/return` | 拒收/隔离中物品退回捐赠方 |
| POST | `/batches/{b}/dispose` | 处置核销（必填 `method`） |
| POST | `/issues` | 教师领用（必填 `ref_no`，并发安全） |
| GET | `/issues` | 领用在外清单（可按 `?teacher=` 过滤） |
| POST | `/returns` | 归还（`REUSE` 重回可用 / `DISCARD` 处置） |
| GET | `/items/{item_code}/trace` | 单件全链路追踪 |

业务错误统一返回 `{"ok": false, "error": {"code", "message"}}`，如 `duplicate_voucher`、`duplicate_reference`、`quarantine_control`、`insufficient_balance`（HTTP 409）。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 全部回归测试（24 个）
python3 -m compileall -q src tools tests      # 编译检查
python3 tools/check_contract.py domain/contract.json
```

## 典型流程

```text
登记(待收捐赠) ──部分接收──▶ 隔离库存 ──验收──▶ 已批准待放行 ──放行──▶ 可用库存 ──领用──▶ 领用在外
                                   └─▶ 验收拒收 ──▶ 退回捐赠方                       └─归还─▶ 可用库存 / 处置核销
```
