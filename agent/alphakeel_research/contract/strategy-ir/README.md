# Strategy IR v1（`alphakeel.strategy-ir/1`）规范

日期：2026-10-07。状态：v1 定稿（T1）。方案：`docs/plans/strategy-ir-loop-2026-10-07.md` §2.1–§2.2。

一份 IR 文件（`strategy.json`）就是一个策略的**唯一定义**。Vibe-Trading（Python）与 AlphaKeel（Rust）各自独立实现本文的语义，
用 `vectors/` 下的共享金标向量证明一致；任何一侧都不得“翻译”另一侧的代码。本文是两侧实现的契约：实现与本文冲突时以本文为准，
并同时修正实现、`schema.json`、生成器与向量。

机器可读形状：`schema.json`（JSON Schema draft 2020-12）。schema 只表达形状；本文 §3 的跨字段规则（参数路径一致、
平台对与形态一致等）由两侧的 `validate` 执行。

## 1. 文档结构

```json
{
  "schema": "alphakeel.strategy-ir/1",
  "name": "carry-basic",
  "kind": "cross_venue_carry",
  "universe": {"pairs": "any", "shape": "cross_perp", "excluded_bases": ["LUNA"], "quote_ccys": ["USDT"]},
  "assumptions": {"horizon_hours": 72, "basis_stress": "0.0005", "funding_estimate": "predicted"},
  "entry": {
    "conditions": [{"feature": "net_conservative", "op": ">=", "value": "0.0005"}],
    "rank_by": "net_conservative", "candidate_limit": 5, "cooldown_minutes": 360, "one_per_base": true
  },
  "exit": {"max_hold_hours": 72, "max_loss": "20", "take_profit": null,
           "conditions": [{"feature": "funding_hourly_net", "op": "<", "value": "0"}]},
  "sizing": {"notional_per_leg": "1000", "max_open": 3, "leverage": null},
  "parameters": {"min_net": {"value": "0.0005", "robust": ["0.0003", "0.001"], "path": "entry.conditions[0].value"}},
  "provenance": {"tool": "vibe-trading", "run_id": "run-0001", "window": {"start": "2026-07-01", "end": "2026-09-30"},
                 "trials": {"count": 12, "evidence": "trials.jsonl"}}
}
```

