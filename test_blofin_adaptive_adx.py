"""Offline regression tests: direction-specific adaptive ADX; no exchange access."""
import unittest

import blofin_adaptive_adx as adx


def trade(side, adx_value, delta, pnl, index, version=True):
    return {
        "side": side,
        "strategy": f"SMA10_SMA20_{side}_10m",
        "signal_snapshot": {
            "adx14": adx_value,
            "adx14_previous": adx_value - delta,
            "adx_filter_version": adx.VERSION if version else "legacy",
        },
        "gross_pnl_usdt": pnl / 100 * 10.0,
        "notional_usdt": "10",
        "opened_ms": 1000000 + index * 100000,
        "closed_ms": 1100000 + index * 100000,
    }


class AdaptiveAdxTests(unittest.TestCase):
    def mixed(self, side="LONG", validation_wins_below=True):
        rows = [
            trade(side, 28, 2, 1.0, i)
            if i % 5 < 3 else trade(side, 35, -2, -1.0, i)
            for i in range(20)
        ]
        for i in range(8):
            winning = i < 5
            low_adx = winning == validation_wins_below
            rows.append(trade(side, 28 if low_adx else 35,
                              2 if low_adx else -2,
                              1.0 if winning else -1.0, 20 + i))
        return rows

    def test_fallback_without_completed_outcomes(self):
        policy = adx.policy_for_side([], "LONG")
        self.assertEqual(policy["limit"], 38)
        self.assertEqual(policy["trend"], "ANY")
        self.assertIn("FALLBACK", policy["status"])
        self.assertTrue(adx.decision_for_signal([], {"side": "LONG", "adx14": 30})["allowed"])
        self.assertFalse(adx.decision_for_signal([], {"side": "SHORT", "adx14": 38})["allowed"])

    def test_separate_long_short_and_walk_forward_validation(self):
        history = self.mixed("LONG")
        policy = adx.policy_for_side(history, "LONG")
        self.assertEqual(policy["status"], "VALIDATED_ADAPTIVE")
        self.assertEqual(policy["usable_completed_trades"], 28)
        self.assertLessEqual(policy["limit"], 38)
        self.assertGreater(policy["validation_gross_pnl_improvement_pct_sum"], 0)
        self.assertEqual(adx.policy_for_side(history, "SHORT")["limit"], 38)
        good = adx.decision_for_signal(history, {
            "side": "LONG", "adx14": 28, "adx14_previous": 26,
        })
        bad = adx.decision_for_signal(history, {
            "side": "LONG", "adx14": 35, "adx14_previous": 37,
        })
        self.assertTrue(good["allowed"])
        self.assertFalse(bad["allowed"])

    def test_validation_failure_keeps_fixed_fallback(self):
        policy = adx.policy_for_side(self.mixed(validation_wins_below=False), "LONG")
        self.assertEqual(policy["limit"], 38)
        self.assertEqual(policy["status"], "FALLBACK_HELD_OUT_VALIDATION_FAILED")

    def test_only_this_strategy_version_and_completed_gross_results(self):
        rows = self.mixed("SHORT")
        self.assertEqual(len(adx.eligible_results(rows, "SHORT")), 28)
        rows[0]["signal_snapshot"]["adx_filter_version"] = "legacy"
        rows[1]["strategy"] = "OTHER_STRATEGY"
        rows[2]["closed_ms"] = None
        rows[3]["gross_pnl_usdt"] = None
        self.assertEqual(len(adx.eligible_results(rows, "SHORT")), 24)
        self.assertEqual(len(adx.eligible_results(rows, "LONG")), 0)

    def test_no_learning_from_untraded_adx_above_38(self):
        history = self.mixed("LONG")
        signal = {"side": "LONG", "adx14": 39, "adx14_previous": 38}
        self.assertFalse(adx.decision_for_signal(history, signal)["allowed"])
        self.assertTrue(all(value <= 38 for value in adx.LIMIT_CANDIDATES))


if __name__ == "__main__":
    unittest.main()
