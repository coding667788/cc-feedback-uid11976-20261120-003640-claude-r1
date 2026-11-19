# 园区访客预约：离线通行裁定器

一个完全离线的访客通行判定器。它只回答一个问题：**在第 `t` 分钟，某位访客能否进入园区某个区域？** 答案只取决于三类事实——预约是否有效、陪同人是否持续在岗、目的区域是否接受该陪同人的资格——以及预约期间发生的陪同交接。

- Windows 原生 Python 3.12，仅使用标准库与 SQLite。
- 不连接真实门禁、操作系统管理接口或任何外部服务。
- 自带 `load` / `coverage` / `admit` / `verify` 四个命令、样本数据和 61 个测试。

---

## 1. 快速开始

```bash
# 载入样本批次
python -m visitor_pass --db park.db load samples/01-base.json

# 查看可通行区间与陪同变化
python -m visitor_pass --db park.db coverage --booking B-DEMO

# 裁定一次通行（幂等键 REQ-1）
python -m visitor_pass --db park.db admit --booking B-DEMO --at 10 --request REQ-1

# 载入一条资格撤销
python -m visitor_pass --db park.db load samples/03-revocation.json

# 复核全部不变量（不写库）
python -m visitor_pass --db park.db verify
```

一次完整的演示（含撤销对旧回执的影响）可以直接运行：

```bash
python samples/demo.py
```

重新生成样本：

```bash
python samples/make_samples.py
```

运行测试：

```bash
cd tests && python -m unittest discover -s . -p "test_*.py" -v
```

---

## 2. 数据模型

所有时间都是**整数分钟**，区间一律**左闭右开** `[start, end)`。区间长度可以超过一亿分钟（`10^8`）；实现只扫描端点，不逐分钟存储或遍历。

### 2.1 预约（booking）

| 字段 | 含义 |
|---|---|
| `booking_id` | 预约 ID，主键 |
| `visitor` | 访客 |
| `area` | 目的区域 |
| `start`, `end` | 预约有效期 `[start, end)` |
| `quota` | 本地名额上限（该预约可放行的次数） |
| `home_escort` | **首位陪同人** |

预约创建后不可改写；同一 `booking_id` 以不同内容再次出现会被整批拒绝。

### 2.2 在岗（presence）与资格（qualification）

两者都是半开区间，可以按人按区域存多段：

```json
{"person": "Escort-Wang", "start": 48, "end": 92}
{"person": "Escort-Wang", "area": "A1", "start": 0, "end": 120}
```

**某人在某分钟可陪同**，当且仅当该分钟同时落在他的**在岗**区间与**资格**区间内。这两类区间各自都是若干半开区间的并集，因此**相接且无空隙**的多段（例如 `[0,30)`、`[30,60)`）会合并成一段，被视为连续覆盖。

### 2.3 陪同交接（handover）

```json
{
  "handover_id": "HO-1",
  "booking_id": "B-DEMO",
  "area": "A1",
  "old_escort": "Escort-Li",
  "new_escort": "Escort-Wang",
  "at": 50,
  "confirmations": [
    {"person": "Escort-Li",   "booking_id": "B-DEMO", "area": "A1", "at": 50},
    {"person": "Escort-Wang", "booking_id": "B-DEMO", "area": "A1", "at": 50}
  ]
}
```

一个交接在 `t` 时刻**有效**，必须同时满足：

1. `t` 落在预约区间内；
2. 当前**实际在岗陪同人**（designated escort）恰好是 `old_escort`；
3. `old` 与 `new` **都**对同一预约、同一区域、同一 `t` 给出确认（`confirmations` 中各自出现一条完全匹配的记录）；
4. `old` 与 `new` 的窗口**都连续覆盖** `[t-2, t+2)`；
5. `t` 严格晚于该预约上一条**已生效**的交接（时间必须前进）。

有效交接在 `t` **生效**：从第 `t` 分钟起，实际陪同人变为 `new_escort`。

> 注意：`confirmations` 是权威来源。为书写方便，也可以用 `"old_confirmed": true` / `"new_confirmed": true` 作为简写，载入时会等价地补成确认记录。

### 2.4 资格撤销（revocation）

```json
{"revocation_id": "REV-ZHAO", "person": "Escort-Zhao", "area": "A1", "effective": 100}
```

撤销**可以回溯到历史时刻**：它取消该人该区域**自 `effective` 分钟起**的全部资格，包括那些在撤销之前就登记、并且跨越了 `effective` 的长区间。也就是说，`[0, 120)` 的资格在 `effective=100` 之后被截断为 `[0, 100)`。