所有对象都**拒绝未知字段**（`provenance` 内部除外）。没有 `features` 段：特征词汇表是固定的（§4）。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema` | 常量 | `alphakeel.strategy-ir/1`，其他值拒绝。 |
| `strategy_id` | 字符串，可选 | 自述 id；存在时必须等于计算值（§2）。不进定义。 |
| `name` | 非空字符串 | 人读名称，进 id。 |
| `kind` | 字符串 | v1 只接受 `cross_venue_carry`（两腿对冲，AK 执行层 docs/16）；其他值两侧一致地拒绝。 |
| `universe.pairs` | `"any"` 或 `[[long_venue, short_venue], …]` | 平台名同 `screener::venue::Venue`：`binance okx bybit bitget gate aster hyperliquid lighter backpack`。列表有序（顺序进 id）、非空、不重复；`cross_perp` 要求两平台不同，`spot_perp` 要求相同。 |
| `universe.shape` | `cross_perp` \| `spot_perp` | 组合形态；与候选的 `kind` 相同才参与。 |
| `universe.excluded_bases` | 大写字符串列表 | 不参与的基础币。 |
| `universe.quote_ccys` | 大写字符串列表，缺省 `["USDT"]` | 允许的计价币（非空、不重复）。缺省值在计算 id 前补上。 |
| `assumptions.horizon_hours` | 整数 ≥ 1 | 估算持有时长（小时）。 |
| `assumptions.basis_stress` | 十进制串，`[0, 1)` | 基差不利变化压力（占名义比例），只进保守情景。 |
| `assumptions.funding_estimate` | `predicted` \| `last_settled` | 视图里 `funding_rate` 的来源。不改变公式；运行时不能提供该来源时必须**拒绝加载**，不得静默替换（T3 执行）。 |
| `entry.conditions` | 条件列表（AND） | 每条 `{feature, op, value}`，见下。可为空。 |
| `entry.rank_by` | 特征名 | 降序排名。 |
| `entry.candidate_limit` | 整数 1–200 | 每个时刻最多保留的候选数。 |
| `entry.cooldown_minutes` | 整数 ≥ 0 | 同一基础币平仓后的冷却。 |
| `entry.one_per_base` | `true` | v1 固定为 true。 |
| `exit.max_hold_hours` | 整数 ≥ 1 | 最长持有。 |
| `exit.max_loss` | 十进制串 > 0 | 计价币金额：`net_now ≤ −max_loss` 即平。 |
| `exit.take_profit` | 十进制串 > 0 或 `null` | 必填（可为 null）。 |
| `exit.conditions` | 条件列表（OR） | 任一成立即平。 |
| `sizing.notional_per_leg` | 十进制串 > 0 | 每腿名义（计价币）。原样传给引擎，数量取整归引擎。 |
| `sizing.max_open` | 整数 1–50 | 同时持仓上限。 |
| `sizing.leverage` | 十进制串 > 0 或 `null` | 必填（可为 null = 运行环境的值）。 |
| `parameters` | 对象 | 参数名 → `{value, robust: [lo, hi], path}`，供人审阅；见 §3。可为 `{}`。 |
| `provenance` | 对象 | 自由内容（工具、run_id、研究窗口、试验数、数据审计、研究凭证……）；只检查形状：`tool`（非空字符串）与 `window.start`、`window.end`（字符串）必填。不进 id。 |

条件：`{"feature": <特征名>, "op": ">=" | "<=" | ">" | "<" | "between", "value": <十进制串> | [<lo>, <hi>]}`。
`between` 是闭区间 `lo ≤ x ≤ hi`，要求 `lo ≤ hi`，且只有 `between` 的 value 是区间。所有比较值都是十进制串
（毫秒类特征也是，如 `"5000"`）；布尔特征 `price_estimated` 按 `0/1` 比较（例：`{"feature": "price_estimated", "op": "<=", "value": "0"}` = 只要真实盘口）。

**数字**：金额、比例、阈值一律是**规范十进制字符串**——无指数、无 `+`、无多余前导零、无小数尾零、无 `-0`
（正则 `^-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$` 且不是 `-0`）。因此同一数值只有一种写法、只有一个 id。
JSON 整数只用于计数、小时、分钟（`horizon_hours`、`candidate_limit`、`cooldown_minutes`、`max_hold_hours`、`max_open`）。

## 2. 规范形式与 `strategy_id`

- `definition` = 文档去掉 `provenance` 与 `strategy_id` 两个顶层键；若 `universe.quote_ccys` 缺省则补 `["USDT"]`。
- `canonical` = 键按码点排序、无空白、UTF-8 原样输出、只允许整数的 JSON（与研究契约 docs/37 §2 同一函数：
  Rust `alphakeel_util::canon::canonical_bytes`，Python `json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`）。
- `strategy_id = "ir-" + sha256_hex(canonical(definition))[:24]`。

推论（`vectors/canonical.json` 逐条验证）：改 `provenance` 不改 id；键顺序不改 id；改任何数值、假设、名称、列表顺序都改 id。

## 3. 校验（两侧一致）

1. 形状：`schema.json`；未知字段、未知特征、未知运算符、未知平台、十进制串不规范 → 拒绝。
2. `schema` 常量、`kind = cross_venue_carry`。
3. §1 表中的取值范围；`universe.pairs` 与 `shape` 一致；`excluded_bases`、`quote_ccys` 大写。
4. 条件：`between` ↔ 区间且 `lo ≤ hi`；其他运算符 ↔ 单值。
5. `parameters`：v1 **不支持占位符**。每个参数用 `path` 指向它描述的字面量，路径语法 `seg(.seg)*`，`seg = name([index])*`，
   第一段只能是 `entry`、`exit`、`sizing`、`assumptions`（例：`entry.conditions[0].value`、`entry.conditions[1].value[0]`、
   `exit.max_hold_hours`、`sizing.notional_per_leg`）。路径必须指向十进制串或整数，且数值等于 `value`；`robust` 满足 `lo ≤ value ≤ hi`。
6. `provenance` 形状。
7. 文档带 `strategy_id` 时必须等于 §2 的计算值。
8. `provenance` 内部不参与 id，可以含浮点数；其它任何位置出现浮点数都拒绝（金额/比例必须是十进制串）。

**错误码（两侧相同，报告第一个失败的规则）**：`ir.shape`（第 1 条）、`ir.schema`、`ir.kind`（第 2 条）、`ir.invalid`（第 3、4、6 条，附路径）、
`ir.parameter_path`、`ir.parameter_mismatch`（第 5 条）、`ir.strategy_id_mismatch`（第 7 条）。Rust `IrError::code()` 与 Python
`IrError.code` 返回同一字符串。

## 4. 决策视图与特征

### 4.1 视图

一个决策时刻 `now_ms` 的输入是一组候选组合（帧）。每个组合：

```
PairView { id, base, kind: cross_perp | spot_perp, long: LegView, short: LegView }
LegView  { venue, symbol, market: perp | spot, quote_ccy, bid, ask, bbo: bool,
           funding_rate?, interval_hours?, next_funding_ms?, funding_px?, volume_quote?, taker_fee, quote_ms? }
