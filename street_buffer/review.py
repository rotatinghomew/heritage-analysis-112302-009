"""缓冲通知审核、补报重审与经营人视图。

审核同时核对两类证据：本期可归因影响能支持的金额范围，以及通知本身的
覆盖日期、对象和已支付承诺。协议要求：

* 已签通知的未支付金额可重算，已支付金额只可提交复议，不得自动冲回；
* 多个施工阶段同时影响同一经营周时，同一影响只计一次；
* 补报若改变可用对照或使绕行路线失去通达资格，要区分可以更正的新申请、
  仅调整未兑现部分的已通知申请、以及原结论失效但金额须人工复议的情形。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .dataset import Dataset, Notice
from .estimation import EstimationReport, UnitEstimate, WeekEstimate
from .util import money, sort_weeks, week_key


DECISIONS = {
    "confirm": "维持",
    "adjust_unpaid": "仅调整未兑现部分",
    "correctable_new": "可更正的新申请",
    "supplement_new": "可就差额提交新申请",
    "invalidated_manual": "原结论失效，转人工复议",
    "paid_excess_manual": "已付超出可支持范围，转人工复议",
    "duplicate_cover": "与其他通知重叠，不重复计入支持额度",
    "manual_review": "人工复核",
    "not_supported": "无可用依据，不予支持",
    "outside_period": "覆盖期不在可归因影响窗口",
    "pre_period_only": "覆盖期仅含修缮前周，无施工影响",
    "object_mismatch": "通知对象与账册经营人不符",
}


@dataclass
class NoticeReview:
    notice_id: str
    shop: str
    owner: str
    period: str
    notice_amount: float | None
    paid: float
    unpaid: float
    notice_status: str
    covered_weeks: list[str] = field(default_factory=list)
    unit_key: tuple[str, str] | None = None
    unit_status: str = ""
    unit_owner: str = ""
    owner_match: bool = True
    route_affected: bool = False
    event_excluded_weeks: list[str] = field(default_factory=list)
    impact_weeks: list[str] = field(default_factory=list)
    uncovered_impact_weeks: list[str] = field(default_factory=list)
    covered_non_impact_weeks: list[str] = field(default_factory=list)
    overlap_weeks: list[str] = field(default_factory=list)
    duplicate_weeks: list[str] = field(default_factory=list)
    support_low: float = 0.0
    support_high: float = 0.0
    allocated_low: float = 0.0
    allocated_high: float = 0.0
    control_units: list[str] = field(default_factory=list)
    control_weeks: list[str] = field(default_factory=list)
    route_lost_weeks: list[str] = field(default_factory=list)
    coverage_state: str = ""
    decision: str = ""
    reasons: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    remaining_unknown: list[str] = field(default_factory=list)

    @property
    def supported_ceiling(self) -> float:
        return max(0.0, self.allocated_high)

    @property
    def supported_floor(self) -> float:
        return max(0.0, self.allocated_low)

    @property
    def gap_to_ceiling(self) -> float:
        if self.notice_amount is None:
            return self.supported_ceiling
        return max(0.0, self.supported_ceiling - self.notice_amount)

    @property
    def excess_over_ceiling(self) -> float:
        if self.notice_amount is None:
            return 0.0
        return max(0.0, self.notice_amount - self.supported_ceiling)

    @property
    def paid_invalid(self) -> bool:
        return self.paid > self.supported_ceiling + 1e-9

    def as_dict(self) -> dict:
        return {
            "通知": self.notice_id,
            "店铺": self.shop,
            "经营人": self.owner,
            "对象匹配": self.owner_match,
            "绕行路线影响": self.route_affected,
            "覆盖期": self.period,
            "覆盖周": list(self.covered_weeks),
            "通知金额": money(self.notice_amount),
            "已支付": money(self.paid),
            "未支付": money(self.unpaid),
            "通知状态": self.notice_status,
            "经营人状态": self.unit_status,
            "可支持下限": money(self.supported_floor),
            "可支持上限": money(self.supported_ceiling),
            "去重后下限": money(self.allocated_low),
            "去重后上限": money(self.allocated_high),
            "影响周": list(self.impact_weeks),
            "未覆盖影响周": list(self.uncovered_impact_weeks),
            "覆盖但无影响周": list(self.covered_non_impact_weeks),
            "重叠施工周": list(self.overlap_weeks),
            "重复覆盖周": list(self.duplicate_weeks),
            "覆盖判断": self.coverage_state,
            "对照店铺": list(self.control_units),
            "对照周": list(self.control_weeks),
            "失去通达的周": list(self.route_lost_weeks),
            "审核结论": DECISIONS.get(self.decision, self.decision),
            "结论代码": self.decision,
            "依据": list(self.reasons),
            "可进入的下一步": list(self.actions),
            "仍未知": list(self.remaining_unknown),
        }


@dataclass
class Reassessment:
    shop: str
    owner: str
    notice_id: str | None
    previous_decision: str
    new_decision: str
    previous_supported: tuple[float, float]
    new_supported: tuple[float, float]
    paid: float
    unpaid: float
    trigger: str
    reason: str
    action: str
    status: str = ""

    def as_dict(self) -> dict:
        return {
            "店铺": self.shop,
            "经营人": self.owner,
            "通知": self.notice_id,
            "原结论": DECISIONS.get(self.previous_decision, self.previous_decision),
            "新结论": DECISIONS.get(self.new_decision, self.new_decision),
            "原可支持区间": [money(self.previous_supported[0]), money(self.previous_supported[1])],
            "新可支持区间": [money(self.new_supported[0]), money(self.new_supported[1])],
            "已支付": money(self.paid),
            "未支付": money(self.unpaid),
            "触发": self.trigger,
            "说明": self.reason,
            "处理": self.action,
            "类型": self.status,
        }


@dataclass
class ReviewReport:
    notices: list[NoticeReview]
    unclaimed: list[dict]
    summary: dict
    unit_statuses: dict[tuple[str, str], str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def for_notice(self, notice_id: str) -> NoticeReview | None:
        for r in self.notices:
            if r.notice_id == notice_id:
                return r
        return None

    def as_dict(self) -> dict:
        return {
            "汇总": self.summary,
            "通知审核": [n.as_dict() for n in self.notices],
            "未获通知的影响": list(self.unclaimed),
            "警告": list(self.warnings),
        }


def _sum_range(weeks: Iterable[WeekEstimate]) -> tuple[float, float]:
    low = sum(max(0.0, w.low or 0.0) for w in weeks)
    high = sum(max(0.0, w.high or 0.0) for w in weeks)
    return low, high


class ReviewEngine:
    def __init__(self, dataset: Dataset, estimation: EstimationReport):
        self.ds = dataset
        self.est = estimation
        self._unit_by_shop: dict[str, list[UnitEstimate]] = {}
        for unit in estimation.units:
            self._unit_by_shop.setdefault(unit.shop, []).append(unit)

    # ---------- 审核 ----------
    def review_notices(self) -> ReviewReport:
        notices = list(self.ds.notices)
        claims, winners = self._claim_matrix(notices)
        reviews: list[NoticeReview] = []
        for notice in notices:
            reviews.append(self._review_one(notice, claims, winners))
        unclaimed = self._unclaimed_impact(notices)
        summary = self._summary(reviews, unclaimed)
        statuses = {(u.shop, u.owner): u.status for u in self.est.units}
        return ReviewReport(
            notices=reviews, unclaimed=unclaimed, summary=summary,
            unit_statuses=statuses, warnings=list(self.ds.warnings),
        )

    def _find_unit(self, shop: str, owner: str) -> tuple[UnitEstimate | None, bool, list[str]]:
        candidates = self._unit_by_shop.get(shop, [])
        if not candidates:
            return None, True, []
        if owner:
            for unit in candidates:
                if unit.owner == owner:
                    return unit, True, []
            return None, False, [u.owner for u in candidates if u.owner]
        if len(candidates) == 1:
            return candidates[0], True, []
        return None, False, [u.owner for u in candidates if u.owner]

    def _notice_weeks(self, notice: Notice) -> list[str]:
        return self.ds.covered_weeks(notice)

    def _claim_matrix(
        self, notices: Sequence[Notice]
    ) -> tuple[dict[str, dict[str, int]], dict[tuple[tuple[str, str], str], str]]:
        """为每个通知的每个影响周分配独占名额，避免同一影响重复计入。

        已支付 > 已签 > 草稿；同一优先级内按通知编号稳定排序。只有满足估计
        条件的经营人参与分配；不合格经营人的通知不占用额度，也不被误判为
        被他人覆盖。
        """
        order = {"paid": 0, "issued": 1, "partial": 0, "draft": 2}
        eligible = []
        for notice in notices:
            if notice.status in {"cancelled", "rejected"}:
                continue
            unit, matched, _ = self._find_unit(notice.shop, notice.owner)
            if unit is None or unit.status != "estimated":
                continue
            weeks = self._notice_weeks(notice)
            priority = order.get(notice.payment_state if notice.has_paid else notice.status, 3)
            eligible.append((priority, notice.notice_id, notice, unit, weeks))
        eligible.sort(key=lambda x: (x[0], x[1]))
        claims: dict[str, dict[str, int]] = {n.notice_id: {} for n in notices}
        winner: dict[tuple[tuple[str, str], str], str] = {}
        for _, nid, notice, unit, weeks in eligible:
            for week in weeks:
                key = ((unit.shop, unit.owner), week)
                if key in winner:
                    continue
                winner[key] = nid
                claims[nid][week] = 1
        return claims, winner

    def _review_one(
        self, notice: Notice, claims: Mapping[str, Mapping[str, int]],
        winners: Mapping[tuple[tuple[str, str], str], str],
    ) -> NoticeReview:
        unit, matched, other_owners = self._find_unit(notice.shop, notice.owner)
        r = NoticeReview(
            notice_id=notice.notice_id, shop=notice.shop, owner=notice.owner,
            period=notice.period, notice_amount=notice.amount,
            paid=notice.paid_amount, unpaid=notice.unpaid_amount,
            notice_status=notice.status, covered_weeks=sort_weeks(self._notice_weeks(notice)),
            owner_match=matched,
        )
        if notice.status == "cancelled":
            r.decision = "not_supported"
            r.coverage_state = "已撤销"
            r.reasons.append("通知已撤销，不再计入支持额度")
            r.actions.append("如需恢复，按新申请重新提交")
            return r
        if unit is None:
            if other_owners:
                r.decision = "object_mismatch"
                r.reasons.append(
                    "通知经营人与账册经营人不一致；不能把影响从原经营人自动挪给承接人"
                )
                r.actions.append("核对通知对象与经营人变更时点，转人工复核")
                r.remaining_unknown.extend(sorted(set(other_owners) - {notice.owner}))
            else:
                r.decision = "not_supported"
                r.reasons.append("账册中没有该店铺的经营记录，无法建立可归因影响")
                r.actions.append("补充店铺与经营人对应关系后再申请")
                r.remaining_unknown.append("店铺经营记录")
            r.coverage_state = "无经营记录"
            return r

        r.unit_key = (unit.shop, unit.owner)
        r.unit_status = unit.status
        r.unit_owner = unit.owner
        r.route_affected = bool(unit.route_lost_weeks)
        r.event_excluded_weeks = list(unit.event_excluded_weeks)
        r.control_units = list(unit.control_units)
        r.control_weeks = list(unit.control_weeks)
        r.route_lost_weeks = list(unit.route_lost_weeks)
        weekly = {w.week: w for w in unit.weekly}
        impact = [w.week for w in unit.weekly if (w.low or 0) > 0 and (w.effect or 0) > 0]
        r.impact_weeks = impact
        r.uncovered_impact_weeks = [w for w in impact if w not in r.covered_weeks]
        r.covered_non_impact_weeks = [
            w for w in r.covered_weeks
            if w in weekly and (weekly[w].effect is None or (weekly[w].effect or 0) <= 0)
        ]
        r.overlap_weeks = sort_weeks([
            w for w in r.covered_weeks
            if w in weekly and len(weekly[w].phases) > 1
        ])
        r.duplicate_weeks = sort_weeks([
            w for w in r.covered_weeks
            if r.unit_key is not None
            and (r.unit_key, w) in winners
            and winners[(r.unit_key, w)] != notice.notice_id
            and w in weekly
            and (weekly[w].effect or 0) > 0
        ])

        covered_estimated = [weekly[w] for w in r.covered_weeks if w in weekly]
        found_low, found_high = _sum_range(covered_estimated)
        r.support_low, r.support_high = found_low, found_high
        alloc_low = alloc_high = 0.0
        for week in covered_estimated:
            if claims.get(notice.notice_id, {}).get(week.week) is None:
                continue
            alloc_low += max(0.0, week.low or 0.0)
            alloc_high += max(0.0, week.high or 0.0)
        r.allocated_low, r.allocated_high = alloc_low, alloc_high
        if not r.covered_weeks:
            r.coverage_state = "无法对应账册周"
        elif not found_high:
            r.coverage_state = "覆盖期内无正影响"
        elif r.uncovered_impact_weeks:
            r.coverage_state = "部分覆盖"
        elif r.covered_non_impact_weeks:
            r.coverage_state = "覆盖期含无影响周"
        else:
            r.coverage_state = "覆盖与影响一致"

        if unit.status in {
            "owner_transfer_cut", "no_baseline", "no_control", "no_common_trend",
            "insufficient_pre", "not_affected", "estimated_sensitivity_only",
        }:
            r.reasons.extend(unit.reasons or [f"该经营人状态：{unit.status}"])
            if notice.has_paid:
                r.decision = "invalidated_manual"
                r.reasons.append("已付通知的原结论失效；已支付金额只可提交复议，不得自动冲回")
                r.actions.append("保留已付记录，提交人工复议")
            elif notice.status in {"issued", "paid"}:
                r.decision = "adjust_unpaid"
                r.reasons.append("已通知金额失去当前依据；只调整未兑现部分，不自动冲回")
                r.actions.append("停止未支付部分，待补报后按新证据重审")
            else:
                r.decision = "correctable_new"
                r.actions.append("可补充对照或经营人证据后更正为新申请")
            if notice.amount is not None and notice.amount > 0:
                r.remaining_unknown.append("可归因影响范围")
            return r

        if r.duplicate_weeks:
            r.reasons.append(
                f"周 {'、'.join(r.duplicate_weeks)} 同时被其他通知占用；同一影响不重复计入支持额度"
            )
        if r.overlap_weeks:
            r.reasons.append(
                f"周 {'、'.join(r.overlap_weeks)} 有多个施工阶段同时影响，损失按周只计一次"
            )
        if r.uncovered_impact_weeks:
            r.reasons.append(
                f"影响周 {'、'.join(r.uncovered_impact_weeks)} 未被该通知覆盖，可另行申请"
            )
        if r.covered_non_impact_weeks:
            names = []
            for week in r.covered_non_impact_weeks:
                entry = weekly.get(week)
                if entry is not None and entry.status == "excluded_concurrent_event":
                    names.append(f"{week}（同期事件已排除）")
                else:
                    names.append(week)
            r.reasons.append(
                f"周 {'、'.join(names)} 在覆盖期内但没有正的可归因影响"
            )

        amount = notice.amount
        post_weeks = {
            w.week for w in unit.weekly
            if w.role in {"direct", "spillover"} and w.status != "excluded_concurrent_event"
        }
        all_impact_weeks = set(impact)
        if r.covered_weeks and not (set(r.covered_weeks) & (post_weeks | all_impact_weeks)):
            # 覆盖期完全落在修缮前或非施工周：不是金额高低问题，而是没有可归因影响。
            if notice.has_paid:
                r.decision = "invalidated_manual"
                r.reasons.append("通知覆盖期不含可归因影响周；已支付部分不得自动冲回")
                r.actions.append("保留已付记录，提交人工复议")
            else:
                r.decision = "pre_period_only"
                r.reasons.append("通知覆盖期只含修缮前/非施工周，不能支持施工损失")
                r.actions.append("改按实际受影响周重新申请覆盖期")
            return r

        if r.duplicate_weeks and (r.covered_weeks and
                                  len(r.duplicate_weeks) == len(
                                      [w for w in r.covered_weeks if w in weekly and (weekly[w].effect or 0) > 0])):
            # 覆盖到的影响周全部已被优先级更高的通知占用：同一影响不得重复支持。
            r.decision = "duplicate_cover"
            r.reasons.append(
                "该通知覆盖的影响周已全部由其他通知计入支持额度，本通知不重复支持"
            )
            if notice.has_paid:
                r.actions.append("已付金额保留，转人工复议；未支付部分停止")
            else:
                r.actions.append("核对是否重复通知；如对象确属本店，合并到已有通知处理")
            return r

        if amount is None:
            r.decision = "manual_review"
            r.reasons.append("通知金额缺失，无法核对支持额度")
            r.actions.append("补录通知金额后复核")
        elif not r.supported_ceiling and not r.paid:
            r.decision = "correctable_new"
            r.reasons.append("当前证据不支持正金额，且尚未支付；可更正为新申请")
            r.actions.append("补充证据后按新申请提交")
        elif notice.payment_state == "paid" or (notice.has_paid and r.paid_invalid):
            r.decision = "paid_excess_manual" if r.paid_invalid else "confirm"
            if r.paid_invalid:
                r.reasons.append("已支付金额高于当前可支持上限；已付部分不得自动冲回")
                r.actions.append("保留已付记录，提交人工复议")
            if not r.paid_invalid and amount is not None and amount < r.supported_floor:
                r.reasons.append("通知金额低于可支持下限，可就差额另提新申请")
                r.actions.append("差额部分按新申请提交")
        elif amount > r.supported_ceiling + 1e-9:
            if notice.status == "draft":
                r.decision = "correctable_new"
                r.reasons.append("草稿金额高于当前可支持上限，尚未签发，可直接更正")
                r.actions.append("按可支持上限下调后提交新申请")
            else:
                r.decision = "adjust_unpaid"
                r.reasons.append(
                    f"通知金额 {money(amount)} 高于可支持上限 {money(r.supported_ceiling)}；"
                    "只调整未兑现部分，已付金额保留"
                )
                r.actions.append("按可支持上限重算未支付部分；已付部分如异议转人工复议")
        elif amount < r.supported_floor - 1e-9:
            r.decision = "supplement_new"
            r.reasons.append(
                f"通知金额 {money(amount)} 低于可支持下限 {money(r.supported_floor)}"
            )
            r.actions.append("通知内金额维持；差额部分作为新申请另行提交")
        else:
            r.decision = "confirm"
            r.reasons.append("通知金额落在当前可支持区间内，且覆盖期与影响周相符")
            r.actions.append("按原通知执行；补报变化时再复核")

        if unit.quality.missing_weeks:
            r.remaining_unknown.append(f"缺报周 {unit.quality.missing_weeks} 个，不能按零交易或损失处理")
        if unit.quality.natural_closed_weeks:
            r.remaining_unknown.append(f"自然闭店周 {unit.quality.natural_closed_weeks} 个，不计入支持")
        if any(w for w in r.covered_weeks if w in weekly and weekly[w].status == "not_estimable"):
            r.remaining_unknown.append("部分覆盖周缺少可用对照，未计入金额")
        if not r.owner_match:
            r.reasons.append("通知未指定经营人，按店铺唯一经营人关联")
        return r

    # ---------- 未获通知的影响 ----------
    def _unclaimed_impact(self, notices: Sequence[Notice]) -> list[dict]:
        claimed: set[tuple[str, str, str]] = set()
        for notice in notices:
            if notice.status == "cancelled":
                continue
            unit, _, _ = self._find_unit(notice.shop, notice.owner)
            if unit is None:
                continue
            for week in self._notice_weeks(notice):
                claimed.add((unit.shop, unit.owner, week))
        rows: list[dict] = []
        for unit in self.est.units:
            if not unit.support_eligible:
                continue
            for week in unit.weekly:
                if (week.low or 0) <= 0 or (week.effect or 0) <= 0:
                    continue
                if (unit.shop, unit.owner, week.week) in claimed:
                    continue
                rows.append({
                    "店铺": unit.shop,
                    "经营人": unit.owner,
                    "周": week.week,
                    "角色": week.role,
                    "施工阶段": list(week.phases),
                    "可支持下限": money(max(0.0, week.low or 0.0)),
                    "可支持上限": money(max(0.0, week.high or 0.0)),
                    "说明": "该影响周尚无有效通知覆盖，可按新申请提交",
                })
        return rows

    def _summary(self, reviews: Sequence[NoticeReview], unclaimed: Sequence[Mapping]) -> dict:
        active = [r for r in reviews if r.notice_status not in {"cancelled", "rejected"}]
        counts: dict[str, int] = {}
        for r in active:
            counts[r.decision] = counts.get(r.decision, 0) + 1
        committed = sum(r.notice_amount or 0.0 for r in active)
        paid = sum(r.paid for r in active)
        unpaid = sum(r.unpaid for r in active)
        supported_high = sum(r.supported_ceiling for r in active)
        adjust_amount = sum(
            max(0.0, min(r.unpaid, (r.notice_amount or 0.0) - r.supported_ceiling))
            for r in active if r.decision == "adjust_unpaid"
        )
        gap_amount = sum(
            max(0.0, r.supported_ceiling - (r.notice_amount or 0.0))
            for r in active if r.decision == "supplement_new"
        ) + sum((row["可支持上限"] or 0.0) for row in unclaimed)
        duplicate_notices = sum(1 for r in active if r.decision == "duplicate_cover")
        return {
            "通知数": len(active),
            "结论分布": {DECISIONS.get(k, k): v for k, v in sorted(counts.items())},
            "通知承诺总额": money(committed) or 0.0,
            "已支付承诺": money(paid) or 0.0,
            "未支付承诺": money(unpaid) or 0.0,
            "去重后支持上限": money(supported_high) or 0.0,
            "未兑现部分应下调": money(adjust_amount) or 0.0,
            "可另行申请缺口": money(gap_amount) or 0.0,
            "重复覆盖通知数": duplicate_notices,
            "未覆盖影响周数": len(unclaimed),
            "存在未覆盖影响": bool(unclaimed),
        }

    # ---------- 补报重审 ----------
    def reassess(self, previous: ReviewReport, current: ReviewReport | None = None) -> list[Reassessment]:
        current = current or self.review_notices()
        out: list[Reassessment] = []
        prev_by_id = {r.notice_id: r for r in previous.notices}
        cur_by_id = {r.notice_id: r for r in current.notices}
        all_ids = sorted(set(prev_by_id) | set(cur_by_id), key=lambda x: (week_key(x), x))
        for nid in all_ids:
            old = prev_by_id.get(nid)
            new = cur_by_id.get(nid)
            if old is None and new is None:
                continue
            if old is None and new is not None:
                out.append(self._reassess_new_notice(new))
                continue
            if old is not None and new is None:
                out.append(self._reassess_removed(old))
                continue
            out.append(self._reassess_pair(old, new))
        # 经营人层面：原先不满足、补报后满足的，生成可更正新申请提示。
        seen_notice_units = {(r.shop, r.owner) for r in current.notices}
        for unit in self.est.units:
            if (unit.shop, unit.owner) in seen_notice_units:
                continue
            if unit.status != "estimated" or not unit.support_eligible:
                continue
            if previous.unit_statuses.get((unit.shop, unit.owner)) == "estimated":
                continue
            out.append(Reassessment(
                shop=unit.shop, owner=unit.owner, notice_id=None,
                previous_decision="not_supported", new_decision="correctable_new",
                previous_supported=(0.0, 0.0), new_supported=unit.supportable_loss_range,
                paid=0.0, unpaid=0.0, trigger="补报后形成可用对照/趋势",
                reason="补报使该经营人首次具备可归因影响和可用对照",
                action="可提交新的缓冲申请；金额以重算区间为准", status="可更正的新申请",
            ))
        return out

    def _reassess_new_notice(self, new: NoticeReview) -> Reassessment:
        return Reassessment(
            shop=new.shop, owner=new.owner, notice_id=new.notice_id,
            previous_decision="not_supported", new_decision=new.decision,
            previous_supported=(0.0, 0.0), new_supported=(new.allocated_low, new.allocated_high),
            paid=new.paid, unpaid=new.unpaid, trigger="补报后新增通知",
            reason="补报中出现此前未登记的通知", action="按新通知依据复核", status="新申请",
        )

    def _reassess_removed(self, old: NoticeReview) -> Reassessment:
        return Reassessment(
            shop=old.shop, owner=old.owner, notice_id=old.notice_id,
            previous_decision=old.decision, new_decision="not_supported",
            previous_supported=(old.allocated_low, old.allocated_high), new_supported=(0.0, 0.0),
            paid=old.paid, unpaid=old.unpaid, trigger="补报后通知不再存在",
            reason="支持表中已无该通知记录",
            action=("已付金额保留，转人工复议" if old.paid > 0 else "停止未支付部分，按新资料重审"),
            status="原结论失效",
        )

    def _reassess_pair(self, old: NoticeReview, new: NoticeReview) -> Reassessment:
        prev_range = (old.allocated_low, old.allocated_high)
        new_range = (new.allocated_low, new.allocated_high)
        trigger = "资料更正"
        action = "按新结论处理"
        status = "无实质变化"
        reason = "可支持区间与审核结论未发生实质变化"

        lost_control = (
            old.unit_status == "estimated"
            and (new.unit_status in {"no_control", "no_common_trend"}
                 or set(new.control_units) < set(old.control_units))
        )
        lost_route = (
            bool(set(new.route_lost_weeks) - set(old.route_lost_weeks))
            and bool(set(new.route_lost_weeks) & set(new.covered_weeks))
        ) or (
            old.unit_status == "estimated"
            and new.unit_status != "estimated"
            and bool(new.route_lost_weeks or new.route_affected)
        )
        became_eligible = (
            old.unit_status != "estimated" and new.unit_status == "estimated"
        )
        payment_changed = abs(new.paid - old.paid) > 0.01
        if payment_changed and new.paid > 0 and new.paid > new.allocated_high + 1e-9:
            status = "原结论失效"
            trigger = "补报后已支付金额高于可支持上限"
            action = "新支付的超出部分只可提交复议，不得自动冲回"
            reason = "补报改变可支持上限，已支付金额现高于上限"
        elif lost_route or lost_control:
            trigger = "补报使绕行路线失去通达资格" if lost_route else "补报改变可用对照"
            if new.paid > 0 or old.paid > 0:
                status = "原结论失效"
                action = "已支付部分只可提交复议，不得自动冲回；未支付部分暂停"
            elif new.notice_status == "draft":
                status = "可更正的新申请"
                action = "草稿尚未签发，按补报后的对照/通达状态更正申请"
            else:
                status = "可重审"
                action = "停止未兑现部分，按补报后的对照/通达状态重算"
            reason = "；".join(new.reasons[:3]) or "补报后不再满足估计条件"
        elif became_eligible:
            status = "可更正的新申请"
            action = "按补报后的证据提交或更正申请"
            trigger = "补报形成可用对照"
            reason = "补报后该经营人重新满足共同趋势与对照条件"
        elif old.decision != new.decision or abs(prev_range[1] - new_range[1]) > 0.01 or payment_changed:
            trigger = self._diff_trigger(old, new)
            if payment_changed:
                trigger = "补报改变支付状态"
            if new.paid > 0 and new.paid > new.allocated_high + 1e-9:
                status = "原结论失效"
                action = "已付金额高于新上限，保留支付记录并转人工复议"
            elif new.decision in {"adjust_unpaid", "supplement_new", "correctable_new"}:
                status = "仅调整未兑现部分" if new.paid > 0 or old.paid > 0 else "可更正的新申请"
                action = {
                    "adjust_unpaid": "仅重算未支付部分，已付承诺不变",
                    "supplement_new": "原通知维持，差额另提新申请",
                    "correctable_new": "尚未签发，按新证据更正申请",
                }.get(new.decision, action)
            elif new.decision == "invalidated_manual":
                status = "原结论失效"
                action = "已支付部分保留，转人工复议"
            elif payment_changed:
                status = "补报更新支付记录"
                action = (
                    "已付金额只作记录更新，不自动冲回；未付部分按当前可支持区间复核"
                    if new.paid > 0 else "按最新支付状态复核未付部分"
                )
            else:
                status = "金额变化"
                action = "按新可支持区间调整后续审核"
            reason = "; ".join(new.reasons[:3]) or "可支持区间变化"
        return Reassessment(
            shop=new.shop, owner=new.owner or old.owner, notice_id=new.notice_id,
            previous_decision=old.decision, new_decision=new.decision,
            previous_supported=prev_range, new_supported=new_range,
            paid=max(old.paid, new.paid), unpaid=new.unpaid, trigger=trigger,
            reason=reason, action=action, status=status,
        )

    def _diff_trigger(self, old: NoticeReview, new: NoticeReview) -> str:
        if old.covered_weeks != new.covered_weeks:
            return "通知覆盖期变化"
        if old.overlap_weeks != new.overlap_weeks:
            return "施工阶段重叠关系变化"
        if old.control_weeks != new.control_weeks:
            return "可用对照周变化"
        return "可归因影响重算"

    # ---------- 经营人视图 ----------
    def merchant_view(self, shop: str, owner: str | None = None, review: ReviewReport | None = None) -> dict:
        current = review or self.review_notices()
        reviews = [r for r in current.notices if r.shop == shop]
        if owner is not None:
            reviews = [r for r in reviews if r.owner in {"", owner}]
        units = [u for u in self._unit_by_shop.get(shop, []) if owner is None or u.owner == owner]
        return {
            "店铺": shop,
            "经营人": owner or (units[0].owner if len(units) == 1 else ""),
            "有效依据": [self._valid_basis(u, reviews) for u in units],
            "通知": [r.as_dict() for r in reviews],
            "仍未知": self._unknowns(units, reviews),
            "重算影响": [self._recalc_summary(u) for u in units],
            "可进入的下一步": self._next_steps(units, reviews),
        }

    def _valid_basis(self, unit: UnitEstimate, reviews: Sequence[NoticeReview]) -> dict:
        return {
            "经营人": unit.owner,
            "状态": unit.status,
            "基线": money(unit.baseline),
            "可支持区间": [money(unit.supportable_loss_range[0]), money(unit.supportable_loss_range[1])],
            "可解释变化窗口": list(unit.change_window) if unit.change_window else None,
            "对照店铺": list(unit.control_units),
            "有效比较条件": list(unit.comparison_conditions),
            "影响周": [
                {"周": w.week, "角色": w.role, "阶段": list(w.phases),
                 "下限": money(w.low), "上限": money(w.high)}
                for w in unit.weekly if (w.effect or 0) > 0
            ],
            "通知关联": [r.notice_id for r in reviews if r.unit_key == (unit.shop, unit.owner)],
        }

    def _unknowns(self, units: Sequence[UnitEstimate], reviews: Sequence[NoticeReview]) -> list[str]:
        out: list[str] = []
        for unit in units:
            q = unit.quality
            if q.missing_weeks:
                out.append(f"有 {q.missing_weeks} 个缺报周，不能按零交易或损失处理")
            if q.natural_closed_weeks:
                out.append(f"有 {q.natural_closed_weeks} 个自然闭店周，不纳入施工损失")
            if q.works_closed_weeks:
                out.append(f"有 {q.works_closed_weeks} 个施工相关闭店周，缺少可核营业额")
            if q.transfer_cut:
                out.append("存在经营人变更，承接人施工前基线未知")
            if unit.status == "no_control":
                out.append("没有同期未受扰动、修缮前有共同趋势的可用对照")
            elif unit.status == "estimated_sensitivity_only" or unit.status == "no_common_trend":
                out.append("可用对照趋势不一致，金额仅作敏感性参考，不直接支持")
        for r in reviews:
            out.extend(r.remaining_unknown)
            if r.uncovered_impact_weeks:
                out.append(f"通知 {r.notice_id} 未覆盖影响周 {'、'.join(r.uncovered_impact_weeks)}")
        return list(dict.fromkeys(out))

    def _recalc_summary(self, unit: UnitEstimate) -> dict:
        per_phase: dict[str, dict[str, float]] = {}
        for week in unit.weekly:
            if (week.effect or 0) <= 0:
                continue
            for phase in week.phases:
                bucket = per_phase.setdefault(phase, {"下限": 0.0, "上限": 0.0})
                bucket["下限"] += max(0.0, week.low or 0.0) / len(week.phases)
                bucket["上限"] += max(0.0, week.high or 0.0) / len(week.phases)
        return {
            "经营人": unit.owner,
            "可支持区间": [money(unit.supportable_loss_range[0]), money(unit.supportable_loss_range[1])],
            "按阶段": {
                k: {"下限": money(v["下限"]), "上限": money(v["上限"])}
                for k, v in per_phase.items()
            },
            "说明": "多阶段重叠周按周只计一次，阶段金额为展示分摊，不可重复累加",
        }

    def _next_steps(self, units: Sequence[UnitEstimate], reviews: Sequence[NoticeReview]) -> list[str]:
        steps: list[str] = []
        for r in reviews:
            for action in r.actions:
                if action not in steps:
                    steps.append(action)
        for unit in units:
            if unit.status == "no_control":
                steps.append("补充同期未受绕行影响、且修缮前有共同趋势的对照店铺")
            elif unit.status == "no_common_trend":
                steps.append("补充修缮前共同周，或改用本街段自身趋势，不迁移别处效应")
            elif unit.status == "owner_transfer_cut":
                steps.append("提供经营人变更时点与承接后经营记录")
            elif unit.quality.missing_weeks:
                steps.append("补报缺报周；补报前缺报不按零交易计算")
        if not steps:
            steps.append("按当前结论执行；资料更新时重新审核")
        return list(dict.fromkeys(steps))