这是一次**尾部截断**，而非按登记顺序的编辑：一旦某人在某区域存在撤销，则其 `effective` 及之后的分钟一律无资格，不论相应的资格记录是在撤销之前还是之后登记。撤销对该人该区域是一个硬下界，本实现不提供"在撤销区间内重新授予"的操作。这样做的理由是回放确定、与登记顺序无关；任务描述中的场景也不需要跨越撤销点的重新授予。若将来需要，"撤销 → 之后重新授予"应当建模为**新的人员资格周期**（新的有效起点），而不是在同一时间轴上打洞。

---

## 3. 裁定语义

### 3.1 实际陪同人时间线

把预约区间 `[start, end)` 切分成若干"朝代"：

- 预约开始时，实际陪同人是 `home_escort`；
- 每一条**有效**交接在时刻 `t` 切换实际陪同人。

**无效交接从不改变实际陪同人。** 这带来两个重要推论：

- 旧陪同人的空白时间**不能**被另一个从未被交接过去的陪同人填补——跨人拼接必须有有效交接；
- 如果第一条交接无效，后续所有"旧陪同人等于第一条交接的新陪同人"的声明都无法生效，因为它们依赖一条从未成立的交接。这正是"后续也不得续接依赖无效交接的声明"。

### 3.2 可通行区间

对实际陪同人时间线上的每一段，取该陪同人在该段内的窗口（在岗 ∩ 资格）。窗口内的分钟可通行；窗口外形成**不可通行的空白**。

空白**不会**因为同一陪同人稍后重新在岗而自动闭合——跨过空白同样需要交接。但同一陪同人晚些时候真正在岗的那部分仍然是可通行的，因此 `coverage` 返回的是**若干段最大连续可通行区间**，而不是"一条前缀"。

一条**有效**交接可以在空白之后发生：它只要求交接双方各自覆盖 `[t-2, t+2)`。此时空白本身依旧不可通行，但 `t` 之后的分钟按新陪同人的窗口判定。例如：

```
E1 在岗 [0,30) 与 [40,100)，E2 在岗 [0,100)
H1: E1 -> E2 at 45
→ 可通行 [0,30) E1、[40,45) E1、[45,100) E2；空白 [30,40) 永远不可通行
```

这里 H1 是有效的（E1、E2 都覆盖 `[43,47)`），它不"填补"空白，只是从 `t=45` 起换人。

### 3.3 一个直观例子

```
预约 B-DEMO: [0, 120)   区域 A1   首位陪同 Escort-Li
Escort-Li    在岗 [0, 52)     资格 [0, 120)
Escort-Wang  在岗 [48, 92)    资格 [0, 120)
Escort-Zhao  在岗 [88, 120)   资格 [0, 120)

HO-1: Li -> Wang at 50   （两者都覆盖 [48, 52)，且各自确认）
HO-2: Wang -> Zhao at 90 （两者都覆盖 [88, 92)）
```

结果：

```
[0, 50)   Escort-Li
[50, 90)  Escort-Wang
[90, 120) Escort-Zhao     ← 三者相接，整段 120 分钟可通行
```

若 `Escort-Wang` 的在岗改为 `[49, 92)`（比 `t-2=48` 晚一分钟），`HO-1` 失效，则 `Wang` 永远不会被指定为陪同人，`HO-2` 也随之失效，`[50, 120)` 全部不可通行。

---

## 4. 命令

所有命令都向标准输出打印一个 JSON 对象。

### `load <batch.json | ->`

以**整批原子**方式载入。一个批次可以包含任意组合的 `bookings` / `presence` / `qualifications` / `revocations` / `handovers`。出现下列任一问题时，**整批拒绝、不落库**，退出码 `2`：

| 情形 | `error` |
|---|---|
| 同一预约在同一分钟有两条交接 | `duplicate-minute` |
| 交接时间早于已存的最后一条交接（时间倒退） | `backwards-time` |
| 交接图形成有向环（`A -> B -> A` 等） | `handover-cycle` |
| 同一 `handover_id` 两次出现且内容不同 | `handover-conflict` |
| 同一 `revocation_id` 两次出现且内容不同 | `revocation-conflict` |
| 同一 `booking_id` 已存但内容不同 | `booking-conflict` |
| 复用已用过的 `batch_id` 但内容不同 | `batch-conflict` |
| 交接引用了不存在的预约、字段类型错误等 | `input` |

**完全相同的重复**（同 ID、同内容）是幂等空操作：同一 `batch_id` 的同一批次重放会返回 `"idempotent": true`；跨批次重复的交接/撤销会被忽略并计入 `duplicates_ignored`。