```

`?` 字段可缺失或为 `null`，表示“视图里没有”，**不是 0**。`id` 在帧内唯一（筛选器的写法是
`cross|spot:BASE:long_venue:long_symbol>short_venue:short_symbol`，但语义上只要求唯一，用作排序次序）。
Python 实现用 60 位精度、Rust 用 `rust_decimal`（约 28 位有效数字）：只在第 28 位之后的舍入平局上可能不同，向量不覆盖该情形，视为已知边界。`funding_px` 是平台用来算资金费的名义价格（标记价 / Hyperliquid oracle / Lighter index），对应筛选器 `PerpQuote` 的
`funding_px` 或 `mark`；`next_funding_ms` v1 特征不用。

**组合不合法**（不是特征不可用，整对拒绝，原因 `invalid:<code>`）：

- `quote_ccy_mismatch`：两腿 `quote_ccy` 不同。v1 不做 USDC/USDT 换汇（筛选器的 `quote_fx` 不在范围内）。
- `shape_mismatch`：`cross_perp` 要求两腿都是 `perp` 且平台不同；`spot_perp` 要求做多腿 `spot`、做空腿 `perp`、同一平台。

### 4.1a 从扫描帧枚举组合（两侧必须一致）

Python（`Pit`）与 Rust（扫描帧 / `QuoteIndex`）各自从**原始报价**枚举候选组合，**不**用筛选器 `Screen.opportunities`
（它已被扫描配置的成交额门槛、偏离剔除与 `top_n` 裁过，两侧不可能一致）。规则：

1. 决策时刻 `now_ms` = 该帧的决策时间 `t`。腿 = 本帧有报价行（`quotes.frame == 本帧序号`）的合约；资金费字段取**同一帧**的
   `observations` 行（永续没有本帧观测 → `funding_rate`/`interval_hours`/`next_funding_ms`/`funding_px` 缺失，特征按不可用处理）。
   旧帧的报价不延用：不在本帧的合约不进帧。报价行带交易所时间 `quote_ts` 且 `quote_ts > now_ms` 的腿**不进帧**
   （决策可见性没有未来容忍，与 docs/37 点时间规则一致；Python `Pit` 本就隐藏它，Rust 侧必须同样丢弃）。
2. `base`、`quote_ccy` 取自数据包 `instruments` 表的 `base`、`quote` 列；只用 `selected == true` 的合约。
3. `taker_fee` 取数据包费用表 `fees[venue].perp|spot`；缺失 → 该腿不进帧。`quote_ms` = 报价行 `quote_ts`（可空），`bbo`
   = 报价行 `bbo`，`funding_px` = 观测行 `funding_px`，缺失时退回报价行 `mark`，再缺失则为空（§4.2 用中间价）。
4. `cross_perp`：同一 `base`、同一 `quote_ccy`、两个不同平台的永续，**有序**对 (long, short) 都枚举（A>B 与 B>A 是两个组合）。
   `spot_perp`：同平台、同 `base`、同 `quote_ccy` 的 (spot 多腿, perp 空腿)。`id` 用筛选器写法
   `cross|spot:BASE:long_venue:long_symbol>short_venue:short_symbol`，以便决策层对比时键相同。
5. 帧内组合按 `id` 升序交给 §5；§5 再按 IR 的 `universe` 过滤。

### 4.2 公式

记做多腿 L、做空腿 S，`H = assumptions.horizon_hours`，`bs = assumptions.basis_stress`。所有运算是精确十进制。
这些公式就是 `screener::score::{funding_over, economics, ratios}`（筛选器 `Opportunity` 用的同一份代码）：

```
mid(X)        = (X.bid + X.ask) / 2
fpx(X)        = X.funding_px            若视图给出；否则 mid(X)            # 近似，非平台公式
fund(X)       = 0                                             若 X.market = spot
              = ((X.funding_rate × fpx(X)) × H) / X.interval_hours   若 X.market = perp
                不可用：perp 腿缺 funding_rate、缺 interval_hours 或 interval_hours = 0
