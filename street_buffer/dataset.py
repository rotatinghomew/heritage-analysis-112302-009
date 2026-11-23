"""输入账册的读取、规范化和完整性检查。"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .util import (
    as_bool,
    as_float,
    blank,
    inclusive_weeks,
    parse_ratio,
    period_range,
    sort_weeks,
    text,
    week_key,
)


PRE_LABELS = {"pre", "baseline", "before", "prior", "修缮前", "施工前", "前期", "基线"}
POST_LABELS = {"post", "after", "afterwards", "修缮后", "施工后", "后期", "恢复"}
ACTIVE_LABELS = {"works", "work", "construction", "phase", "building", "施工", "围挡", "修缮", "施工期"}
CLOSED_STATUS = {"closed", "closure", "close", "shut", "停业", "闭店", "关门", "歇业"}
MISSING_STATUS = {"missing", "unknown", "na", "n/a", "missing_report", "缺报", "未知", "未报", "缺失"}
ZERO_STATUS = {"zero", "no_trade", "no-sale", "no_sale", "零交易", "无交易", "零营业额"}
PAID_STATUS = {"paid", "settled", "已支付", "已付", "支付"}
CANCEL_STATUS = {"cancelled", "canceled", "withdrawn", "rejected", "void", "撤销", "驳回", "作废"}
DRAFT_STATUS = {"draft", "草稿", "拟稿", "待提交"}
ISSUED_STATUS = {"issued", "notice", "已通知", "已签发", "签发"}

WORK_TERMS = ("施工", "修缮", "围挡", "绕行", "封路", "道路", "工程", "works", "construction", "detour")


def _norm_status(value: object) -> str:
    s = text(value).lower()
    if s in CLOSED_STATUS:
        return "closed"
    if s in MISSING_STATUS:
        return "missing"
    if s in ZERO_STATUS:
        return "zero"
    if s in PAID_STATUS:
        return "paid"
    if s in CANCEL_STATUS:
        return "cancelled"
    if s in DRAFT_STATUS:
        return "draft"
    if s in ISSUED_STATUS:
        return "issued"
    return s or "unknown"


def _notice_ids(value: object) -> tuple[str, ...]:
    s = text(value)
    if not s:
        return ()
    for sep in (";", "；", "|", "，", ","):
        s = s.replace(sep, " ")
    return tuple(dict.fromkeys(part for part in s.split() if part))


def _is_work_reason(value: object) -> bool:
    s = text(value).lower()
    return bool(s) and any(term in s for term in WORK_TERMS)


@dataclass(frozen=True)
class Observation:
    shop: str
    owner: str
    week: str
    amount: float | None
    raw_status: str
    kind: str
    open: bool | None
    street: str
    phase: str
    detour: bool | None
    spillover: bool | None
    notice_ids: tuple[str, ...]
    route_id: str
    trade_week: str
    settlement_week: str
    source: str
    line: int
    close_reason: str
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def unit(self) -> tuple[str, str]:
        return (self.shop, self.owner)

    @property
    def is_usable_amount(self) -> bool:
        return self.kind in {"trade", "zero"} and self.amount is not None

    @property
    def kind_label(self) -> str:
        return {
            "trade": "成交",
            "zero": "营业但零交易",
            "missing": "缺报",
            "closed_natural": "自然闭店",
            "closed_works": "施工相关闭店",
        }.get(self.kind, self.kind)


@dataclass(frozen=True)
class Notice:
    notice_id: str
    shop: str
    owner: str
    period: str
    start_week: str
    end_week: str
    amount: float | None
    raw_status: str
    status: str
    paid_amount: float
    source: str
    line: int
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def has_paid(self) -> bool:
        return self.paid_amount > 0

    @property
    def unpaid_amount(self) -> float:
        if self.status == "cancelled" or self.amount is None:
            return 0.0
        return max(0.0, self.amount - self.paid_amount)

    @property
    def payment_state(self) -> str:
        if self.status == "cancelled":
            return "cancelled"
        if self.paid_amount <= 0:
            return "unpaid"
        if self.amount is not None and self.paid_amount + 1e-9 >= self.amount:
            return "paid"
        return "partial"


@dataclass(frozen=True)
class PhaseDef:
    phase: str
    street: str = ""
    start_week: str = ""
    end_week: str = ""
    kind: str = ""
    detour: bool | None = None
    route_id: str = ""
    source: str = ""
    line: int = 0


@dataclass(frozen=True)
class Event:
    """同期但非本次修缮的影响（节日、邻街先封、天气、应急等）。"""

    event_id: str
    kind: str
    start_week: str
    end_week: str
    scope: str = "citywide"
    street: str = ""
    note: str = ""
    source: str = ""
    line: int = 0

    def applies_to(self, street: str, week: str, universe: Iterable[str] = ()) -> bool:
        weeks = inclusive_weeks(self.start_week, self.end_week, sort_weeks(universe))
        if week not in weeks:
            return False
        scope = (self.scope or "citywide").lower()
        if scope in {"citywide", "city", "all", "全域", "全市", "全区", "全区县"}:
            return True
        if scope in {"street", "segment", "街段", "街道"}:
            return bool(street) and street == self.street
        if scope in {"neighbor", "adjacent", "邻街", "隔壁", "周边"}:
            return bool(street) and street != self.street
        return True


@dataclass(frozen=True)
class Route:
    route_id: str
    street: str = ""
    from_node: str = ""
    to_node: str = ""
    start_week: str = ""
    end_week: str = ""
    status: str = "open"
    capacity: float | None = None
    source: str = ""
    line: int = 0

    def usable(self, week: str, universe: Iterable[str] = ()) -> bool:
        weeks = sort_weeks(universe)
        closed_state = self.status.lower() in {
            "closed", "unavailable", "blocked", "中断", "关闭", "不可用",
        }
        in_window = True
        if self.start_week and self.end_week and weeks:
            in_window = week in inclusive_weeks(self.start_week, self.end_week, weeks)
        if closed_state:
            # 对状态为关闭的路线，起止周表示关闭期；窗口外恢复通达。
            return not in_window
        return in_window


@dataclass
class Dataset:
    observations: list[Observation] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    policies: list[tuple[str, str, str]] = field(default_factory=list)
    phase_defs: list[PhaseDef] = field(default_factory=list)
    routes: dict[str, Route] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    source_dir: str = ""
    files: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    # ---------- 基本索引 ----------
    @property
    def weeks(self) -> list[str]:
        return sort_weeks(
            [o.week for o in self.observations]
            + [n.start_week for n in self.notices]
            + [n.end_week for n in self.notices]
            + [p.start_week for p in self.phase_defs]
            + [p.end_week for p in self.phase_defs]
        )

    @property
    def shops(self) -> list[str]:
        return sorted({o.shop for o in self.observations})

    @property
    def streets(self) -> list[str]:
        return sorted({o.street for o in self.observations if o.street})

    def observation_list(self, shop: str, owner: str | None = None) -> list[Observation]:
        rows = [o for o in self.observations if o.shop == shop]
        if owner is not None:
            rows = [o for o in rows if o.owner == owner]
        return sorted(rows, key=lambda o: week_key(o.week))

    def operator_units(self) -> list[tuple[str, str]]:
        return sorted({o.unit for o in self.observations})

    def notices_for(self, shop: str, owner: str | None = None) -> list[Notice]:
        rows = [n for n in self.notices if n.shop == shop]
        if owner is not None:
            rows = [n for n in rows if not n.owner or n.owner == owner]
        return sorted(rows, key=lambda n: (week_key(n.start_week), n.notice_id))

    def notice(self, notice_id: str) -> Notice | None:
        for n in self.notices:
            if n.notice_id == notice_id:
                return n
        return None

    # ---------- 施工阶段与角色 ----------
    def _infer_phase_windows(self) -> None:
        """从账册或 phases.csv 推断每个施工阶段的周窗口、施工街段和受影响街段。"""
        self.active_weeks_by_phase: dict[str, tuple[str, ...]] = {}
        self.construction_streets: dict[str, tuple[str, ...]] = {}
        self.affected_shops: dict[str, tuple[str, ...]] = {}
        self.side_streets: dict[str, tuple[str, ...]] = {}
        universe = self.weeks

        if self.phase_defs:
            grouped: dict[str, list[PhaseDef]] = {}
            for p in self.phase_defs:
                label = p.phase
                kind = (p.kind or "").lower()
                if kind in {"pre", "post", "baseline", "after"} or label.lower() in PRE_LABELS | POST_LABELS:
                    continue
                grouped.setdefault(label, []).append(p)
            for label, defs in grouped.items():
                weeks: list[str] = []
                construction_streets: set[str] = set()
                side_streets: set[str] = set()
                for p in defs:
                    if p.start_week and p.end_week:
                        weeks.extend(inclusive_weeks(p.start_week, p.end_week, universe))
                    if p.street:
                        if (p.kind or "").lower() in {"side", "spillover", "外溢", "侧街"}:
                            side_streets.add(p.street)
                        elif (p.kind or "").lower() not in {"pre", "post", "baseline", "after"}:
                            construction_streets.add(p.street)
                if not weeks:
                    weeks = [o.week for o in self.observations if o.phase == label]
                active = sort_weeks(weeks)
                self.active_weeks_by_phase[label] = tuple(active)
                construction_streets |= {
                    o.street for o in self.observations
                    if o.phase == label and o.detour is True and o.street
                }
                self.construction_streets[label] = tuple(sorted(construction_streets))
                self.affected_shops[label] = tuple(sorted({
                    o.shop for o in self.observations if o.phase == label and o.detour is True
                }))
                side_streets |= {
                    o.street for o in self.observations
                    if o.phase == label and o.spillover is True and o.street
                }
                self.side_streets[label] = tuple(sorted(side_streets))
            if self.active_weeks_by_phase:
                return

        labels = sorted({o.phase for o in self.observations if o.phase})
        for label in labels:
            low = label.lower()
            if low in PRE_LABELS or low in POST_LABELS:
                continue
            active = tuple(sort_weeks([o.week for o in self.observations if o.phase == label]))
            if not active:
                continue
            self.active_weeks_by_phase[label] = active
            detour_streets = {
                o.street for o in self.observations
                if o.phase == label and o.detour is True and o.street
            }
            # 若账册没有显式绕行标记，至少把该阶段出现过的街段视为施工影响范围。
            if not detour_streets:
                detour_streets = {
                    o.street for o in self.observations if o.phase == label and o.street
                }
            self.construction_streets[label] = tuple(sorted(detour_streets))
            self.affected_shops[label] = tuple(sorted({
                o.shop for o in self.observations if o.phase == label and o.detour is True
            }))
            self.side_streets[label] = tuple(sorted({
                o.street for o in self.observations
                if o.phase == label and o.spillover is True and o.street
            }))

    def active_phases(self, week: str) -> tuple[str, ...]:
        return tuple(
            label for label, weeks in self.active_weeks_by_phase.items() if week in weeks
        )

    def construction_weeks(self) -> list[str]:
        weeks: set[str] = set()
        for group in self.active_weeks_by_phase.values():
            weeks.update(group)
        return sort_weeks(weeks)

    def is_construction_week(self, week: str) -> bool:
        return bool(self.active_phases(week))

    def route_state(self, route_id: str, week: str) -> str:
        """返回 ``ok`` / ``lost`` / ``unknown``；未登记路线不等于中断。"""
        if not route_id:
            return "ok"
        route = self.routes.get(route_id)
        if route is None:
            return "unknown"
        return "ok" if route.usable(week, self.weeks) else "lost"

    def route_usable(self, route_id: str, week: str) -> bool:
        return self.route_state(route_id, week) != "lost"

    def route_known(self, route_id: str) -> bool:
        return not route_id or route_id in self.routes

    def role_for(self, obs: Observation) -> tuple[str, tuple[str, ...], list[str]]:
        """返回 ``(direct/spillover/unaffected/unknown, 阶段, 说明)``。

        direct 表示施工街段直接受阻；spillover 表示仍营业侧街因绕行产生的
        可到达性外溢；unaffected 才可能进入对照池。
        """
        phases = self.active_phases(obs.week)
        reasons: list[str] = []
        if obs.detour is True:
            role = "direct"
            reasons.append("账册标记为绕行/直接受阻")
        elif obs.spillover is True:
            role = "spillover"
            reasons.append("账册标记为绕行外溢（可到达性改变）")
        elif phases:
            street_direct = any(
                obs.street and obs.street in self.construction_streets.get(p, ())
                for p in phases
            )
            side = any(
                obs.street and obs.street in self.side_streets.get(p, ())
                for p in phases
            )
            if street_direct:
                role = "direct"
                reasons.append("街段处于同期施工影响范围")
            elif side:
                role = "spillover"
                reasons.append("同期施工仍在营业侧街，绕行改变可到达性")
            elif obs.detour is False:
                role = "unaffected"
                reasons.append("施工期该经营人未标记绕行或外溢")
            else:
                role = "unknown"
                reasons.append("施工期内缺少绕行/可到达性标记")
        else:
            role = "unaffected"
            reasons.append("非施工期")
        if obs.route_id:
            state = self.route_state(obs.route_id, obs.week)
            if state == "lost":
                reasons.append("绕行路线在该周不通达")
            elif state == "unknown":
                reasons.append("绕行路线未登记，通达状态未知")
        return role, phases, reasons

    def is_control_candidate(self, obs: Observation) -> bool:
        role, _, _ = self.role_for(obs)
        if role != "unaffected":
            return False
        if obs.kind not in {"trade", "zero"}:
            return False
        if obs.route_id and not self.route_usable(obs.route_id, obs.week):
            return False
        return True

    # ---------- 完整性 ----------
    def _check_integrity(self) -> None:
        # 同一经营人同一周重复记录
        seen: dict[tuple[str, str, str], list[Observation]] = {}
        for o in self.observations:
            seen.setdefault((o.shop, o.owner, o.week), []).append(o)
        for key, rows in seen.items():
            if len(rows) > 1:
                self.warnings.append(
                    f"重复周记录：店铺 {key[0]} 经营人 {key[1] or '未标注'} 周 {key[2]} 有 {len(rows)} 条，估计时按冲突处理"
                )
            for o in rows:
                if o.kind == "closed_natural" and o.amount not in (None, 0):
                    self.warnings.append(f"{o.shop} {o.week} 标记闭店但仍有金额，按闭店处理")
                if o.settlement_week and o.settlement_week != o.week:
                    self.warnings.append(
                        f"{o.shop} {o.week} 成交在 {o.settlement_week} 结算，收入仍归属成交周"
                    )

        by_id: dict[str, Notice] = {}
        for n in self.notices:
            if n.notice_id in by_id:
                self.warnings.append(f"通知编号 {n.notice_id} 在支持表中重复")
            by_id[n.notice_id] = n
            if n.amount is not None and n.amount < 0:
                self.warnings.append(f"通知 {n.notice_id} 金额为负")
            if n.owner and n.shop:
                owners = {o.owner for o in self.observation_list(n.shop) if o.owner}
                if owners and n.owner not in owners:
                    self.warnings.append(
                        f"通知 {n.notice_id} 的对象 {n.owner} 与店铺 {n.shop} 的经营人不一致"
                    )
            if n.amount is not None and n.paid_amount > n.amount + 1e-9:
                self.warnings.append(f"通知 {n.notice_id} 已支付金额超过通知金额")

        primary_notice_shops: dict[str, set[str]] = {}
        for o in self.observations:
            for nid in o.notice_ids:
                primary_notice_shops.setdefault(nid, set()).add(o.shop)
        for nid, shops in primary_notice_shops.items():
            if nid not in by_id:
                self.warnings.append(f"账册引用了未在支持表登记的通知 {nid}")
            elif len(shops) > 1:
                self.warnings.append(
                    f"通知 {nid} 在账册中关联多个店铺（{'、'.join(sorted(shops))}），覆盖对象不唯一"
                )
            elif by_id[nid].shop and shops != {by_id[nid].shop}:
                self.warnings.append(
                    f"通知 {nid} 的账册店铺 {'、'.join(sorted(shops))} 与支持表店铺 {by_id[nid].shop} 不一致"
                )
        for nid, notice in by_id.items():
            if nid not in primary_notice_shops:
                self.warnings.append(f"通知 {nid} 未在账册周记录中引用，覆盖关系仅按支持表判断")

        for n in self.notices:
            if n.start_week and n.start_week not in self.weeks:
                self.warnings.append(f"通知 {n.notice_id} 的起始周 {n.start_week} 不在账册周序中")
            covered = self.covered_weeks(n)
            if not covered:
                self.warnings.append(f"通知 {n.notice_id} 的覆盖期 {n.period} 无法对应账册周")

        for route_id, route in self.routes.items():
            if route.status.lower() in {"closed", "unavailable", "blocked", "中断", "关闭", "不可用"}:
                users = [o.shop for o in self.observations if o.route_id == route_id]
                if users:
                    self.warnings.append(
                        f"绕行路线 {route_id} 为不可用状态，涉及店铺 {'、'.join(sorted(set(users)))}"
                    )

    def covered_weeks(self, notice: Notice) -> list[str]:
        return inclusive_weeks(notice.start_week, notice.end_week, self.weeks)

    def owners_for_weeks(self, shop: str, weeks: Iterable[str]) -> set[str]:
        wanted = set(weeks)
        return {o.owner for o in self.observations if o.shop == shop and o.week in wanted and o.owner}

    def events_for(self, street: str, week: str) -> list[Event]:
        return [e for e in self.events if e.applies_to(street, week, self.weeks)]

    def is_clean_week(self, street: str, week: str, ignore_event_ids: Iterable[str] = ()) -> bool:
        ignored = set(ignore_event_ids)
        return not any(e for e in self.events_for(street, week) if e.event_id not in ignored)

    def phase_defs_for(self, week: str) -> list[PhaseDef]:
        return [
            p for p in self.phase_defs
            if week in inclusive_weeks(p.start_week, p.end_week, self.weeks)
        ]


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            return [], []
        fields = [f.strip() for f in reader.fieldnames]
        rows = []
        for raw in reader:
            rows.append({(k or "").strip(): (v if v is not None else "") for k, v in raw.items()})
    return fields, rows


def _pick(row: Mapping[str, str], *names: str) -> str:
    for name in names:
        if name in row and not blank(row[name]):
            return text(row[name])
    return ""


def _observation_from_row(row: Mapping[str, str], source: str, line: int) -> Observation:
    shop = _pick(row, "shop", "店铺", "商户")
    owner = _pick(row, "owner", "经营人", "经营者")
    week = _pick(row, "week", "business_week", "营业周", "周")
    trade_week = _pick(row, "trade_week", "成交周", "transaction_week") or week
    settlement_week = _pick(row, "settlement_week", "settled_week", "结算周", "入账周")
    amount = as_float(_pick(row, "amount", "revenue", "sales", "金额", "营业额", "收款"))
    raw_status = _pick(row, "status", "状态")
    status = _norm_status(raw_status)
    open_flag = as_bool(_pick(row, "open", "开门", "营业", "is_open"))
    close_reason = _pick(row, "close_reason", "closure_reason", "闭店原因", "停业原因")
    street = _pick(row, "street", "街段", "街道")
    phase = _pick(row, "phase", "阶段", "施工阶段")
    detour = as_bool(_pick(row, "detour", "绕行", "reroute"))
    spillover = as_bool(_pick(row, "spillover", "外溢", "可到达性改变"))
    route_id = _pick(row, "route", "route_id", "绕行路线")
    notice_ids = _notice_ids(_pick(row, "notice", "notices", "通知", "通知编号"))

    if open_flag is False or status == "closed":
        kind = "closed_works" if _is_work_reason(close_reason) else "closed_natural"
    elif status == "missing":
        # 明确标为缺报时，即使来源误填 0 也不当作零交易。
        kind = "missing"
    elif status == "zero" and (amount is None or abs(amount) < 1e-12):
        kind = "zero"
    elif amount is None:
        kind = "missing"
    elif abs(amount) < 1e-12:
        kind = "zero"
    else:
        kind = "trade"

    known = {
        "shop", "owner", "week", "business_week", "营业周", "周", "trade_week", "成交周",
        "transaction_week", "settlement_week", "settled_week", "结算周", "入账周", "amount",
        "revenue", "sales", "金额", "营业额", "收款", "status", "状态", "open", "开门", "营业",
        "is_open", "close_reason", "closure_reason", "闭店原因", "停业原因", "street", "街段",
        "街道", "phase", "阶段", "施工阶段", "detour", "绕行", "reroute", "route", "route_id",
        "绕行路线", "notice", "notices", "通知", "通知编号", "spillover", "外溢", "可到达性改变",
    }
    extra = tuple(sorted((k, text(v)) for k, v in row.items() if k not in known and not blank(v)))
    return Observation(
        shop=shop, owner=owner, week=week, amount=amount, raw_status=raw_status,
        kind=kind, open=open_flag, street=street, phase=phase, detour=detour,
        spillover=spillover, notice_ids=notice_ids, route_id=route_id, trade_week=trade_week,
        settlement_week=settlement_week, source=source, line=line,
        close_reason=close_reason, extra=extra,
    )


def _notice_from_row(row: Mapping[str, str], source: str, line: int) -> Notice:
    notice_id = _pick(row, "notice", "notice_id", "通知", "通知编号")
    period = _pick(row, "period", "覆盖期", "期间")
    start = _pick(row, "start_week", "start", "起始周", "开始周")
    end = _pick(row, "end_week", "end", "结束周", "截止周")
    if not start:
        start, end2 = period_range(period)
        end = end or end2
    start = start or period
    end = end or start
    amount = as_float(_pick(row, "amount", "support", "金额", "缓冲金额"))
    raw_status = _pick(row, "status", "状态")
    status = _norm_status(raw_status)
    paid_flag = as_bool(_pick(row, "paid", "已支付", "已付"))
    paid_amount = as_float(_pick(row, "paid_amount", "已支付金额", "已付金额"))
    if paid_amount is None:
        paid_amount = amount if (paid_flag is True or status == "paid") and amount is not None else 0.0
    if paid_flag is True and status not in {"cancelled", "rejected"}:
        status = "paid" if amount is not None and paid_amount + 1e-9 >= amount else status
    known = {
        "notice", "notice_id", "通知", "通知编号", "period", "覆盖期", "期间", "start_week",
        "start", "起始周", "开始周", "end_week", "end", "结束周", "截止周", "amount", "support",
        "金额", "缓冲金额", "status", "状态", "paid", "已支付", "已付", "paid_amount",
        "已支付金额", "已付金额",
    }
    extra = tuple(sorted((k, text(v)) for k, v in row.items() if k not in known and not blank(v)))
    return Notice(
        notice_id=notice_id, shop=_pick(row, "shop", "店铺", "商户"),
        owner=_pick(row, "owner", "经营人", "经营者"), period=period,
        start_week=start, end_week=end, amount=amount, raw_status=raw_status,
        status=status, paid_amount=paid_amount or 0.0, source=source, line=line, extra=extra,
    )


def load_dataset(directory: str | Path) -> Dataset:
    """读取目录中的主账册、支持表和可选阶段/路线文件。"""
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(f"输入目录不存在：{root}")
    ds = Dataset(source_dir=str(root))
    files: list[Path] = []
    primary = root / "primary.csv"
    if primary.exists():
        files.append(primary)
    for path in sorted(root.glob("support-*.csv")):
        if path not in files:
            files.append(path)
    for name in ("notices.csv", "phases.csv", "routes.csv", "events.csv"):
        path = root / name
        if path.exists() and path not in files:
            files.append(path)
    ds.files = tuple(p.name for p in files)
    if primary not in files:
        raise FileNotFoundError(f"缺少 primary.csv：{root}")

    for path in files:
        fields, rows = _read_csv(path)
        lower = {f.lower() for f in fields}
        for i, row in enumerate(rows, start=2):
            if path.name == "primary.csv":
                ds.observations.append(_observation_from_row(row, path.name, i))
            elif "policy" in lower:
                ds.policies.append((
                    _pick(row, "policy", "策略"), _pick(row, "item", "项目"),
                    _pick(row, "rule", "规则"),
                ))
            elif "notice" in lower and "shop" in lower:
                ds.notices.append(_notice_from_row(row, path.name, i))
            elif "route_id" in lower or (path.name == "routes.csv"):
                route_id = _pick(row, "route_id", "route", "路线")
                ds.routes[route_id] = Route(
                    route_id=route_id, street=_pick(row, "street", "街段"),
                    from_node=_pick(row, "from", "from_node", "起点"),
                    to_node=_pick(row, "to", "to_node", "终点"),
                    start_week=_pick(row, "start_week", "start", "起始周"),
                    end_week=_pick(row, "end_week", "end", "结束周"),
                    status=_pick(row, "status", "状态") or "open",
                    capacity=parse_ratio(_pick(row, "capacity", "容量")),
                    source=path.name, line=i,
                )
            elif "phase" in lower and ("start_week" in lower or "start" in lower):
                ds.phase_defs.append(PhaseDef(
                    phase=_pick(row, "phase", "阶段", "施工阶段"),
                    street=_pick(row, "street", "街段", "街道"),
                    start_week=_pick(row, "start_week", "start", "起始周"),
                    end_week=_pick(row, "end_week", "end", "结束周"),
                    kind=_pick(row, "kind", "类型"),
                    detour=as_bool(_pick(row, "detour", "绕行")),
                    route_id=_pick(row, "route", "route_id", "绕行路线"),
                    source=path.name, line=i,
                ))
            elif path.name == "events.csv" or ("event" in lower and "week" in lower):
                ds.events.append(Event(
                    event_id=_pick(row, "event", "event_id", "事件", "事件编号"),
                    kind=_pick(row, "kind", "type", "类型") or "unclassified",
                    start_week=_pick(row, "start_week", "start", "week", "起始周", "周"),
                    end_week=_pick(row, "end_week", "end", "结束周") or _pick(
                        row, "start_week", "start", "week", "起始周", "周"
                    ),
                    scope=_pick(row, "scope", "范围") or "citywide",
                    street=_pick(row, "street", "街段", "街道"),
                    note=_pick(row, "note", "说明"),
                    source=path.name, line=i,
                ))
            else:
                ds.observations.append(_observation_from_row(row, path.name, i))

    ds.observations = [o for o in ds.observations if o.shop and o.week]
    ds.notices = [n for n in ds.notices if n.notice_id]
    ds._infer_phase_windows()
    ds._check_integrity()
    return ds