批次内交接按 `at` 排序应用，所以**批次内乱序**是合法的；非法的是越过已存历史的"倒退"。

### `coverage --booking <id> [--from M] [--to N]` / `coverage --all`

返回：

- `coverage`：最大连续可通行区间（已合并、按时间排序）；
- `passable`：逐段陪同记录，含 `escort` 与 `entered_by`（引入该段的交接 ID）；
- `changes`：**每一次实际陪同变化**，按时间排序；
- `gaps`：不可通行的空白；
- `largest`：最大连续可通行区间；
- `handovers`：每条交接及其 `applied` / `reason`；
- `scan`：证明端点扫描的元信息（`minutes_iterated: 0`）。

### `admit --booking <id> --at <minute> --request <id>`

判定某访客在第 `at` 分钟能否通行。

- **被接受**时返回 `"admitted": true`，并附 `evidence`（用到的陪同段、用过的交接链、之前的空白）。
- **被拒绝**时返回 `"admitted": false` 与 `reason`，可能是 `outside-booking-window`、`no-escort-coverage`、`quota-exhausted`。
- **一次通行只占一个名额**：只有 `admitted: true` 的回执会消耗 `quota`；被拒绝的请求不占名额。
- **幂等**：同一个 `--request` 再次调用时，永远原样返回**首次回执**（`"idempotent": true`），不会重新裁定、不会二次占名额。即使传入不同的 `--at`，也只返回首次回执，并在 `request_mismatch` 中说明。
- 预约状态更新后如需重新裁定，必须使用**新的请求 ID**。

### `verify`

只读地重算一切：重建每条陪同链、复算名额、检查回执完整性，报告：

- `quota`：每个预约的 `admitted_receipts` 与 `remaining`；
- `receipt_drift`：因后续变更而不再成立的旧回执（`severity: hard` 表示现在已不可通行，`soft` 表示陪同人变了但仍可通行）；
- `invariants`：`quota_respected`、`receipts_intact`、`drifted_receipts_registered`、`no_orphan_impacts`、`no_orphan_handovers`。

> `verify` 报告漂移是**正常**的：旧回执本就应该保留原判，漂移只说明"自那以后世界变了"，并且必须已经有对应的影响记录。`verify` 只在**未记录**漂移或名额越界时才返回 `ok: false`（退出码 `1`）。

---

## 5. 资格撤销与旧回执

这是本实现最需要耐心理解的部分。

### 5.1 撤销是可回溯的

撤销 `effective` 是一个**历史时刻**。载入后：

- 该人该区域自 `effective` 起的资格立即消失；
- 因此一些原先有效的交接可能失效（若它需要被撤销窗口内的分钟）；
- 失效交接又可能让后续交接一起失效（见 §3.1）。

### 5.2 旧回执原样保留，另记影响

已发出的回执**永远不会被改写**。当一批撤销导致某个已放行回执的结论发生变化时，系统追加一条**影响记录**，包含：

- `verdict_before` / `verdict_after`；
- `escort_before` / `escort_after`；
- `first_cause`：**导致变化的第一处依据**——按生效时间排序，第一条单独撤销就能改变该回执时刻陪同人的撤销。

再次用同一请求 ID 调用 `admit`，返回的首次回执中 `impacted_by` 就会列出这些影响记录；回执本身的 `admitted` / `escort` / `created_at` 保持不变。

### 5.3 演示

`python samples/demo.py` 的输出节选：

```
== revocation: Escort-Zhao lost A1 qualification from t=100 ==
  [  0, 50) Escort-Li
  [ 50, 90) Escort-Wang
  [ 90,100) Escort-Zhao

== replay REQ-3: first receipt preserved, audit appended ==
  still admitted: True escort: Escort-Zhao
  impact: admitted -> rejected | revocation REV-ZHAO: qualification of
          Escort-Zhao for A1 is cancelled from t=100; escort at t=100
          changes from Escort-Zhao to none
```

---

## 6. 并发与一致性

- 每条命令都在**一个 SQLite 事务**中完成。写命令用 `BEGIN IMMEDIATE`，因而撤销载入、回执裁定、名额更新**串行化**在同一个快照上。
- 两个并发请求争最后一个名额时，恰好一个成功，另一个得到 `quota-exhausted`；`verify` 保证不会超额（测试 `test_quota_and_concurrency.py` 覆盖）。
- `admit` 的幂等检查与插入在同一 `BEGIN IMMEDIATE` 事务内，因此并发重放同一请求 ID 也只会产生一张回执。
- 数据库使用 WAL 模式，读命令（`coverage`、`verify`）在只读事务中看到一致快照。