n             = L.ask                                         # 参考名义：做多腿卖一价
fees_q        = (L.ask × L.taker_fee + S.bid × S.taker_fee) + (L.bid × L.taker_fee + S.ask × S.taker_fee)
spread_q      = (L.ask − L.bid) + (S.ask − S.bid)
stress_q      = n × bs
funding_q     = fund(S) − fund(L)                             # 空腿收入 − 多腿支出（持有期，每 1 基础币）
basis_q       = mid(S) − mid(L)
kept          = funding_q × 0.5   若 funding_q > 0；否则 funding_q        # 正资金费减半，负的全额
cons_q        = kept − (fees_q + spread_q + stress_q)
exp_q         = (funding_q + basis_q) − (fees_q + spread_q)
hourly        = (funding_q / H) / n                           # 未取整
```

特征（比例都是“占做多腿卖一名义”的小数，0.001 = 0.1%）：

| 特征 | 值 | 取整 |
| --- | --- | --- |
| `funding_hourly_net` | `hourly` | 10 位 |
| `funding_apr` | `hourly × 8760`（用**未取整**的 hourly） | 6 位 |
| `funding` | `funding_q / n` | 8 位 |
| `basis` | `basis_q / n` | 8 位 |
| `spread` | `spread_q / n` | 8 位 |
| `fees` | `fees_q / n` | 8 位 |
| `stress` | `stress_q / n` | 8 位 |
| `net_expected` | `exp_q / n` | 8 位 |
| `net_conservative` | `cons_q / n` | 8 位 |
| `volume_min` | `min(L.volume_quote, S.volume_quote)`（计价币） | 不取整 |
| `quote_age_ms` | `max(max(0, now_ms − L.quote_ms), max(0, now_ms − S.quote_ms))` | 整数 |
| `leg_skew_ms` | `|L.quote_ms − S.quote_ms|` | 整数 |
| `price_estimated` | `not (L.bbo and S.bbo)` | 布尔 |

- 取整一律**银行家舍入**（half-even，`rust_decimal::round_dp` 与 Python `ROUND_HALF_EVEN`），输出去尾零、`-0` 写作 `0`。
  条件与排名比较的是**取整后的值**。
- 不可用：前九个特征（经济学组）在任一腿 `fund` 不可用或 `n = 0` 时**整组**不可用；`volume_min` 在任一腿缺 `volume_quote`
  时不可用；`quote_age_ms`、`leg_skew_ms` 在任一腿缺 `quote_ms` 时不可用（比筛选器严格：筛选器对单腿缺时间的组合取已知腿）；
  `price_estimated` 总是可用。
- 未来时间的报价年龄按 0 计（与筛选器相同）；时钟可信度检查（`quote_time_verified`）不在 v1 特征里。
- 输出文本：十进制特征为规范十进制串，毫秒为 JSON 整数，`price_estimated` 为布尔，不可用为 `null`。

## 5. 决策语义

输入：IR、帧（`pairs`）、状态、`now_ms`。

```
PositionView { id, base, long: LegRef, short: LegRef, opened_ms, net_now? }   LegRef { venue, symbol }
State { positions: [PositionView], cooldowns: { base → last_close_ms } }
Decision { exits: [{position_id, reason}],
           entries: [{pair_id, base, long: LegRef, short: LegRef, notional_per_leg, features}],
           rejected: [{pair_id, reason}] }
