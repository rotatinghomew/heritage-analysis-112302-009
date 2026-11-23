# 街巷修缮分期经营影响与缓冲审核工具

本目录提供一个 **只依赖 Python 3.11 标准库** 的分析工具，用仓库内的合成账册
演示“分期围挡 → 可归因经营影响 → 缓冲资格与金额复核”的完整链路。所有金额、
店铺和通知均为样例，不含真实统计结论。

## 快速开始

```bash
# 估计各期直接受阻、外溢和可支持区间
python3 -m street_buffer estimate --data fixtures

# 审核已发缓冲通知，可用 text 便于阅读
python3 -m street_buffer review --data fixtures --format text

# 经营人视图：有效依据、仍未知、重算影响、下一步
python3 -m street_buffer merchant --data fixtures --shop S1

# 比较两个尚未执行的分期方案
python3 -m street_buffer scenario --data examples --scenarios examples/scenarios.csv

# 保存上一版审核结果后，补报资料重审
python3 -m street_buffer review --data examples --save before.json
python3 -m street_buffer reassess --data examples --previous before.json
```

`examples/` 是一份更完整的合成账册，包含施工阶段、绕行路线、同期事件和方案表；
`fixtures/` 是仓库原有最小样例。两个命令均输出 JSON（`review`、`merchant` 除外
的 text 选项只影响展示）。

运行测试：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
```

## 估计口径（对应 `docs/业务规程.md`）

1. **角色三分**：直接受阻（施工街段或账册标记 `detour=yes`）、可到达性外溢
   （侧街 `spillover=yes`）、未受影响。只有未受影响店铺可进入对照池；同期受
   绕行影响的店铺**不作无干扰对照**。
2. **共同趋势**：用对照指数与修缮前周拟合本店比例趋势。方向冲突、残差漂移
   超出 `--trend-tolerance`、或对照在修缮前无变化时，不把别处变化迁移过来。
   结论降为 `estimated_sensitivity_only`，不进入可支持额度。
3. **反事实与区间**：每周反事实 = 基线 × 趋势映射；损失 = 反事实 − 实际。
   区间来自确定性种子的自助重抽，并叠加修缮前残差噪音下限。
4. **数据状态**：自然闭店、缺报、营业但零交易分别统计，互不填补；缺报不按
   零交易计损失，自然闭店不计施工归因，零交易按真实零收入计算。
5. **成交与转让**：金额按成交周归属，结算周不同不移动收入。经营人变更后，
   承接人在施工前没有同经营人基线时标记 `owner_transfer_cut`，不继承前手影响。
6. **多阶段重叠**：同一经营周只计一次损失；阶段金额为展示分摊（等分），
   `overlap_weeks` 明示重叠，阶段金额不可相加。
7. **同期事件**：`events.csv` 中的节日、邻街先封等事件周从基线、对照与损失
   中排除，并列入“应排除的同期影响”。

## 审核口径

每条通知同时核对：

- **覆盖日期与对象**：覆盖周是否包含可归因影响周；经营人是否与账册一致。
  已付/已签但对象不一致转人工，不把影响挪给承接人。
- **金额与支付状态**：可支持下限/上限与通知金额比较。已签通知的**未支付部分**
  可重算（`adjust_unpaid`）；**已支付金额只提交复议**，不得自动冲回
  （`invalidated_manual` / `paid_excess_manual`）。
- **去重**：同一经营人同一周被多个通知覆盖时，只有优先通知计入额度，其余标记
  `duplicate_cover`，汇总中的“去重后支持上限”只算一次。
- **未覆盖影响**：有正影响但无通知覆盖的周列入“未获通知的影响”，可另行申请。

补报重审（`reassess`）区分三类处理：

| 情形 | 结果 |
| --- | --- |
| 尚未签发 / 草稿，证据更正 | 可更正的新申请 |
| 已通知但未支付，证据变化 | 仅调整未兑现部分 |
| 原结论失效且已有支付 | 保留已付记录，转人工复议 |

## 目录

- `street_buffer/dataset.py`：CSV 读取、角色判定、阶段/路线/事件索引、完整性检查
- `street_buffer/estimation.py`：对照池、共同趋势、逐周反事实、自助区间、阶段汇总
- `street_buffer/review.py`：通知审核、去重、补报重审、经营人视图
- `street_buffer/scenario.py`：分期方案覆盖缺口与预计缓冲占用比较
- `street_buffer/__main__.py`：命令行入口

## 输入扩展（全部可选）

主账册 `primary.csv` 为必需；支持表可按 `support-*.csv` 提供。以下文件可选：

| 文件 | 关键字段 | 用途 |
| --- | --- | --- |
| `notices.csv` | `notice, shop, owner, period/start_week, amount, status, paid` | 通知；也可放在 `support-*.csv` |
| `phases.csv` | `phase, street, start_week, end_week, kind` | 多阶段窗口；`kind=side` 表示外溢侧街 |
| `routes.csv` | `route_id, status, start_week, end_week` | 绕行通达；`closed` 的起止为关闭期 |
| `events.csv` | `event, kind, street, scope, start_week, end_week` | 同期应排除事件 |
| 方案表 | `scenario, phase, street, start_week, end_week, effect_ratio, detour_ratio` | `scenario` 子命令 |

主账册可用 `spillover=yes` 显式标记侧街外溢；没有标记时，施工期内未标记绕行
的店铺按未受影响处理，不臆测外溢。未登记的路线按“通达状态未知”提示，不直接
判定中断。
