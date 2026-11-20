# 戏院座票存根重放与字幕牌可见度（seat_archive）

离线、可复查的座位状态复原工具。输入是几册**座票存根流水、多版座图、换座单与
后补勘误**，输出是任意业务切面的占座状态（带原凭据）以及字幕牌可见度/选牌报告。
纯 Python 标准库，无外部服务；全部几何数用 `fractions.Fraction` 精确比较，不使用浮点。

与仓库根目录原有的 `archive_handoff`（档案移交）完全独立，互不依赖。

## 目录与职责

```
seat_archive/
  rationals.py   精确有理数解析/格式化（"5/8"、"-13/4" 等）
  models.py      不可变记录：Seat / SeatMapVersion / TicketEvent / Erratum / Study / Occupancy
  store.py       追加式案卷仓：身份摘要、幂等复读、异文冲突、派生报告落盘
  replay.py      业务顺序重放：发票/入场/离场/作废/单人换座/双人互换
  visibility.py  横截面投影、阴影并集、可见区段、等宽候选牌选择
  dossier.py     两套内置示例案卷 A（题面数值例）与 B（全流程例）
  cli.py         命令行
tests/           unittest（无需 pytest），与既有测试并存
```

## 业务规则（重放侧）

- **多版座图**：同场可保留多版座图；票面 `(排号, 座号)` 按凭证自身所载
  `map_version` 解释到跨版本稳定的 `seat_id`。未知版、未知票面号一律拒绝。
- **业务顺序**：按流水序号处理 `issue → admit → leave → void → swap → exchange`。
- **售出未入场不占座**：`issue` 只取得座位售出权；`admit` 才进入占座表。
- **单人换座**：持有效存根且在场（present）方可换到**空座**；目标非空且本动作
  没有相应移出时拒绝（`target_not_empty`），原观众不会被挤走。
- **双人互换**：一次动作，按**换前状态**同时核对两人、同时生效。任一写入前完成
  全部校验：缺一方（`party_missing`）、任一方已作废（`party_voided`）、不在场
  （`party_not_present`）、目标被第三人占用且无对应移出（`target_not_empty`）
  都整笔拒绝，座位原状不动。
- **作废**：未入场存根可作废；已作废存根再入场/换座/互换均拒绝。
- **被拒动作留痕**：拒绝记录仍在流水里，带 `status="rejected"` 与稳定原因码，
  但不改变任何状态。
- **后补勘误**：勘误只追加（`errata.jsonl`），把目标事件替换为更正动作后另起
  修订（如 `r2`）重放至指定切面；原始修订（`original`）的旧切面始终可读。
- 每个占座都带**原凭据链**：发票事件 `issued_by`、入场事件 `admitted_by`、
  最近换座事件 `last_move_by`。

## 可见度几何（题内横截面约定）

- 字幕牌在 `y=10`，阅览眼点在 `y=0`；占用座位在 `0<y<10` 给出横向不透明闭线段。
- 眼点 `(eye_x, 0)` 与座位截面点 `(x, y)` 的连线延至字幕线，投影横坐标为

      x_s = eye_x + (x - eye_x) · 10 / (10 - y)

  即阴影落在座位**远离眼点**一侧。题面校验：眼点 0、`y=5` 的座位段 `[1, 5/2]`
  投影为字幕线上的 `[2, 5]`。
- 空座不产生遮挡；多个阴影先做并集，**重叠只扣一次**；仅端点相碰为零测度，
  对长度无影响（裁剪时严格 `c_lo < c_hi` 才计长度）。
- 选牌：候选牌必须**等宽**；最大化“最差观察者仍可见的长度”，平手按给定
  `board_id` 字典序破同。报告逐观察者列出阴影并集、各牌可见区段与精确长度。

## 内置示例

### 案卷 A：题面数值例（从存根一路重放到选牌）

唯一占用座 S1 在 `y=5` 的线段 `[1, 5/2]`，候选左牌 `[0,3]`、右牌 `[3,6]`：

| 研究 | 切面/修订 | 眼点 | 阴影 | 左牌可见 | 右牌可见 | 选牌 |
|---|---|---|---|---|---|---|
| A1_BEFORE_SWAP | cut 6, original | x=0 | `[2,5]` | 2 | 1 | **B_LEFT** |
| A2_AFTER_SWAP  | cut 7, original（T1 换入 S2） | x=0 | `[0,3/2]` | 3/2 | 3 | **B_RIGHT** |
| A3_EYE_MOVED   | cut 6, original（座图占座固定） | x=3 | `[-1,2]` | 1 | 3 | **B_RIGHT** |

### 案卷 B：全流程例（覆盖其余要求）

8 个稳定座位、两版座图（v1 甲乙排 / v2 AB 排，同物理座位票面号不同）、24 笔
流水与 1 条勘误，覆盖：售出未入场（T8）、未入场/已废票换座被拒、换到占座被拒、
双人互换成功（e23）与失败三例（缺一方 e21、目标非空 e22、废票 e20）、离场
（T7→S7 空座）、勘误把 e23 更正为 T5/T6 互换。三个观察眼点（0、3、−3）下
B1/B2/B3 的最差可见长度分别为 `0、3/4、3/2`，选 **B3**；另有 `B-TIE` 研究
两块牌均完整可见、按牌号破同选 **B4**。

## 命令

```bash
DATA=materials/theater

# 建立两套示例案卷（同内容重复执行为幂等 no-op）
python3 -m seat_archive.cli build --all --data $DATA

# 重放全部研究，落盘 snapshots/ 与 reports/
python3 -m seat_archive.cli run-examples --data $DATA

# 任意切面/修订的座位状态（含逐笔 trace 与原凭据）
python3 -m seat_archive.cli snapshot --data $DATA/B --cut 24 --revision r2

# 单个研究的可见度与选牌
python3 -m seat_archive.cli visibility --data $DATA/B --study B-CORRECTED

# 重开案卷：核对 dossier 身份摘要、报告自身摘要及“可由流水重建且一致”
python3 -m seat_archive.cli verify --data $DATA
```

## 落盘格式（离线 JSON）

```
<data>/<dossier>/
  dossier.json    案卷身份 + 座位几何 + 各版座图 + 研究配置（带 digest，改一个字节即失效）
  events.jsonl    票面动作流水，只追加（同号同文幂等，同号异文/序号冲突拒绝）
  errata.jsonl    勘误，只追加（修订号唯一，必须指向已知事件）
  snapshots/*.json 切面占座 + trace + 摘要（派生物，可随时重建）
  reports/*.json   各观察者阴影/可见区段/长度 + 排名 + 选牌 + 摘要
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：投影公式与题面三幕、重叠阴影只扣一次、端点相碰零长度、多观察点最差长度
最大化、牌号破同、等宽校验、状态机各拒绝原因、双人互换原子性、勘误重放与旧切面
可读、追加仓幂等/冲突/防篡改。