```

`net_now` = 引擎给出的“现在全部平仓”的净盈亏（计价币）；未知为 null。基础币比较一律先转 ASCII 大写。

**(1) 退出先于进场。** 按 `position_id` 升序逐个持仓，取第一个成立的原因：

1. `now_ms − opened_ms ≥ max_hold_hours × 3 600 000` → `max_hold`
2. `net_now` 已知且 `net_now ≤ −max_loss` → `max_loss`
3. `take_profit` 非空、`net_now` 已知且 `net_now ≥ take_profit` → `take_profit`
4. 在帧里找**第一个**两腿 `(venue, symbol)` 都与持仓相同的组合，算特征；`exit.conditions` 按列表顺序第一个**可用且成立**的条件
   → `condition:<feature>`。组合不在帧里或不合法（§4.1）→ 没有特征退出，只有 1–3。不可用的特征使该条件不成立。

退出不看 `universe`（已开的仓不因宇宙收窄而被迫平仓，只按退出规则）。

**(2) 进场。** 对帧里每个组合依次检查，第一个失败即拒绝并记录原因：

1. `kind ≠ universe.shape` → `universe:shape`
2. `universe.pairs` 不含 `[long.venue, short.venue]` → `universe:pair`
3. `base ∈ excluded_bases` → `universe:excluded_base`
4. 组合不合法 → `invalid:quote_ccy_mismatch` / `invalid:shape_mismatch`
5. `long.quote_ccy ∉ quote_ccys` → `universe:quote_ccy`
6. 按列表顺序逐条入场条件：特征不可用 → `unavailable:<feature>`；不成立 → `entry:<feature>`
7. `rank_by` 特征不可用 → `unavailable:<feature>`
8. 该基础币已有持仓（含本时刻刚决定退出的）→ `open_base`
9. `cooldowns[base] + cooldown_minutes × 60 000 > now_ms` → `cooldown`

通过的组合按 `rank_by` **降序**、`pair_id` **升序**排序；之后依次：

10. 同一基础币只保留排名最高的一个，其余 → `duplicate_base`（`one_per_base`）
11. 排名第 `candidate_limit` 之后 → `candidate_limit`
12. 剩下的前 `max(0, max_open − len(positions))` 个进场，其余 → `max_open`

**本时刻决定退出的持仓仍按“开着”计**（第 8 步与第 12 步）：退出要等引擎确认成交后才释放基础币与名额，
否则同一时刻“平 A 开 A”会在平仓失败时变成双倍敞口。下一时刻引擎把它从 `positions` 移除、在 `cooldowns` 记平仓时刻。

输出次序：`exits` 按 `position_id` 升序；`entries` 按排名；`rejected` 按 `pair_id` 升序，每个组合只报第一个原因。
`entries[].notional_per_leg` 就是 `sizing.notional_per_leg`，`features` 是 §4 的完整特征（含 `null`）。

### 5a. 对账用的意图命名与 `net_now`（两侧一致）

- **意图 id**：Python `ir_policy` 与 Rust `ir_strategy` 运行都用 `n<trade>-<L|S>-<open|close>`（`trade` 从 1 起，每个进场决定按
  `entries` 次序各占一个号；`L` = 做多腿，`S` = 做空腿；任一腿没有真实一档买卖价（`bbo = false`）或缺汇率的进场决定**不提交意图、不占号**，
  与 Rust `paper::open_planned` 的拒绝一致），`position_ref = p<trade><L|S>`，`pair_id = pair-<trade>`。研究服务的
  L2 决策层按 `intent_id` 逐条对比，命名不同就无法对账。
- **意图数量**：两腿同量，`qty = notional_per_leg ÷ long.ask`，**半偶舍入到 8 位小数**（两侧相同；本地模拟器的数量精度也是 8 位）。
- **缺报价时不退出**：持仓的任一腿在本帧没有可成交报价（没有本帧报价行、`bbo = false` 或缺汇率）时，本帧**不提交该组合的退出意图**
  （即使 `max_hold` 已到），等下一帧有报价再退出；与 Rust 纸上执行“估值冻结时不出场”一致。不得用旧帧报价定价退出。
- **`net_now`（§5 的持仓立即平仓净额）**：两侧同一公式 = 触及价盈亏（多腿 `qty × (bid − avg_entry)`，空腿
  `qty × (avg_entry − ask)`）+ 该仓位已结算的资金费 − 入场手续费 − 触及价平仓 taker 费；单位为腿的计价币（v1 只允许 USDT）。
  研究服务的 policy 上下文按**腿**给出 `positions[].net_now`（同一公式按腿计），策略把同一 `pair` 两腿相加得到组合的 `net_now`；
  paper/回测的 Rust 决策器用 `Mark.net_if_closed`（同一公式按组合计）。任一腿缺可成交报价 → 该组合 `net_now` 未知（null）。

## 6. 向量

`vectors/` 下三个文件，由 `tools/gen-strategy-ir-vectors.py` 按本文公式用 Python `Decimal` 独立生成（不引用任何一侧实现），
`--check` 校验仓库里的文件是最新的：

- `features.json`：`{pair, assumptions, now_ms} → expected`（特征）或 `error`（`quote_ccy_mismatch` / `shape_mismatch`）。
  覆盖正/负资金费、`funding_px` 与中间价回退、不同结算间隔、缺成交额、缺报价时间、未来报价时间、缺资金费率、无盘口、
  现货+永续、计价币不同、形态不符、第 8 位银行家舍入、不同持有期。
- `canonical.json`：`doc → definition_canonical, strategy_id`；含 `same_id_as` / `different_id_from` 标注
  （出处变化、键顺序、自述 id、`quote_ccys` 缺省、数值变化、假设变化、平台对列表及其顺序）。
- `decisions.json`：`{ir, frame, state, now_ms} → expected`（Decision）。覆盖按排名进场、`candidate_limit`、`max_open`、冷却、
  `one_per_base`（已持仓与同时刻重复）、特征不可用、每一种退出原因、持仓组合不在帧里、宇宙过滤（形态/平台对/排除币/计价币/非法组合）、
  `between` 与布尔特征、现货+永续进场。

## 7. 实现

- **Rust**：`crates/strategy-ir`（包 `alphakeel-strategy-ir`，lib `strategy_ir`；层序在 `core` 与 `store` 之间，
  只依赖 `alphakeel-util` 与 `alphakeel-screener`）。`ir`（类型、`Ir::parse`、`validate`、`strategy_id`）、`features`
  （`LegView`/`PairView`/`Features`、`compute`，经济学直接调用 `screener::score::{funding_over, economics, ratios}`）、
  `decide`（`PositionView`、`State`、`Frame`、`decide`）。全部纯函数。`tests/vectors.rs` 逐项比对三个向量文件。
  规范 JSON 的唯一实现是 `alphakeel_util::canon`（研究契约 `alphakeel_research_contract::contract::canonical_*` 调用它）。
- **生成器**：`tools/gen-strategy-ir-vectors.py`（第三份独立实现，只依赖本文）。改规范时：先改本文，再改生成器与两侧实现，
  重新生成向量并让两侧测试通过。
- **Python**（T2）：Vibe-Trading `agent/alphakeel_research/ir.py`，跑同一组向量。

## 8. 不在 v1

需要历史序列的特征（z-score、资金费持续性、回本时间 `payback_hours`）、跨计价币换汇、`kind` 以外的策略形态、参数占位符。
schema 不为它们占位，两侧也不做假实现。

**运行时守卫不属于 IR。** 在线 paper（`arb-screen screen --paper-state … --strategy <id>`）与跨所执行器（`arb-screen cross-live run --strategy <id>`）
加载 IR 时，在 IR 的退出规则之外加一道运行时守卫：任一腿标记价进入强平价的 `paper.liq_buffer` 范围即平仓（与内置规则的
`liquidation_risk` 同一判断 `screener::paper::near_liquidation`，平仓原因 `liquidation_risk`、`strategy_reason = runtime:liq_buffer`）。
守卫只在这两个运行环境打开（`IrDecider::with_runtime_guards`）；回测与研究服务的 `ir_strategy` 保持纯 IR 语义，Python 侧也不实现它，
所以守卫产生的退出在对账（L2）里看不到，是运行环境的安全措施而不是策略的一部分，也不进 `strategy_id`。
