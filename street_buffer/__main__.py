"""命令行入口：估计、审核、重审、经营人视图和方案比较。

示例::

    python3 -m street_buffer estimate --data fixtures
    python3 -m street_buffer review   --data fixtures --format text
    python3 -m street_buffer merchant --data fixtures --shop S1
    python3 -m street_buffer reassess --data fixtures --previous old.json
    python3 -m street_buffer scenario --data fixtures --scenarios plans.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dataset import load_dataset
from .estimation import EstimationConfig, Estimator
from .review import ReviewEngine, ReviewReport
from .scenario import ScenarioPlanner, load_scenarios


def _dump(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _build(args) -> tuple:
    ds = load_dataset(args.data)
    config = EstimationConfig(
        min_pre_weeks=args.min_pre_weeks,
        trend_pre_weeks=args.trend_pre_weeks,
        min_controls=args.min_controls,
        confidence=args.confidence,
        bootstrap=args.bootstrap,
        trend_tolerance=args.trend_tolerance,
        include_spillover=not args.exclude_spillover,
        seed=args.seed,
    )
    report = Estimator(ds, config).estimate()
    return ds, report


def _print_warnings(ds) -> None:
    if ds.warnings:
        print("# 数据警告", file=sys.stderr)
        for warning in ds.warnings:
            print(f"  - {warning}", file=sys.stderr)


def _render_estimate(report) -> str:
    lines = ["# 分期可归因影响估计", ""]
    lines.append("## 总额")
    for key, value in report.totals.items():
        lines.append(f"- {key}: {value:,.2f}")
    lines.append("")
    lines.append("## 阶段")
    for phase, est in sorted(report.phase_estimates.items()):
        lines.append(
            f"- {phase}: 直接 {est.direct_loss:,.2f} / 外溢 {est.spillover_loss:,.2f} / "
            f"合计 {est.total_loss:,.2f}（区间 {est.total_low:,.2f}–{est.total_high:,.2f}）"
        )
        if est.overlap_weeks:
            lines.append(f"  重叠周（按周去重）: {'、'.join(est.overlap_weeks)}")
    lines.append("")
    lines.append("## 经营人")
    for unit in report.units:
        label = f"{unit.shop}/{unit.owner}" if unit.owner else unit.shop
        lines.append(f"### {label}（{unit.status}）")
        for reason in unit.reasons:
            lines.append(f"- 原因: {reason}")
        if unit.support_eligible:
            low, high = unit.supportable_loss_range
            lines.append(f"- 基线: {unit.baseline:,.2f}；可支持区间: {low:,.2f}–{high:,.2f}")
            if unit.change_window:
                lines.append(f"- 可解释变化窗口: {unit.change_window[0]}–{unit.change_window[1]}")
            lines.append(
                f"- 对照: {', '.join(unit.control_units) or '无'}（共同前周 {len(unit.control_weeks)}）"
            )
            for week in unit.weekly:
                if week.effect is None:
                    lines.append(f"  - {week.week} [{week.role}] 不可估计：{'；'.join(week.notes)}")
                    continue
                lines.append(
                    f"  - {week.week} [{week.role}] 实际 {week.actual:,.2f} / 反事实 "
                    f"{week.counterfactual:,.2f} / 影响 {week.effect:,.2f}"
                    f"（{week.low:,.2f}–{week.high:,.2f}）"
                )
                for note in week.notes:
                    lines.append(f"    · {note}")
        lines.append(f"- 数据质量: {unit.quality.as_dict()}")
    if report.warnings:
        lines.append("")
        lines.append("## 警告")
        for warning in report.warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def _render_review(ds, review: ReviewReport) -> str:
    lines = ["# 缓冲通知审核", "", "## 汇总"]
    for key, value in review.summary.items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("## 通知")
    for r in review.notices:
        lines.append(f"### {r.notice_id} · {r.shop} · {r.owner or '未指定经营人'}")
        lines.append(f"- 结论: {r.as_dict()['审核结论']}（{r.decision}）")
        lines.append(
            f"- 覆盖: {r.period or '未注明'} → {', '.join(r.covered_weeks) or '无法对应'}；"
            f"判断: {r.coverage_state}"
        )
        lines.append(
            f"- 金额: 通知 {r.notice_amount}；已付 {r.paid:,.2f}；未付 {r.unpaid:,.2f}；"
            f"可支持 {r.allocated_low:,.2f}–{r.allocated_high:,.2f}"
        )
        for reason in r.reasons:
            lines.append(f"- 依据: {reason}")
        for action in r.actions:
            lines.append(f"- 下一步: {action}")
        for unknown in r.remaining_unknown:
            lines.append(f"- 仍未知: {unknown}")
    if review.unclaimed:
        lines.append("")
        lines.append("## 未获通知的影响")
        for row in review.unclaimed:
            lines.append(
                f"- {row['店铺']}/{row['经营人']} {row['周']} [{row['角色']}] "
                f"{row['可支持下限']:,}–{row['可支持上限']:,}"
            )
    return "\n".join(lines)


def _review_json(review: ReviewReport) -> dict:
    payload = review.as_dict()
    payload["_unit_statuses"] = {
        f"{shop}/{owner}": status for (shop, owner), status in review.unit_statuses.items()
    }
    return payload


def _review_from_json(payload: dict) -> ReviewReport:
    from .review import NoticeReview

    notices = []
    for row in payload.get("通知审核", []):
        notices.append(NoticeReview(
            notice_id=row.get("通知", ""), shop=row.get("店铺", ""), owner=row.get("经营人", ""),
            period=row.get("覆盖期", ""), notice_amount=row.get("通知金额"),
            paid=float(row.get("已支付") or 0.0), unpaid=float(row.get("未支付") or 0.0),
            notice_status=row.get("通知状态", ""), covered_weeks=list(row.get("覆盖周", [])),
            unit_status=row.get("经营人状态", ""),
            owner_match=bool(row.get("对象匹配", True)),
            impact_weeks=list(row.get("影响周", [])),
            uncovered_impact_weeks=list(row.get("未覆盖影响周", [])),
            covered_non_impact_weeks=list(row.get("覆盖但无影响周", [])),
            overlap_weeks=list(row.get("重叠施工周", [])),
            duplicate_weeks=list(row.get("重复覆盖周", [])),
            control_units=list(row.get("对照店铺", [])),
            control_weeks=list(row.get("对照周", [])),
            route_lost_weeks=list(row.get("失去通达的周", [])),
            support_low=float(row.get("可支持下限") or 0.0),
            support_high=float(row.get("可支持上限") or 0.0),
            allocated_low=float(row.get("去重后下限") or 0.0),
            allocated_high=float(row.get("去重后上限") or 0.0),
            coverage_state=row.get("覆盖判断", ""),
            decision=row.get("结论代码", ""), reasons=list(row.get("依据", [])),
            actions=list(row.get("可进入的下一步", [])),
            remaining_unknown=list(row.get("仍未知", [])),
        ))
    statuses = {}
    for key, status in payload.get("_unit_statuses", {}).items():
        shop, _, owner = key.partition("/")
        statuses[(shop, owner)] = status
    return ReviewReport(
        notices=notices, unclaimed=list(payload.get("未获通知的影响", [])),
        summary=dict(payload.get("汇总", {})), unit_statuses=statuses,
        warnings=list(payload.get("警告", [])),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="street_buffer",
        description="街巷修缮分期影响估计与缓冲资格审核",
        epilog=(
            "示例：\n"
            "  python3 -m street_buffer estimate --data fixtures\n"
            "  python3 -m street_buffer review --data fixtures --format text\n"
            "  python3 -m street_buffer merchant --data fixtures --shop S1\n"
            "  python3 -m street_buffer scenario --data examples --scenarios examples/scenarios.csv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="命令")

    estimate = commands.add_parser("estimate", help="估计直接受阻、外溢和可支持区间")
    estimate.add_argument("--format", choices=["json", "text"], default="json")

    review = commands.add_parser("review", help="审核已发缓冲通知")
    review.add_argument("--format", choices=["json", "text"], default="json")
    review.add_argument("--save", help="把当前审核结果写入 JSON，供补报重审")

    merchant = commands.add_parser("merchant", help="经营人视图：依据、未知、重算与下一步")
    merchant.add_argument("--shop", required=True, help="店铺")
    merchant.add_argument("--owner", help="经营人；省略时按店铺唯一经营人")

    reassess = commands.add_parser("reassess", help="按补报资料重审上一版审核结果")
    reassess.add_argument("--previous", required=True, help="上一版 review JSON")
    reassess.add_argument("--format", choices=["json", "text"], default="json")

    scenario = commands.add_parser("scenario", help="比较尚未执行的分期方案")
    scenario.add_argument("--scenarios", required=True, help="方案 CSV")
    scenario.add_argument("--format", choices=["json", "text"], default="json")

    for sub in (estimate, review, merchant, reassess, scenario):
        sub.add_argument("--data", default="fixtures", help="输入目录（含 primary.csv）")
        sub.add_argument("--min-pre-weeks", type=int, default=2)
        sub.add_argument("--trend-pre-weeks", type=int, default=3)
        sub.add_argument("--min-controls", type=int, default=1)
        sub.add_argument("--confidence", type=float, default=0.95)
        sub.add_argument("--bootstrap", type=int, default=500)
        sub.add_argument("--trend-tolerance", type=float, default=0.55)
        sub.add_argument("--seed", type=int, default=20261123)
        sub.add_argument("--exclude-spillover", action="store_true", help="只把直接受阻计入支持额度")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ds, report = _build(args)
    _print_warnings(ds)
    engine = ReviewEngine(ds, report)

    if args.command == "estimate":
        if args.format == "text":
            print(_render_estimate(report))
        else:
            _dump(report.as_dict())
        return 0

    if args.command == "review":
        review = engine.review_notices()
        payload = _review_json(review)
        if args.save:
            Path(args.save).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.format == "text":
            print(_render_review(ds, review))
        else:
            _dump(payload)
        return 0

    if args.command == "merchant":
        _dump(engine.merchant_view(args.shop, args.owner))
        return 0

    if args.command == "reassess":
        previous = _review_from_json(json.loads(Path(args.previous).read_text(encoding="utf-8")))
        rows = engine.reassess(previous)
        if args.format == "text":
            for row in rows:
                print(f"{row.notice_id or row.shop}: {row.status} / {row.trigger}")
                print(f"  {row.action}")
        else:
            _dump([row.as_dict() for row in rows])
        return 0

    if args.command == "scenario":
        planner = ScenarioPlanner(ds, report)
        compare = planner.compare(load_scenarios(args.scenarios))
        _dump(compare)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