---

## 7. 算法与复杂度

`coverage` 与 `admit` 的核心是**端点扫描**，不是逐分钟循环：

1. 每个人在每个区域的"窗口" = `merge(在岗) ∩ effective(资格)`，是若干合并后的区间；
2. 实际陪同人时间线由首位陪同人和已生效交接的 `at` 切分，段数 = 交接数 + 1；
3. 每段与对应陪同人的窗口求交，得到可通行段与空白段；
4. 所有操作都是排序、合并、求交，复杂度 `O((P + Q + H) log(P + Q + H))`，其中 P 是在岗区间数，Q 是资格区间数，H 是交接数。

**区间长度不进入复杂度。** 测试 `test_scale.py` 覆盖：

- 2 亿分钟的预约，端点扫描在毫秒级完成；
- 2000 段交接链、10 亿分钟跨度，`load` / `coverage` / `admit` / `verify` 全部在几十毫秒内完成。

---

## 8. 测试

```bash
cd tests && python -m unittest discover -s . -p "test_*.py"
```

| 文件 | 覆盖内容 |
|---|---|
| `test_intervals.py` | 半开区间合并、相接合并、相离不合并、求交、包含、首个空洞 |
| `test_chain_semantics.py` | 相接区间拼接、`[t-2,t+2)` 恰好覆盖、**恰少一分钟**被拒、空白永久不可通行、无效交接阻断后续交接、未确认交接惰性 |
| `test_revocation_and_receipts.py` | 回溯撤销截断资格尾部、撤销早期资格使两次后续交接同时失效、旧回执保全与影响记录、`first_cause` 取最早生效撤销、跨区域隔离 |
| `test_batch_rejection.py` | 同分钟重复交接、二元/三元环、跨批次环、时间倒退、重复 ID 内容冲突、批次原子性、类型校验、批次内乱序合法 |
| `test_quota_and_concurrency.py` | 名额按预约计算、拒绝不占名额、幂等重放不占第二名额、**两个并发请求争最后一个名额**、多线程竞争 |
| `test_random_reference.py` | 与**逐分钟参考裁定器**对比的 120 个随机小用例：密集/稀疏两种生成模式，逐分钟比对放行结论与陪同人身份，并断言测试本身确有覆盖多样性 |
| `test_scale.py` | 一亿/两亿分钟跨度、2000 段交接链、端点扫描不逐分钟 |

参考裁定器位于 `visitor_pass/reference.py`，它以完全不同的输入形状（按人的在岗字典、元组形式的资格/撤销/确认）逐分钟重算，避免"用同一份代码验证自己"。

---

## 9. 目录结构

```
visitor_pass/
  __init__.py
  __main__.py     CLI（load / coverage / admit / verify）
  store.py        数据模型、SQLite schema、半开区间代数、撤销生效计算
  chain.py        陪同链与实际陪同人时间线；端点扫描
  validation.py   批次整批校验（同分钟、环、倒退、重复 ID）
  service.py      事务化服务：load / coverage / admit / verify、影响记录
  reference.py    逐分钟参考裁定器（仅测试使用）
samples/
  make_samples.py 生成样本 JSON
  01-base.json …  样本批次
  demo.py         端到端演示
tests/
  support.py      测试夹具
  test_*.py       见上表
```

---

## 10. 设计取舍

**为什么空白不"永久封死后续"？** 任务要求 `coverage` 返回"最大连续可通行区间"（复数），并要求"没有有效交接时，原陪同人的空白时间保持不可通行"。把后续恢复的在岗也判为不可通行会把结果压成单条前缀，与"最大连续可通行区间"的表述相悖。因此本实现的规则是：**空白本身不可通行，空白之外的分钟按实际陪同人的窗口判定。** 唯一无法跨越的是"跨人拼接"——那必须有有效交接。

**为什么撤销按 `effective` 直接截断，而不是按登记顺序？** 任务原文是"自该点及之后的资格"被取消，按时间截断是最直白的读法，且不受登记顺序影响，便于审计复现。

**为什么确认必须逐条匹配 `booking/area/t`？** 任务要求"各自确认同一预约、区域和 `t`"。因此确认记录里出现任何字段不一致（例如区域写错）都视为未确认，交接失效。

**为什么失效交接还会保留在 `handovers` 输出里？** 审计需要看到"声明过、但未生效"的交接及其拒绝原因，而不是让它静默消失。
