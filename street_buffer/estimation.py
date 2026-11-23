"""可归因经营影响估计。

估计遵循街道协议的三条底线：

* 同期施工影响范围内的店铺（直接受阻或绕行外溢）不进入对照池；
* 修缮前与对照缺少共同趋势支撑的经营人，不把别处变化迁移过来；
* 自然闭店、缺报、营业但零交易分别处理，不互相填补；金额按实际营业
  经营人及成交周归属，不随店铺转让自动转移。
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from statistics import mean, median
from typing import Mapping, Sequence

from .dataset import Dataset, Observation
from .util import mad, money, sort_weeks, week_key


@dataclass(frozen=True)
class EstimationConfig:
    min_pre_weeks: int = 2
    trend_pre_weeks: int = 3
    min_controls: int = 1
    confidence: float = 0.95
    bootstrap: int = 500
    trend_tolerance: float = 0.55
    uncertainty_floor: float = 0.05
    seed: int = 20261123
    include_spillover: bool = True
    use_all_weeks_as_pre: bool = False

    def __post_init__(self) -> None:
        if self.min_pre_weeks < 1:
            raise ValueError("min_pre_weeks 至少为 1")
        if self.bootstrap < 0:
            raise ValueError("bootstrap 次数不能为负")


@dataclass
class DataQuality:
    trade_weeks: int = 0
    zero_weeks: int = 0
    missing_weeks: int = 0
    natural_closed_weeks: int = 0
    works_closed_weeks: int = 0
    duplicate_weeks: int = 0
    transfer_cut: bool = False
    settlement_lags: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "成交周": self.trade_weeks,
            "营业但零交易周": self.zero_weeks,
            "缺报周": self.missing_weeks,
            "自然闭店周": self.natural_closed_weeks,
            "施工相关闭店周": self.works_closed_weeks,
            "重复冲突周": self.duplicate_weeks,
            "转让截断": self.transfer_cut,
            "成交与结算不同周": list(self.settlement_lags),
        }


@dataclass
class WeekEstimate:
    week: str
    role: str
    phases: tuple[str, ...]
    actual: float | None
    counterfactual: float | None
    effect: float | None
    low: float | None
    high: float | None
    status: str
    supportable_loss: float | None
    notes: list[str] = field(default_factory=list)
    phase_allocation: dict[str, float] = field(default_factory=dict)

    @property
    def change_window_member(self) -> bool:
        return self.low is not None and self.low > 0

    def as_dict(self) -> dict:
        return {
            "周": self.week,
            "角色": self.role,
            "施工阶段": list(self.phases),
            "实际": money(self.actual),
            "反事实": money(self.counterfactual),
            "影响": money(self.effect),
            "可信下限": money(self.low),
            "可信上限": money(self.high),
            "可支持损失": money(self.supportable_loss),
            "状态": self.status,
            "阶段分摊": {k: money(v) for k, v in self.phase_allocation.items()},
            "说明": list(self.notes),
        }


@dataclass
class PhaseEstimate:
    phase: str
    direct_loss: float = 0.0
    spillover_loss: float = 0.0
    direct_low: float = 0.0
    direct_high: float = 0.0
    spillover_low: float = 0.0
    spillover_high: float = 0.0
    total_low_acc: float = 0.0
    total_high_acc: float = 0.0
    weeks: list[str] = field(default_factory=list)
    overlap_weeks: list[str] = field(default_factory=list)
    estimated_shops: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total_loss(self) -> float:
        return self.direct_loss + self.spillover_loss

    @property
    def total_low(self) -> float:
        return max(0.0, self.total_low_acc)

    @property
    def total_high(self) -> float:
        return max(self.total_loss, self.total_high_acc)

    def as_dict(self) -> dict:
        return {
            "阶段": self.phase,
            "直接受阻损失": money(self.direct_loss),
            "外溢损失": money(self.spillover_loss),
            "合计损失": money(self.total_loss),
            "可信区间": [money(self.total_low), money(self.total_high)],
            "直接区间": [money(self.direct_low), money(self.direct_high)],
            "外溢区间": [money(self.spillover_low), money(self.spillover_high)],
            "涉及周": list(self.weeks),
            "重叠施工周": list(self.overlap_weeks),
            "纳入估计店铺数": self.estimated_shops,
            "说明": list(self.notes),
        }


@dataclass
class UnitEstimate:
    shop: str
    owner: str
    street: str
    status: str
    support_eligible: bool
    pre_weeks: list[str] = field(default_factory=list)
    post_weeks: list[str] = field(default_factory=list)
    control_units: list[str] = field(default_factory=list)
    control_weeks: list[str] = field(default_factory=list)
    comparison_conditions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    weekly: list[WeekEstimate] = field(default_factory=list)
    quality: DataQuality = field(default_factory=DataQuality)
    b: float | None = None
    a: float | None = None
    baseline: float | None = None
    control_index: dict[str, float] = field(default_factory=dict)
    route_lost_weeks: list[str] = field(default_factory=list)
    event_excluded_weeks: list[str] = field(default_factory=list)
    phase_totals: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def change_window(self) -> tuple[str, str] | None:
        weeks = [w.week for w in self.weekly if w.change_window_member]
        if not weeks:
            return None
        return (weeks[0], weeks[-1])

    @property
    def supportable_loss_range(self) -> tuple[float, float]:
        if not self.support_eligible:
            return (0.0, 0.0)
        lows = [w.low for w in self.weekly if w.low is not None]
        highs = [w.high for w in self.weekly if w.high is not None]
        return (
            money(sum(max(0.0, x) for x in lows) or 0.0) or 0.0,
            money(sum(max(0.0, x) for x in highs) or 0.0) or 0.0,
        )

    def as_dict(self) -> dict:
        return {
            "店铺": self.shop,
            "经营人": self.owner,
            "街段": self.street,
            "状态": self.status,
            "可支持": self.support_eligible,
            "修缮前周": list(self.pre_weeks),
            "施工周": list(self.post_weeks),
            "对照店铺": list(self.control_units),
            "对照周": list(self.control_weeks),
            "对照条件": list(self.comparison_conditions),
            "原因": list(self.reasons),
            "基线": money(self.baseline),
            "斜率b": None if self.b is None else round(self.b, 4),
            "截距a": None if self.a is None else round(self.a, 4),
            "可支持区间": [money(self.supportable_loss_range[0]), money(self.supportable_loss_range[1])],
            "可解释变化窗口": list(self.change_window) if self.change_window else None,
            "数据质量": self.quality.as_dict(),
            "周估计": [w.as_dict() for w in self.weekly],
            "阶段合计": self.phase_totals,
        }


@dataclass
class EstimationReport:
    units: list[UnitEstimate]
    phase_estimates: dict[str, PhaseEstimate]
    totals: dict[str, float]
    warnings: list[str]
    config: EstimationConfig
    active_weeks: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def unit(self, shop: str, owner: str | None = None) -> UnitEstimate | None:
        matches = [u for u in self.units if u.shop == shop]
        if owner is not None:
            matches = [u for u in matches if u.owner == owner]
        if not matches:
            return None
        if len(matches) > 1 and owner is None:
            return None
        return matches[0]

    def as_dict(self) -> dict:
        return {
            "总额": {k: money(v) for k, v in self.totals.items()},
            "阶段": {k: v.as_dict() for k, v in self.phase_estimates.items()},
            "经营人": [u.as_dict() for u in self.units],
            "警告": list(self.warnings),
            "施工周": {k: list(v) for k, v in self.active_weeks.items()},
        }


def _ols(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = mean(xs), mean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 1e-12:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    a = my - b * mx
    return a, b


def _percentile(values: Sequence[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def classification_insufficient(n_pairs: int, trend_pre_weeks: int) -> bool:
    """共同前周不足以识别趋势时返回 True。"""
    return n_pairs < max(3, trend_pre_weeks)


def _seed_for(*parts: str, base: int) -> int:
    raw = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return (int(digest[:16], 16) ^ base) & 0xFFFFFFFF


class Estimator:
    def __init__(self, dataset: Dataset, config: EstimationConfig | None = None):
        self.ds = dataset
        self.config = config or EstimationConfig()
        self.units: dict[tuple[str, str], list[Observation]] = {}
        for obs in dataset.observations:
            self.units.setdefault(obs.unit, []).append(obs)
        self._unit_streets = {
            unit: next((o.street for o in rows if o.street), "")
            for unit, rows in self.units.items()
        }
        self._warned: set[str] = set()

    # ---------- 公共入口 ----------
    def estimate(self) -> EstimationReport:
        units: list[UnitEstimate] = []
        for unit in sorted(self.units):
            units.append(self._estimate_unit(unit))
        phases = self._aggregate_phases(units)
        totals = self._aggregate_totals(units, phases)
        warnings = list(self.ds.warnings)
        return EstimationReport(
            units=units,
            phase_estimates=phases,
            totals=totals,
            warnings=warnings,
            config=self.config,
            active_weeks=dict(self.ds.active_weeks_by_phase),
        )

    # ---------- 单元序列 ----------
    def _estimate_unit(self, unit: tuple[str, str]) -> UnitEstimate:
        shop, owner = unit
        rows = sorted(self.units[unit], key=lambda o: (week_key(o.trade_week), o.line))
        street = self._unit_streets.get(unit, "")
        quality = DataQuality()
        by_week: dict[str, list[Observation]] = {}
        for o in rows:
            by_week.setdefault(o.trade_week, []).append(o)

        for o in rows:
            if o.kind == "trade":
                quality.trade_weeks += 1
            elif o.kind == "zero":
                quality.zero_weeks += 1
            elif o.kind == "missing":
                quality.missing_weeks += 1
            elif o.kind == "closed_natural":
                quality.natural_closed_weeks += 1
            elif o.kind == "closed_works":
                quality.works_closed_weeks += 1
            if o.settlement_week and o.settlement_week != o.trade_week:
                quality.settlement_lags.append(
                    {"成交周": o.trade_week, "结算周": o.settlement_week, "归属": "按成交周"}
                )

        # 找出该经营人首个受影响周；此前为修缮前窗口。
        roles: dict[str, str] = {}
        for week in by_week:
            role, _, _ = self.ds.role_for(by_week[week][0])
            roles[week] = role
        affected = [
            w for w in sort_weeks(by_week)
            if roles.get(w) in {"direct", "spillover"}
            and (self.config.include_spillover or roles.get(w) != "spillover")
        ]
        all_weeks = sort_weeks(by_week)
        if not affected:
            return UnitEstimate(
                shop=shop, owner=owner, street=street, status="not_affected",
                support_eligible=False, reasons=["账册未显示该经营人在施工期直接受阻或外溢"],
                quality=quality,
            )
        first_affected = min(affected, key=week_key)
        if self.config.use_all_weeks_as_pre:
            pre_candidates = [w for w in all_weeks if w not in set(affected)]
        else:
            pre_candidates = [w for w in all_weeks if week_key(w) < week_key(first_affected)]

        # 店铺转让：承接人若在施工开始后才出现，不能用前手数据做基线，也不转移影响。
        all_owners = {o.owner for o in self.ds.observations if o.shop == shop and o.owner}
        transfer_cut = len(all_owners) > 1 and not pre_candidates
        if transfer_cut:
            quality.transfer_cut = True
            return UnitEstimate(
                shop=shop, owner=owner, street=street, status="owner_transfer_cut",
                support_eligible=False, post_weeks=affected, quality=quality,
                reasons=[
                    "该店铺存在经营人变更，承接人在施工前没有同经营人的可用周记录",
                    "不把前手经营人的影响或基线自动转给承接人；需人工核对承接时点",
                ],
            )

        pre_weeks = [w for w in pre_candidates if self._usable_week(unit, by_week, w)]
        post_weeks = affected

        # 每周的可用金额（trade/zero）；缺报与闭店不填补。
        actual_by_week: dict[str, float | None] = {}
        week_notes: dict[str, list[str]] = {}
        excluded_weeks: set[str] = set()
        route_lost_weeks: list[str] = []
        for week in all_weeks:
            obs_list = by_week[week]
            if len(obs_list) > 1:
                quality.duplicate_weeks += 1
                actual_by_week[week] = None
                week_notes[week] = ["同一经营人同一周存在重复记录，金额按冲突处理"]
                continue
            o = obs_list[0]
            if o.kind in {"trade", "zero"} and o.amount is not None:
                actual_by_week[week] = float(o.amount)
            else:
                actual_by_week[week] = None
                if o.kind == "missing":
                    week_notes[week] = ["缺报：不按零交易处理，也不由对照填补"]
                elif o.kind == "closed_natural":
                    week_notes[week] = ["自然闭店：不计入施工归因损失"]
                elif o.kind == "closed_works":
                    week_notes[week] = ["施工相关闭店：缺少可核营业额，需人工补充证据"]
            if o.route_id:
                state = self.ds.route_state(o.route_id, week)
                if state == "lost":
                    week_notes.setdefault(week, []).append("该周绕行路线不通达")
                    if week in affected:
                        route_lost_weeks.append(week)
                elif state == "unknown":
                    week_notes.setdefault(week, []).append("绕行路线未登记，通达状态未知")
            events = self.ds.events_for(o.street, week)
            if events:
                excluded_weeks.add(week)
                kinds = "、".join(sorted({e.kind for e in events}))
                week_notes.setdefault(week, []).append(
                    f"同期事件（{kinds}）应排除，不把该周变化归因于本街修缮"
                )

        base_values = [actual_by_week[w] for w in pre_weeks if actual_by_week[w] is not None]
        baseline = median(base_values) if base_values else None
        if not pre_weeks and len(all_owners) <= 1:
            return UnitEstimate(
                shop=shop, owner=owner, street=street, status="insufficient_pre",
                support_eligible=False, pre_weeks=pre_weeks, post_weeks=post_weeks,
                quality=quality,
                reasons=["施工前没有该经营人的可用周记录，无法建立基线"],
            )
        if baseline is None or baseline <= 0:
            return UnitEstimate(
                shop=shop, owner=owner, street=street,
                status="no_baseline", support_eligible=False,
                pre_weeks=pre_weeks, post_weeks=post_weeks, quality=quality,
                reasons=["修缮前没有正营业额，无法建立可比较基线；零交易与缺报不相互替代"],
            )

        controls = self._eligible_controls(unit, street, pre_weeks, post_weeks)
        if len(controls) < self.config.min_controls:
            return UnitEstimate(
                shop=shop, owner=owner, street=street, status="no_control",
                support_eligible=False, pre_weeks=pre_weeks, post_weeks=post_weeks,
                baseline=baseline, quality=quality,
                reasons=[
                    "没有满足条件的无干扰对照：同期受绕行/施工影响的店铺已排除",
                    "不对缺少对照的组引用其他街区的变化",
                ],
            )

        if len(pre_weeks) < self.config.min_pre_weeks:
            return UnitEstimate(
                shop=shop, owner=owner, street=street, status="insufficient_pre",
                support_eligible=False, pre_weeks=pre_weeks, post_weeks=post_weeks,
                baseline=baseline,
                control_units=[u[0] if u[1] == "" else f"{u[0]}/{u[1]}" for u in controls],
                quality=quality,
                reasons=[f"修缮前仅有 {len(pre_weeks)} 个可用周，少于最低 {self.config.min_pre_weeks} 周"],
            )

        # 对照指数与趋势拟合。
        use_weeks = sort_weeks(pre_weeks + post_weeks)
        control_index, control_bases, control_used_weeks = self._control_index(
            controls, use_weeks, pre_weeks
        )
        fit = self._fit_target(unit, by_week, pre_weeks, baseline, control_index)
        if fit is None:
            return UnitEstimate(
                shop=shop, owner=owner, street=street, status="no_common_trend",
                support_eligible=False, pre_weeks=pre_weeks, post_weeks=post_weeks,
                baseline=baseline,
                control_units=[f"{u[0]}/{u[1]}" for u in controls],
                control_weeks=control_used_weeks, quality=quality,
                reasons=["修缮前与对照缺少共同支撑：对照指数在这些周没有变化或无法对齐"],
            )
        a, b, pre_pairs, residual_drift, trend_status = fit

        support_eligible = trend_status in {"ok", "limited_pre_support", "level_only"}
        reasons: list[str] = []
        if trend_status == "no_common_trend":
            reasons.append("修缮前趋势与对照分歧超出容忍度，不能把对照变化迁移到本店")
            reasons.append("结论仅作敏感性参考，不直接支持缓冲金额")
        elif trend_status == "limited_pre_support":
            reasons.append("修缮前共同周较少，区间已放宽，建议补报后再核")
        elif trend_status == "level_only":
            reasons.append("对照在修缮前没有变化，只能作水平基准；趋势变化不得迁移")
        if len(pre_weeks) < self.config.trend_pre_weeks:
            support_eligible = support_eligible and len(pre_weeks) >= self.config.min_pre_weeks
            reasons.append(
                f"修缮前仅 {len(pre_weeks)} 周，趋势识别能力有限"
            )

        # 逐周反事实与自助可信区间。
        weekly, phase_draws, weekly_draws = self._week_effects(
            unit=unit, by_week=by_week, post_weeks=post_weeks, actual_by_week=actual_by_week,
            week_notes=week_notes, baseline=baseline, a=a, b=b,
            pre_pairs=pre_pairs, control_index=control_index, controls=controls,
            pre_weeks=pre_weeks,
        )
        phase_totals: dict[str, dict[str, float]] = {}
        for week in weekly:
            if not support_eligible:
                # 无共同趋势的敏感性结果只作参考，不进入可支持额度汇总。
                break
            for phase in week.phases:
                bucket = phase_totals.setdefault(
                    phase, {"direct": 0.0, "spillover": 0.0, "low": 0.0, "high": 0.0, "weeks": 0}
                )
                alloc = week.phase_allocation.get(phase, 0.0)
                if week.role == "spillover":
                    bucket["spillover"] += max(0.0, alloc)
                else:
                    bucket["direct"] += max(0.0, alloc)
                bucket["weeks"] += 1

        conditions = [
            "对照为同期未直接受阻、未受绕行外溢的店铺",
            f"对照店铺 {len(controls)} 家；共同修缮前周 {len(control_used_weeks)} 个",
            "金额按成交周归属，结算周不同不影响归属",
            "缺报、自然闭店、营业但零交易分别统计；缺报与闭店不按零交易填补",
            "同期节日/邻街先封等事件周已从对照与基线中剔除",
            "多个施工阶段重叠时，同一周损失只计一次，再按阶段等分展示",
        ]
        if quality.settlement_lags:
            conditions.append("存在成交与结算不同周，已按成交周归属")
        if any(week.role == "spillover" for week in weekly):
            conditions.append("外溢周按仍营业侧街的可到达性改变单列，不并入直接受阻")

        unit_est = UnitEstimate(
            shop=shop, owner=owner, street=street,
            status="estimated" if support_eligible else "estimated_sensitivity_only",
            support_eligible=support_eligible, pre_weeks=pre_weeks, post_weeks=post_weeks,
            control_units=[f"{u[0]}/{u[1]}" if u[1] else u[0] for u in controls],
            control_weeks=control_used_weeks, comparison_conditions=conditions,
            reasons=reasons, weekly=weekly, quality=quality, a=a, b=b, baseline=baseline,
            control_index=dict(control_index), phase_totals=phase_totals,
            route_lost_weeks=sort_weeks(route_lost_weeks),
            event_excluded_weeks=sort_weeks(excluded_weeks),
        )
        self._attach_draws(unit_est, phase_draws, weekly_draws)
        return unit_est

    # ---------- 可用性与对照 ----------
    def _usable_week(self, unit: tuple[str, str], by_week: Mapping[str, list[Observation]], week: str) -> bool:
        rows = by_week.get(week, [])
        if len(rows) != 1:
            return False
        o = rows[0]
        if o.kind not in {"trade", "zero"} or o.amount is None:
            return False
        role, _, _ = self.ds.role_for(o)
        if role != "unaffected":
            return False
        if not self.ds.is_clean_week(o.street, week):
            return False
        if o.route_id and not self.ds.route_usable(o.route_id, week):
            return False
        return True

    def _eligible_controls(
        self, target: tuple[str, str], street: str, pre_weeks: Sequence[str], post_weeks: Sequence[str]
    ) -> list[tuple[str, str]]:
        controls: list[tuple[str, str]] = []
        post_set = set(post_weeks)
        for unit, rows in self.units.items():
            if unit == target:
                continue
            by_week: dict[str, list[Observation]] = {}
            for o in rows:
                by_week.setdefault(o.trade_week, []).append(o)
            # 任何目标施工周受到施工/绕行影响的店铺不得作为对照。
            affected_in_post = False
            for week in post_set:
                rows_w = by_week.get(week)
                if not rows_w:
                    continue
                role, _, _ = self.ds.role_for(rows_w[0])
                if role in {"direct", "spillover", "unknown"}:
                    affected_in_post = True
                    break
            if affected_in_post:
                continue
            shared = 0
            for week in pre_weeks:
                rows_w = by_week.get(week)
                if rows_w and self._usable_week(unit, by_week, week):
                    shared += 1
            if shared >= 1:
                controls.append(unit)
        return controls

    def _control_index(
        self, controls: Sequence[tuple[str, str]], weeks: Sequence[str], pre_weeks: Sequence[str]
    ) -> tuple[dict[str, float], dict[tuple[str, str], float], list[str]]:
        pre_set = set(pre_weeks)
        bases: dict[tuple[str, str], float] = {}
        for unit in controls:
            vals = [
                o.amount for o in self.units[unit]
                if o.trade_week in pre_set and o.kind in {"trade", "zero"} and o.amount is not None
                and self._usable_week(unit, self._by_week_cache(unit), o.trade_week)
            ]
            if vals:
                base = median(vals)
                if base > 0:
                    bases[unit] = base
        index: dict[str, float] = {}
        used_weeks: list[str] = []
        for week in weeks:
            ratios = []
            for unit in bases:
                bw = self._by_week_cache(unit)
                obs = bw.get(week, [])
                if len(obs) != 1:
                    continue
                o = obs[0]
                if o.kind not in {"trade", "zero"} or o.amount is None:
                    continue
                if not self._usable_week(unit, bw, week):
                    continue
                ratios.append(float(o.amount) / bases[unit])
            if ratios:
                index[week] = median(ratios)
                used_weeks.append(week)
        return index, bases, used_weeks

    def _by_week_cache(self, unit: tuple[str, str]) -> dict[str, list[Observation]]:
        cache = getattr(self, "_bw_cache", None)
        if cache is None:
            cache = {}
            self._bw_cache = cache
        if unit not in cache:
            bw: dict[str, list[Observation]] = {}
            for o in self.units[unit]:
                bw.setdefault(o.trade_week, []).append(o)
            cache[unit] = bw
        return cache[unit]

    def _fit_target(
        self, unit: tuple[str, str], by_week: Mapping[str, list[Observation]],
        pre_weeks: Sequence[str], baseline: float, control_index: Mapping[str, float],
    ) -> tuple[float, float, list[tuple[str, float, float]], float, str] | None:
        pairs: list[tuple[str, float, float]] = []
        for week in pre_weeks:
            if week not in control_index:
                continue
            rows = by_week.get(week, [])
            if len(rows) != 1 or rows[0].amount is None or baseline <= 0:
                continue
            pairs.append((week, control_index[week], float(rows[0].amount) / baseline))
        if len(pairs) < 2:
            return None
        xs = [p[1] for p in pairs]
        ys = [p[2] for p in pairs]
        tol = self.config.trend_tolerance
        target_drift = (ys[-1] - ys[0]) / max(1e-9, abs(mean(ys)))
        control_drift = (xs[-1] - xs[0]) / max(1e-9, abs(mean(xs)))
        control_var = sum((x - mean(xs)) ** 2 for x in xs)

        if classification_insufficient(len(pairs), self.config.trend_pre_weeks):
            # 共同前周不足以估计斜率：只用水平/比例基准，区间自然更宽。
            return 0.0, 1.0, pairs, abs(target_drift - control_drift), "limited_pre_support"

        if control_var <= 1e-12:
            # 对照在修缮前没有变化：只能作水平基准，无法提供趋势支撑。
            if abs(target_drift) > tol:
                return 0.0, 1.0, pairs, abs(target_drift), "no_common_trend"
            return 0.0, 1.0, pairs, abs(target_drift), "level_only"

        # 方向相反且本店变化超过容忍度时，对照无法解释本店趋势。
        if (target_drift * control_drift < 0
                and abs(target_drift) > tol
                and abs(target_drift - control_drift) > tol):
            return 0.0, 1.0, pairs, abs(target_drift - control_drift), "no_common_trend"

        fit = _ols(xs, ys)
        if fit is None:
            return None
        a, b = fit
        resid = [y - (a + b * x) for _, x, y in pairs]
        drift = abs(mean(resid)) / max(0.1, abs(mean(ys)))
        status = "ok" if len(pairs) >= self.config.trend_pre_weeks + 1 else "limited_pre_support"
        if drift > tol:
            status = "no_common_trend"
        # 斜率方向与对照相反且幅度显著，也视为缺少共同趋势。
        if b < 0 and control_drift > tol and abs(target_drift) > tol:
            status = "no_common_trend"
        return a, b, pairs, drift, status

    # ---------- 周效应与自助法 ----------
    def _week_effects(
        self, unit: tuple[str, str], by_week: Mapping[str, list[Observation]],
        post_weeks: Sequence[str], actual_by_week: Mapping[str, float | None],
        week_notes: Mapping[str, list[str]], baseline: float, a: float, b: float,
        pre_pairs: Sequence[tuple[str, float, float]],
        control_index: Mapping[str, float], controls: Sequence[tuple[str, str]],
        pre_weeks: Sequence[str],
    ) -> tuple[list[WeekEstimate], dict[str, dict[str, list[float]]], dict[str, list[float]]]:
        cfg = self.config
        rng = random.Random(_seed_for(unit[0], unit[1], base=cfg.seed))
        draws = max(0, cfg.bootstrap)
        # 预拟合残差，用于自助重抽。
        resid = [y - (a + b * x) for _, x, y in pre_pairs]
        resid_center = mean(resid) if resid else 0.0
        resid_scale = mad(resid) or 0.0

        week_draws: dict[str, list[float]] = {w: [] for w in post_weeks}
        phase_draws: dict[str, dict[str, list[float]]] = {}

        # 对照周指数在自助中的不确定性：先准备每个对照的周比值。
        control_values: dict[tuple[str, str], dict[str, float]] = {}
        for cu in controls:
            bw = self._by_week_cache(cu)
            vals = [o.amount for o in self.units[cu] if o.trade_week in set(pre_weeks)
                    and o.kind in {"trade", "zero"} and o.amount is not None
                    and self._usable_week(cu, bw, o.trade_week)]
            base = median(vals) if vals else None
            if base and base > 0:
                ratios: dict[str, float] = {}
                for week in post_weeks:
                    rows = bw.get(week, [])
                    if len(rows) == 1 and rows[0].kind in {"trade", "zero"} and rows[0].amount is not None:
                        if self._usable_week(cu, bw, week):
                            ratios[week] = float(rows[0].amount) / base
                control_values[cu] = ratios

        for it in range(draws):
            sampled_controls = [rng.choice(controls) for _ in controls] if controls else []
            # 按抽样对照重算当周中位指数。
            boot_index: dict[str, float] = {}
            for week in post_weeks:
                vals = [control_values[c][week] for c in sampled_controls if week in control_values.get(c, {})]
                if vals:
                    boot_index[week] = median(vals)
            # 重抽残差后重拟合前趋势。
            boot_y = []
            for (_, x, y) in pre_pairs:
                e = rng.choice(resid) - resid_center if resid else 0.0
                boot_y.append(y + e)
            fit = _ols([p[1] for p in pre_pairs], boot_y) if len(pre_pairs) >= 2 else None
            ba, bb = fit if fit else (a, b)
            draw_phase_totals: dict[str, dict[str, float]] = {}
            for week in post_weeks:
                if week not in control_index:
                    continue
                c = boot_index.get(week, control_index[week])
                cf = baseline * (ba + bb * c)
                actual = actual_by_week.get(week)
                if actual is None:
                    continue
                loss = cf - actual
                week_draws[week].append(loss)
                phases = self.ds.active_phases(week)
                if phases:
                    share = 1.0 / len(phases)
                    role = self._role_for_week(unit, by_week, week)
                    for phase in phases:
                        bucket = draw_phase_totals.setdefault(
                            phase, {"direct": 0.0, "spillover": 0.0}
                        )
                        bucket[role if role in bucket else "direct"] += loss * share
            for phase, bucket in draw_phase_totals.items():
                target = phase_draws.setdefault(phase, {"direct": [], "spillover": []})
                target["direct"].append(bucket["direct"])
                target["spillover"].append(bucket["spillover"])

        weekly: list[WeekEstimate] = []
        for week in post_weeks:
            rows = by_week.get(week, [])
            role = self._role_for_week(unit, by_week, week)
            phases = self.ds.active_phases(week)
            actual = actual_by_week.get(week)
            notes = list(week_notes.get(week, []))
            street = self._unit_streets.get(unit, "")
            if self.ds.events_for(street, week):
                weekly.append(WeekEstimate(
                    week=week, role=role, phases=phases, actual=actual,
                    counterfactual=None, effect=None, low=None, high=None,
                    status="excluded_concurrent_event", supportable_loss=0.0, notes=notes,
                ))
                continue
            if actual is None or week not in control_index:
                if actual is not None and week not in control_index:
                    notes.append("该周没有可用对照，不能估计反事实")
                weekly.append(WeekEstimate(
                    week=week, role=role, phases=phases, actual=actual,
                    counterfactual=None, effect=None, low=None, high=None,
                    status="not_estimable", supportable_loss=0.0, notes=notes,
                ))
                continue
            cf = baseline * (a + b * control_index[week])
            effect = cf - actual
            arr = week_draws.get(week, [])
            if arr:
                alpha = (1 - cfg.confidence) / 2
                low = _percentile(arr, alpha)
                high = _percentile(arr, 1 - alpha)
            else:
                low = high = effect
            # 数值下限：前趋势残差噪音与反事实比例的最小不确定性。
            floor = max(cfg.uncertainty_floor * abs(cf), 1.4826 * resid_scale * baseline)
            if floor > 0:
                low = min(low, effect - floor) if low is not None else effect - floor
                high = max(high, effect + floor) if high is not None else effect + floor
            if effect > 0 and low is not None and low <= 0:
                notes.append("点估计为损失，但可信下限未越过零，列为待观察变化")
            if effect < 0:
                notes.append("该周估计为正影响，不与其他周损失相抵，也不自动冲减支持额度")
            alloc = {phase: effect / len(phases) for phase in phases} if phases else {}
            supportable = max(0.0, effect) if self._role_for_week(unit, by_week, week) in {"direct", "spillover"} else 0.0
            weekly.append(WeekEstimate(
                week=week, role=role, phases=phases, actual=actual, counterfactual=cf,
                effect=effect, low=low, high=high, status="estimated",
                supportable_loss=supportable, notes=notes, phase_allocation=alloc,
            ))

        # 没有自助抽样时，阶段区间退化为逐周区间求和。
        if not draws:
            for week in weekly:
                for phase in week.phases:
                    bucket = phase_draws.setdefault(phase, {"direct": [], "spillover": []})
                    bucket[week.role if week.role in bucket else "direct"].append(
                        max(0.0, week.effect or 0.0) / len(week.phases)
                    )
        return weekly, phase_draws, week_draws

    def _role_for_week(self, unit: tuple[str, str], by_week: Mapping[str, list[Observation]], week: str) -> str:
        rows = by_week.get(week, [])
        if not rows:
            return "unknown"
        role, _, _ = self.ds.role_for(rows[0])
        return role

    def _attach_draws(
        self, unit_est: UnitEstimate, phase_draws: dict[str, dict[str, list[float]]],
        weekly_draws: dict[str, list[float]],
    ) -> None:
        """阶段合计由逐周证据汇总，区间沿用已加噪声下限的周区间。

        阶段金额只作存在重叠周时的展示分摊；周与周之间、阶段之间不重复
        累计同一周损失。
        """
        phases = {phase for week in unit_est.weekly for phase in week.phases}
        for phase in phases:
            direct = spillover = 0.0
            direct_low = direct_high = 0.0
            spillover_low = spillover_high = 0.0
            for week in unit_est.weekly:
                if phase not in week.phases:
                    continue
                share = 1.0 / len(week.phases)
                point = max(0.0, week.effect or 0.0) * share
                low = max(0.0, week.low or 0.0) * share
                high = max(0.0, week.high or 0.0) * share
                if week.role == "spillover":
                    spillover += point
                    spillover_low += low
                    spillover_high += high
                else:
                    direct += point
                    direct_low += low
                    direct_high += high
            unit_est.phase_totals[phase] = {
                "direct": direct,
                "spillover": spillover,
                "low": direct_low + spillover_low,
                "high": direct_high + spillover_high,
                "direct_low": direct_low,
                "direct_high": direct_high,
                "spillover_low": spillover_low,
                "spillover_high": spillover_high,
                "weeks": sum(1 for w in unit_est.weekly if phase in w.phases),
            }
        # 逐周区间已在自助阶段写入；此处仅兜底。
        for week in unit_est.weekly:
            if week.effect is None:
                continue
            arr = weekly_draws.get(week.week, [])
            if arr and week.low is None:
                week.low = _percentile(arr, alpha)
                week.high = _percentile(arr, 1 - alpha)

    # ---------- 汇总 ----------
    def _aggregate_phases(self, units: Sequence[UnitEstimate]) -> dict[str, PhaseEstimate]:
        out: dict[str, PhaseEstimate] = {}
        for unit in units:
            if not unit.support_eligible:
                continue
            for phase, data in unit.phase_totals.items():
                est = out.setdefault(phase, PhaseEstimate(phase=phase))
                est.direct_loss += data.get("direct", 0.0)
                est.spillover_loss += data.get("spillover", 0.0)
                est.direct_low += data.get("direct_low", 0.0)
                est.direct_high += data.get("direct_high", 0.0)
                est.spillover_low += data.get("spillover_low", 0.0)
                est.spillover_high += data.get("spillover_high", 0.0)
                est.total_low_acc += data.get("low", 0.0)
                est.total_high_acc += data.get("high", 0.0)
                est.estimated_shops += 1
                for week in unit.weekly:
                    if phase in week.phases:
                        if week.week not in est.weeks:
                            est.weeks.append(week.week)
                        if len(week.phases) > 1 and week.week not in est.overlap_weeks:
                            est.overlap_weeks.append(week.week)
        for est in out.values():
            est.weeks = sort_weeks(est.weeks)
            est.overlap_weeks = sort_weeks(est.overlap_weeks)
            est.direct_loss = money(est.direct_loss) or 0.0
            est.spillover_loss = money(est.spillover_loss) or 0.0
            est.direct_low = money(est.direct_low) or 0.0
            est.direct_high = money(est.direct_high) or 0.0
            est.spillover_low = money(est.spillover_low) or 0.0
            est.spillover_high = money(est.spillover_high) or 0.0
            est.total_low_acc = money(est.total_low_acc) or 0.0
            est.total_high_acc = money(est.total_high_acc) or 0.0
            if est.overlap_weeks:
                est.notes.append("存在多阶段重叠周；损失按周只计一次，阶段金额为等分展示，不可相加")
        return out

    def _aggregate_totals(
        self, units: Sequence[UnitEstimate], phases: Mapping[str, PhaseEstimate]
    ) -> dict[str, float]:
        total_loss = sum(p.total_loss for p in phases.values())
        total_low = sum(p.total_low for p in phases.values())
        total_high = sum(p.total_high for p in phases.values())
        gross_gain = 0.0
        supportable_units = 0
        for unit in units:
            if not unit.support_eligible:
                continue
            supportable_units += 1
            for week in unit.weekly:
                if week.effect is not None and week.effect < 0:
                    gross_gain += -week.effect
        return {
            "可归因损失点估计": money(total_loss) or 0.0,
            "可支持损失下限": money(total_low) or 0.0,
            "可支持损失上限": money(total_high) or 0.0,
            "未抵扣的正影响": money(gross_gain) or 0.0,
            "可支持经营人数": float(supportable_units),
            "纳入估计经营人数": float(len(units)),
        }
