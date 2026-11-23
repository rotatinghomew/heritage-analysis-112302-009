"""分期方案比较：覆盖缺口与预计缓冲占用，不直接发放款项。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .dataset import Dataset
from .estimation import EstimationReport
from .util import inclusive_weeks, money, parse_ratio, period_range, sort_weeks, text


@dataclass
class ScenarioPhase:
    phase: str
    street: str
    start_week: str
    end_week: str
    effect_ratio: float = 1.0
    detour_ratio: float = 1.0
    capacity: float | None = None
    note: str = ""

    @property
    def weeks(self) -> tuple[str, str]:
        return self.start_week, self.end_week


@dataclass
class Scenario:
    name: str
    phases: list[ScenarioPhase]
    note: str = ""


@dataclass
class ScenarioResult:
    name: str
    note: str
    affected_shops: list[str] = field(default_factory=list)
    uncovered_weeks: list[str] = field(default_factory=list)
    uncovered_shops: list[dict] = field(default_factory=list)
    overlap_weeks: list[str] = field(default_factory=list)
    estimated_occupancy: float = 0.0
    occupancy_low: float = 0.0
    occupancy_high: float = 0.0
    by_phase: dict[str, dict] = field(default_factory=dict)
    by_street: dict[str, dict] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "方案": self.name,
            "说明": self.note,
            "受影响店铺": list(self.affected_shops),
            "覆盖缺口周": list(self.uncovered_weeks),
            "未覆盖影响": list(self.uncovered_shops),
            "重叠施工周": list(self.overlap_weeks),
            "预计缓冲占用": money(self.estimated_occupancy),
            "占用区间": [money(self.occupancy_low), money(self.occupancy_high)],
            "按阶段": self.by_phase,
            "按街段": self.by_street,
            "假设": list(self.assumptions),
        }


def load_scenarios(path: str) -> list[Scenario]:
    """读取方案 CSV。

    允许两种格式：

    1. ``scenario,phase,street,start_week,end_week,effect_ratio,detour_ratio,capacity,note``
    2. ``scenario,phase,street,period,...``（period 形如 ``W3-W6``）
    """
    import csv
    from pathlib import Path

    scenarios: dict[str, Scenario] = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            clean = {text(k).lstrip("﻿"): text(v) for k, v in row.items() if k}
            name = clean.get("scenario") or clean.get("方案") or "未命名"
            phase = clean.get("phase") or clean.get("阶段") or "P"
            street = clean.get("street") or clean.get("街段") or ""
            start = clean.get("start_week") or clean.get("start") or clean.get("起始周") or ""
            end = clean.get("end_week") or clean.get("end") or clean.get("结束周") or ""
            period = clean.get("period") or clean.get("覆盖期") or ""
            if not start and period:
                start, end = period_range(period)
                end = end or start
            scenario = scenarios.setdefault(
                name, Scenario(name=name, phases=[], note=clean.get("note") or clean.get("说明") or "")
            )
            scenario.phases.append(ScenarioPhase(
                phase=phase, street=street, start_week=start, end_week=end or start,
                effect_ratio=parse_ratio(clean.get("effect_ratio") or clean.get("影响比例"), 1.0) or 0.0,
                detour_ratio=parse_ratio(clean.get("detour_ratio") or clean.get("绕行比例"), 1.0) or 0.0,
                capacity=parse_ratio(clean.get("capacity") or clean.get("容量")),
                note=clean.get("phase_note") or "",
            ))
    return list(scenarios.values())


class ScenarioPlanner:
    def __init__(self, dataset: Dataset, estimation: EstimationReport):
        self.ds = dataset
        self.est = estimation

    # ---------- 模拟 ----------
    def evaluate(self, scenario: Scenario) -> ScenarioResult:
        result = ScenarioResult(name=scenario.name, note=scenario.note)
        result.assumptions.extend([
            "模拟只用于比较尚未执行的分期方案，不产生发放结论",
            "店铺影响按观测到的角色与周区间比例缩放，不假设新店铺进入",
            "覆盖缺口指有可归因影响但当前无有效通知覆盖的经营周",
            "重叠阶段同一周只计一次占用",
        ])
        notices = self.ds.notices
        covered = self._covered_keys(notices)
        phase_by_week: dict[str, list[ScenarioPhase]] = {}
        per_unit_week: dict[tuple[str, str, str], float] = {}
        per_unit_week_high: dict[tuple[str, str, str], float] = {}
        phase_alloc: dict[str, dict] = {}
        street_alloc: dict[tuple[str, str, str], tuple[float, float]] = {}
        matched_phases: dict[tuple[str, str, str], list[ScenarioPhase]] = {}

        # 先确定每个经营周在各阶段下的占用，再按周去重；阶段与街段金额取
        # 去重后的展示分摊，保证“按阶段/按街段相加”不超过去重占用。
        for unit in self.est.units:
            if not unit.support_eligible:
                continue
            for week in unit.weekly:
                if (week.effect or 0) <= 0:
                    continue
                matches: list[tuple[ScenarioPhase, float, float]] = []
                for phase in scenario.phases:
                    if week.week not in set(self._weeks(phase)):
                        continue
                    if phase.street and unit.street and phase.street != unit.street:
                        if week.role != "spillover":
                            continue
                    ratio = phase.effect_ratio
                    if week.role == "spillover":
                        ratio *= phase.detour_ratio
                    matches.append((
                        phase,
                        max(0.0, week.low or 0.0) * ratio,
                        max(0.0, week.high or 0.0) * ratio,
                    ))
                if not matches:
                    continue
                share = 1.0 / len(matches)
                total_low = max(v for _, v, _ in matches)
                total_high = max(h for _, _, h in matches)
                key = (unit.shop, unit.owner, week.week)
                per_unit_week[key] = total_low
                per_unit_week_high[key] = total_high
                matched_phases[key] = [p for p, _, _ in matches]
                if unit.shop not in result.affected_shops:
                    result.affected_shops.append(unit.shop)
                for phase, _, _ in matches:
                    bucket = phase_alloc.setdefault(
                        phase.phase, {"下限": 0.0, "上限": 0.0, "店铺": []}
                    )
                    # 重叠阶段按等分展示，避免阶段金额相加超过总额。
                    bucket["下限"] += total_low * share
                    bucket["上限"] += total_high * share
                    if unit.shop not in bucket["店铺"]:
                        bucket["店铺"].append(unit.shop)
                for phase in {p.phase: p for p, _, _ in matches}.values():
                    street_key = (phase.phase, phase.street or "未指定", unit.shop)
                    low_acc, high_acc = street_alloc.get(street_key, (0.0, 0.0))
                    street_alloc[street_key] = (
                        low_acc + total_low * share,
                        high_acc + total_high * share,
                    )

        for week in {w for p in scenario.phases for w in self._weeks(p)}:
            matched = [p for p in scenario.phases if week in set(self._weeks(p))]
            phase_by_week[week] = matched

        result.occupancy_low = sum(per_unit_week.values())
        result.occupancy_high = sum(per_unit_week_high.values())
        result.estimated_occupancy = result.occupancy_low
        result.overlap_weeks = sort_weeks(
            [w for w, phases in phase_by_week.items() if len(phases) > 1]
        )

        # 覆盖缺口：模拟影响周中，当前通知未覆盖的经营周。
        for unit in self.est.units:
            if not unit.support_eligible:
                continue
            for week in unit.weekly:
                if (week.effect or 0) <= 0:
                    continue
                in_scope = any(
                    week.week in self._weeks(p)
                    and (not p.street or not unit.street or p.street == unit.street
                         or week.role == "spillover")
                    for p in scenario.phases
                )
                if not in_scope:
                    continue
                if (unit.shop, unit.owner, week.week) in covered:
                    continue
                if week.week not in result.uncovered_weeks:
                    result.uncovered_weeks.append(week.week)
                result.uncovered_shops.append({
                    "店铺": unit.shop,
                    "经营人": unit.owner,
                    "周": week.week,
                    "角色": week.role,
                    "可支持下限": money(week.low),
                    "可支持上限": money(week.high),
                })
        result.uncovered_weeks = sort_weeks(result.uncovered_weeks)
        result.affected_shops.sort()
        result.by_phase = {
            k: {"下限": money(v["下限"]), "上限": money(v["上限"]), "店铺": sorted(v["店铺"])}
            for k, v in phase_alloc.items()
        }
        # 街段维度按去重后的展示分摊汇总，保持与阶段分摊同口径。
        by_street: dict[str, dict[str, float]] = {}
        for (_, street, _), (low, high) in street_alloc.items():
            bucket = by_street.setdefault(street, {"下限": 0.0, "上限": 0.0})
            bucket["下限"] += low
            bucket["上限"] += high
        result.by_street = {
            k: {"下限": money(v["下限"]), "上限": money(v["上限"])}
            for k, v in by_street.items()
        }
        return result

    def _weeks(self, phase: ScenarioPhase) -> list[str]:
        universe = self.ds.weeks
        if not universe:
            return []
        return inclusive_weeks(phase.start_week, phase.end_week, universe)

    def _covered_keys(self, notices: Sequence) -> set[tuple[str, str, str]]:
        keys: set[tuple[str, str, str]] = set()
        for notice in notices:
            if notice.status in {"cancelled", "rejected"}:
                continue
            for week in self.ds.covered_weeks(notice):
                keys.add((notice.shop, notice.owner, week))
                if not notice.owner:
                    for unit in self.est.units:
                        if unit.shop == notice.shop:
                            keys.add((unit.shop, unit.owner, week))
        return keys

    # ---------- 比较 ----------
    def compare(self, scenarios: Sequence[Scenario]) -> dict:
        results = [self.evaluate(s) for s in scenarios]
        rows = [r.as_dict() for r in results]
        return {
            "比较": rows,
            "建议": self._recommend(results),
            "说明": "仅用于排期与预算比较；不得据模拟结果直接发放款项",
        }

    def _recommend(self, results: Sequence[ScenarioResult]) -> list[str]:
        if not results:
            return []
        notes: list[str] = []
        best_gap = min(results, key=lambda r: (len(r.uncovered_weeks), r.estimated_occupancy))
        notes.append(
            f"覆盖缺口最小：{best_gap.name}（缺口 {len(best_gap.uncovered_weeks)} 周，"
            f"预计占用 {money(best_gap.estimated_occupancy)}）"
        )
        best_cost = min(results, key=lambda r: (r.estimated_occupancy, len(r.uncovered_weeks)))
        notes.append(
            f"预计占用最低：{best_cost.name}（{money(best_cost.estimated_occupancy)}）"
        )
        for r in results:
            if r.overlap_weeks:
                notes.append(f"{r.name} 有 {len(r.overlap_weeks)} 个重叠施工周，占用已按周去重")
        return notes
