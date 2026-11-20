# 档案保全解释与整案移交（archive_handoff）

两个职责完全独立的服务，共用 SQLite 持久化，仅通过“资格产品（qualification product）”
交换数据。全部为虚构资料，不执行真实删除，也不做现实合规判断。

```
archive_handoff/
  models.py          # 不可变领域记录（Rule/Document/Event/Hold/资格产品）
  rules_service.py   # 规则/事件/保全目录 + 资格计算 + 产品的冻结与落盘
  handoff_service.py # 整案移交仓：保管责任、整案校验、准备/确认/计划、对账
  materials.py       # 只读导入 materials/751 或任意同布局目录
  scenario.py        # 文档化 751 走查、operations 回放、200/10/500/100 规模生成
  cli.py             # 命令行
tests/               # unittest（无需 pytest）
```

## 核心规则（规则/事件侧）

- 每档案有稳定 `doc_id`、`case_id`、一个或多个规则 ID。
- 每条规则给触发事件类型与非负保存天数：`计算日 = 最新适用事件日 + 保存天数`。
- 一个档案的**所有规则都满足**才谈时间条件；计算日取各规则计算日的**最大值**。
  任一必要触发缺失 → 该档案**待定**，**不把缺失事件当作第 0 天**。
- 查询日（整数）**达到**计算日即满足时间条件（`as_of >= computed`）。
- 保全可针对档案或整个 case，区间 `[start, end)`，`end` 为空表示未释放。
  查询日任一适用保全有效即禁止移交。
- **保全不暂停保存期时钟**：冻结天数不追加到到期日。D1 的计算日始终是 30，
  不是 30+25。
- 逐档案输出：计算来源（哪条规则、哪个事件、哪次修订）、有效保全、缺触发原因。
  同一档案可有**多个阻止原因**（如 `hold` + `not_due`），明细按稳定键去重，只出现一次。
- 输入批次**整批接受或整批拒绝**：同号同文复读为幂等 no-op；同号异文、
  非法区间（`end <= start`）、未知引用（未知 doc/rule/case）一律整批拒绝。
- 资格产品按固定 `as_of_day` 冻结，记录 `source_revision` 与各输入修订；
  历史产品不可被后续目录倒改。

## 移交仓（移交侧）

- 拥有**自己的**保管责任状态，初始每案卷为 `LOCAL`。责任状态 = 追加式流水
  `custody_events` + 维护的当前态 `custody_current`；打开时按流水归约对账，
  维护态被篡改会被修复。
- case 必须**整案交接**。一次请求明确 `case_id`、**完整** `doc_ids` 集合、
  `from`、`to`、单一 `as_of_day`。
  - 集合必须**恰好等于**当前案卷成员：少一个成员、或多一个，都拒绝（`not_whole_case`）。
  - 每份成员必须在该查询日的资格产品中 `releasable=true`，否则整案拒绝
    （`ineligible_members`）。
  - 当前保管人必须等于 `from`，否则拒绝（`custody_mismatch`）。
  - 单份都合格但漏成员，同样拒绝；**不能**把规则侧“可移交=true”的行直接导出就算完成。
- 移交侧**不重算**保全日期：它只消费规则侧产出的资格产品（按内容摘要校验）。
- 成功请求在**一个本地事务**内：写交接记录（固定实际成员、资格来源、交接 ID）、
  追加保管流水、更新当前保管人。
- 同 `handoff_id` 同文复读 → 只读回冻结记录；异文 → 拒绝。
- 已移交案卷再用新 ID 声称从 `LOCAL` 交出 → `custody_mismatch` 拒绝。
- **准备/计划失效**：发布计划（`prepare_and_publish`）或准备提案（`prepare`）时冻结
  成员、保管人与逐档资格摘要。确认时若成员、保管责任或相关资格任一变化，
  旧准备整批拒绝（`stale_plan` / `stale_proposal`），不会半案生效。
- **历史固定**：历史计划/提案/交接成员不按当前目录倒改。

## 命令

```bash
DATA=artifacts/demo

# 导入 materials/751（只读输入）
python3 -m archive_handoff.cli --data $DATA import-materials --materials materials/751

# 文档化 751 全流程（生成 artifacts/ 下的 P1.json / P2.json / scenario.json）
python3 -m archive_handoff.cli --data $DATA demo

# 回放 materials/751/operations.jsonl（P1 过期被拒、P2 可用、P1 历史只读）
python3 -m archive_handoff.cli --data $DATA operations

# 发布资格产品（固定查询日，落盘 products/<id>.json）
python3 -m archive_handoff.cli --data $DATA publish --day 40 --product Q40

# 修改保全（修订号递增；旧修订/异文整批拒绝）
python3 -m archive_handoff.cli --data $DATA revise-hold \
  --hold '{"hold_id":"H1","revision":2,"case_id":"C1","start":25,"end":35}'

# 单条移交请求
python3 -m archive_handoff.cli --data $DATA submit \
  --request '{"handoff_id":"WHOLE1","case_id":"C1","doc_ids":["D1","D2"],"from":"LOCAL","to":"VAULT1","as_of_day":40}'

# 从文件批量提交（每行一条完整请求；--allow-rejects 忽略个体拒绝）
python3 -m archive_handoff.cli --data $DATA requests --file my_requests.jsonl --allow-rejects

# 自定义文件导入（rules/documents/events/holds/materials）
python3 -m archive_handoff.cli --data $DATA ingest --kind holds --file extra_holds.json

# 状态与重开核对
python3 -m archive_handoff.cli --data $DATA status
python3 -m archive_handoff.cli --data $DATA verify

# 规模数据：200 档案 / 10 规则 / 500 事件+保全 / 100 请求（生成+校验+重开）
python3 -m archive_handoff.cli --data artifacts/scale scale
```

移交请求是“公开写全”的普通 JSON，可存成文件再用 `requests --file` 实际导入。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：日期算术与边界、缺触发待定、保全半开区间与不暂停时钟、多阻止原因去重、
批次原子拒绝、产品冻结、整案校验、部分拒绝、复读/异文、保管链与重开对账、
751 文档化走查、operations 回放、200/10/500/100 规模。
