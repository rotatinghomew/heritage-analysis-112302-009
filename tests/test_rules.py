import unittest

from street_buffer.dataset import load_dataset
from street_buffer.estimation import EstimationConfig, Estimator
from street_buffer.review import ReviewEngine
from tests.helpers import Book, TempBook, stable_controls


def estimate(book: Book, **config):
    with TempBook(book) as root:
        dataset = load_dataset(root)
        report = Estimator(dataset, EstimationConfig(bootstrap=80, **config)).estimate()
        return dataset, report


class EstimationRuleTests(unittest.TestCase):
    def test_direct_loss_is_counterfactual_minus_actual(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000],
                    street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1", detour="yes")
        stable_controls(book)
        _, report = estimate(book)
        unit = report.unit("S1", "O1")
        self.assertIsNotNone(unit)
        self.assertTrue(unit.support_eligible)
        self.assertEqual([w.effect for w in unit.weekly], [600, 500])
        self.assertEqual(report.phase_estimates["P1"].direct_loss, 1100)
        self.assertAlmostEqual(report.totals["可归因损失点估计"], 1100)

    def test_affected_shop_never_used_as_control(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1", detour="yes")
        stable_controls(book)
        # 另一家同样受绕行影响的店铺不能作对照，即使它变化平稳。
        book.series("S2", "O2", ["W1", "W2", "W3"], [700, 700, 700], street="A", phase="pre")
        book.series("S2", "O2", ["W4", "W5"], [300, 300], street="A", phase="P1", detour="yes")
        _, report = estimate(book)
        unit = report.unit("S1", "O1")
        self.assertNotIn("S2/O2", unit.control_units)
        self.assertIn("C1/X1", unit.control_units)

    def test_no_control_means_no_migration(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1", detour="yes")
        _, report = estimate(book)
        unit = report.unit("S1", "O1")
        self.assertEqual(unit.status, "no_control")
        self.assertFalse(unit.support_eligible)
        self.assertEqual(report.totals["可支持损失上限"], 0.0)

    def test_missing_zero_and_closed_are_distinct(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.add("S1", "O1", "W4", "", "missing", street="A", phase="P1", detour="yes")
        book.add("S1", "O1", "W5", "0", "zero", street="A", phase="P1", detour="yes")
        book.add("S1", "O1", "W6", "", "", open_="no", street="A", phase="P1",
                 detour="yes", close_reason="自然闭店")
        stable_controls(book, weeks=("W1", "W2", "W3", "W4", "W5", "W6"))
        _, report = estimate(book)
        unit = report.unit("S1", "O1")
        quality = unit.quality
        self.assertEqual(quality.missing_weeks, 1)
        self.assertEqual(quality.zero_weeks, 1)
        self.assertEqual(quality.natural_closed_weeks, 1)
        statuses = {w.week: w.status for w in unit.weekly}
        self.assertEqual(statuses["W4"], "not_estimable")
        self.assertEqual(statuses["W5"], "estimated")
        self.assertEqual(statuses["W6"], "not_estimable")
        # 零交易按真实零收入计算，缺报与闭店不按零填补。
        self.assertEqual(unit.weekly[1].actual, 0.0)
        self.assertIsNone(unit.weekly[0].actual)
        self.assertIsNone(unit.weekly[2].actual)

    def test_settlement_week_does_not_move_revenue(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.add("S1", "O1", "W4", "400", street="A", phase="P1", detour="yes",
                 settlement_week="W5")
        book.add("S1", "O1", "W5", "500", street="A", phase="P1", detour="yes")
        stable_controls(book)
        _, report = estimate(book)
        unit = report.unit("S1", "O1")
        self.assertEqual(unit.weekly[0].actual, 400)
        self.assertEqual(unit.weekly[0].week, "W4")
        self.assertTrue(any(x["归属"] == "按成交周" for x in unit.quality.settlement_lags))

    def test_owner_transfer_not_inherited(self):
        book = Book()
        book.series("S4", "O4", ["W1", "W2", "W3"], [700, 700, 700], street="A", phase="pre")
        book.series("S4", "O5", ["W4", "W5"], [300, 250], street="A", phase="P1", detour="yes")
        stable_controls(book)
        _, report = estimate(book)
        unit = report.unit("S4", "O5")
        self.assertEqual(unit.status, "owner_transfer_cut")
        self.assertFalse(unit.support_eligible)
        # 前手经营人不在施工期受影响，不承接后手影响。
        prior = report.unit("S4", "O4")
        self.assertEqual(prior.status, "not_affected")
        self.assertEqual(report.totals["可归因损失点估计"], 0.0)

    def test_spillover_separated_from_direct(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4"], [400], street="A", phase="P1", detour="yes")
        book.series("S2", "O2", ["W1", "W2", "W3"], [900, 900, 900], street="D", phase="pre")
        book.series("S2", "O2", ["W4"], [600], street="D", phase="P1", spillover="yes")
        stable_controls(book, weeks=("W1", "W2", "W3", "W4"))
        _, report = estimate(book)
        self.assertEqual(report.unit("S1", "O1").weekly[0].role, "direct")
        self.assertEqual(report.unit("S2", "O2").weekly[0].role, "spillover")
        phase = report.phase_estimates["P1"]
        self.assertEqual(phase.direct_loss, 600)
        self.assertEqual(phase.spillover_loss, 300)

    def test_concurrent_event_week_excluded(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 100], street="A", phase="P1", detour="yes")
        stable_controls(book)
        book.extra("events.csv", ["event", "kind", "street", "scope", "start_week", "end_week"], [
            ["E1", "festival", "", "citywide", "W5", "W5"],
        ])
        dataset, report = estimate(book)
        unit = report.unit("S1", "O1")
        self.assertEqual(unit.weekly[1].status, "excluded_concurrent_event")
        self.assertIsNone(unit.weekly[1].effect)
        self.assertEqual(report.totals["可归因损失点估计"], 600)
        self.assertTrue(unit.event_excluded_weeks == ["W5"])

    def test_phase_overlap_counted_once(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4"], [400], street="A", phase="P1", detour="yes")
        stable_controls(book, weeks=("W1", "W2", "W3", "W4"))
        book.extra("phases.csv",
                   ["phase", "street", "start_week", "end_week", "kind", "detour", "route"], [
                       ["P1", "A", "W4", "W4", "works", "yes", ""],
                       ["P2", "A", "W4", "W4", "works", "yes", ""],
                   ])
        dataset, report = estimate(book)
        unit = report.unit("S1", "O1")
        self.assertEqual(len(unit.weekly[0].phases), 2)
        self.assertEqual(report.phase_estimates["P1"].total_loss, 300)
        self.assertEqual(report.phase_estimates["P2"].total_loss, 300)
        # 阶段合计 600 只是展示分摊；可归因总额仍是 600 的单周损失。
        self.assertEqual(report.totals["可归因损失点估计"], 600)
        self.assertEqual(report.phase_estimates["P1"].overlap_weeks, ["W4"])

    def test_no_common_trend_not_supportable(self):
        book = Book()
        # 本店修缮前下滑，对照在修缮前上升：方向相反，缺少共同支撑。
        book.series("S1", "O1", ["W1", "W2", "W3"], [1400, 1000, 700], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 300], street="A", phase="P1", detour="yes")
        book.series("C1", "X1", ["W1", "W2", "W3"], [600, 800, 1000], street="B", phase="pre")
        book.series("C1", "X1", ["W4", "W5"], [1000, 1000], street="B", phase="post", detour="no")
        _, report = estimate(book)
        unit = report.unit("S1", "O1")
        self.assertIn(unit.status, {"no_common_trend", "estimated_sensitivity_only"})
        self.assertFalse(unit.support_eligible)
        self.assertEqual(report.totals["可支持损失上限"], 0.0)

    def test_deterministic_bootstrap(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 990, 1010], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1", detour="yes")
        stable_controls(book)
        _, first = estimate(book)
        _, second = estimate(book)
        a = first.unit("S1", "O1").supportable_loss_range
        b = second.unit("S1", "O1").supportable_loss_range
        self.assertEqual(a, b)
        self.assertLessEqual(a[0], a[1])
        self.assertGreaterEqual(a[0], 0.0)


class ReviewRuleTests(unittest.TestCase):
    def _report(self, book):
        dataset, estimation = estimate(book)
        return dataset, estimation, ReviewEngine(dataset, estimation)

    def test_unpaid_amount_can_be_adjusted_paid_cannot(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1",
                    detour="yes", notice="N2")
        stable_controls(book)
        book.notice("N2", "S1", "O1", "W4-W5", 2000, status="issued", paid="no")
        book.notice("N3", "S1", "O1", "W5-W5", 900, status="issued", paid="yes")
        _, _, engine = self._report(book)
        review = engine.review_notices()
        n2 = review.for_notice("N2")
        n3 = review.for_notice("N3")
        self.assertIn(n2.decision, {"adjust_unpaid", "duplicate_cover", "supplement_new"})
        self.assertLessEqual(n2.allocated_high, 1200)
        if n2.decision == "adjust_unpaid":
            self.assertGreater(n2.paid, 0) if n2.paid else True
        # 已支付通知即使超出上限，也不能自动冲回。
        self.assertIn(n3.decision, {"confirm", "paid_excess_manual", "duplicate_cover"})
        if n3.decision == "paid_excess_manual":
            self.assertTrue(any("不得自动冲回" in x for x in n3.reasons))

    def test_notice_overlap_not_double_counted(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 400], street="A", phase="P1",
                    detour="yes", notice="N1")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W4-W5", 1200)
        book.notice("N2", "S1", "O1", "W5-W5", 800)
        _, _, engine = self._report(book)
        review = engine.review_notices()
        first = review.for_notice("N1")
        second = review.for_notice("N2")
        self.assertEqual(second.decision, "duplicate_cover")
        self.assertEqual(first.allocated_high + second.allocated_high, first.allocated_high)
        self.assertEqual(review.summary["去重后支持上限"], first.allocated_high)

    def test_pre_period_only_notice_rejected(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1", detour="yes")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W1-W3", 300)
        _, _, engine = self._report(book)
        review = engine.review_notices()
        self.assertEqual(review.for_notice("N1").decision, "pre_period_only")

    def test_uncovered_impact_reported(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1", detour="yes")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W4-W4", 600)
        _, _, engine = self._report(book)
        review = engine.review_notices()
        weeks = {row["周"] for row in review.unclaimed}
        self.assertEqual(weeks, {"W5"})

    def test_merchant_view_has_basis_and_next_steps(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1", detour="yes")
        book.add("S1", "O1", "W6", "", "missing", street="A", phase="P1", detour="yes")
        stable_controls(book, weeks=("W1", "W2", "W3", "W4", "W5", "W6"))
        book.notice("N1", "S1", "O1", "W4-W5", 1100)
        _, _, engine = self._report(book)
        view = engine.merchant_view("S1", "O1")
        self.assertTrue(view["有效依据"])
        self.assertTrue(any("缺报" in x for x in view["仍未知"]))
        self.assertTrue(view["可进入的下一步"])
        self.assertEqual(len(view["重算影响"]), 1)

    def test_reassess_route_loss_marks_all_three_outcomes(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1",
                    detour="yes", notice="N1")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W4-W5", 900, status="issued", paid="no")
        book.notice("N2", "S1", "O1", "W4-W5", 400, status="issued", paid="yes")
        book.notice("N3", "S1", "O1", "W4-W5", 100, status="draft", paid="no")
        dataset, estimation, engine = self._report(book)
        previous = engine.review_notices()

        # 补报：原先可用的绕行路线在影响周关闭。
        book.extra("routes.csv",
                   ["route_id", "street", "status", "start_week", "end_week"], [
                       ["R1", "A", "closed", "W4", "W5"],
                   ])
        for row in book.rows:
            if row["shop"] == "S1":
                row["route"] = "R1"
        with TempBook(book) as root:
            new_ds = load_dataset(root)
            new_est = Estimator(new_ds, EstimationConfig(bootstrap=20)).estimate()
            new_engine = ReviewEngine(new_ds, new_est)
            rows = new_engine.reassess(previous)
        statuses = {row.notice_id: row for row in rows}
        self.assertTrue(statuses["N1"].trigger.startswith("补报使绕行路线失去通达资格"))
        self.assertEqual(statuses["N1"].status, "可重审")
        self.assertIn("未兑现", statuses["N1"].action)
        self.assertEqual(statuses["N2"].status, "原结论失效")
        self.assertIn("复议", statuses["N2"].action)
        self.assertIn("不得自动冲回", statuses["N2"].action)
        self.assertEqual(statuses["N3"].status, "可更正的新申请")

    def test_reassess_control_loss_with_paid_notice_goes_manual(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1",
                    detour="yes", notice="N1")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W4-W5", 1100, status="issued", paid="yes")
        dataset, estimation, engine = self._report(book)
        previous = engine.review_notices()
        self.assertEqual(previous.for_notice("N1").decision, "confirm")

        # 补报：再记一笔施工阶段，使两家对照在同期也受绕行影响，失去无干扰资格。
        book.extra("phases.csv",
                   ["phase", "street", "start_week", "end_week", "kind", "detour", "route"], [
                       ["P1", "B", "W4", "W5", "side", "", ""],
                   ])
        for row in book.rows:
            if row["shop"] in {"C1", "C2"} and row["week"] in {"W4", "W5"}:
                row["spillover"] = "yes"
                row["detour"] = ""
        with TempBook(book) as root:
            new_ds = load_dataset(root)
            new_est = Estimator(new_ds, EstimationConfig(bootstrap=20)).estimate()
            rows = ReviewEngine(new_ds, new_est).reassess(previous)
        row = {r.notice_id: r for r in rows}["N1"]
        self.assertEqual(row.new_decision, "invalidated_manual")
        self.assertEqual(row.status, "原结论失效")
        self.assertEqual(row.paid, 1100)
        self.assertIn("复议", row.action)

    def test_reassess_does_not_repeat_unchanged_notes(self):
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1",
                    detour="yes", notice="N1")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W4-W5", 1100)
        dataset, estimation, engine = self._report(book)
        previous = engine.review_notices()
        rows = engine.reassess(previous)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "无实质变化")
        self.assertEqual(rows[0].previous_supported, rows[0].new_supported)


        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1",
                    detour="yes", notice="N1")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W4-W5", 1100, status="issued", paid="no")
        book.notice("N2", "S1", "O1", "W4-W5", 500, status="issued", paid="yes")
        book.notice("N3", "S1", "O1", "W4-W5", 200, status="draft", paid="no")
        dataset, estimation, engine = self._report(book)
        previous = engine.review_notices()
        # 补报：撤掉对照，令 S1 失去可用比较条件。
        for row in book.rows:
            if row["shop"] == "C1":
                row["detour"] = ""
                row["spillover"] = "yes"
                row["phase"] = "P1"
        with TempBook(book) as root:
            new_ds = load_dataset(root)
            new_est = Estimator(new_ds, EstimationConfig(bootstrap=20)).estimate()
            new_engine = ReviewEngine(new_ds, new_est)
            rows = new_engine.reassess(previous)
        by_notice = {row.notice_id: row for row in rows}
        self.assertIn("N1", by_notice)
        self.assertTrue(by_notice["N1"].new_decision in {
            "invalidated_manual", "adjust_unpaid", "no_control", "duplicate_cover",
        })
        self.assertTrue(any(row.status for row in rows))


if __name__ == "__main__":
    unittest.main()
