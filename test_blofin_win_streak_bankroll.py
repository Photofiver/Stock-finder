"""Tests for verified winning streak bankroll allocation (no broker calls)."""
import unittest
from decimal import Decimal
import blofin_live_hourly as bot


class BankrollWinStreakTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "bankroll_start_usdt": "20",
            "bankroll_pnl_baseline_usdt": "0",
            "realized_pnl_usdt": "0",
        }

    def close(self, pnl, state=None):
        state = self.state if state is None else state
        state["realized_pnl_usdt"] = str(
            Decimal(state["realized_pnl_usdt"]) + Decimal(str(pnl))
        )
        return bot.register_bankroll_win_streak(state, Decimal(str(pnl)))

    def test_award_only_on_tenth_consecutive_win(self):
        for _ in range(9):
            self.assertEqual(self.close("0.01"), 0)
        self.assertEqual(self.state["bankroll_win_streak"], 9)
        self.assertEqual(self.close("0.01"), Decimal("10"))
        self.assertEqual(self.state["bankroll_streak_bonus_usdt"], "10")
        self.assertEqual(bot.current_live_bankroll(self.state), Decimal("30.10"))

    def test_twenty_consecutive_wins_earn_two_awards(self):
        awards = [self.close("0.01") for _ in range(20)]
        self.assertEqual(awards.count(Decimal("10")), 2)
        self.assertEqual(self.state["bankroll_streak_bonus_usdt"], "20")
        self.assertEqual(self.state["bankroll_streak_awards"], 2)

    def test_loss_resets_the_streak(self):
        for _ in range(9):
            self.close("0.01")
        self.assertEqual(self.close("-0.02"), 0)
        for _ in range(9):
            self.assertEqual(self.close("0.01"), 0)
        self.assertEqual(self.close("0.01"), Decimal("10"))

    def test_flat_resets_the_streak(self):
        for _ in range(9):
            self.close("0.01")
        self.assertEqual(self.close("0"), 0)
        self.assertEqual(self.state["bankroll_win_streak"], 0)
        self.assertEqual(self.close("0.01"), 0)

    def test_never_exceeds_70_even_including_profits(self):
        awards = [self.close("0.01") for _ in range(50)]
        self.assertEqual(bot.current_live_bankroll(self.state), Decimal("70"))
        self.assertEqual(sum(awards), Decimal("49.50"))
        self.assertTrue(self.state["bankroll_streak_cap_reached"])

    def test_cap_stays_reached_after_future_loss(self):
        for _ in range(50):
            self.close("0.01")
        self.close("-5")
        for _ in range(10):
            self.assertEqual(self.close("0.01"), 0)
        self.assertEqual(self.state["bankroll_streak_awards"], 5)
        self.assertTrue(self.state["bankroll_streak_cap_reached"])
        self.assertLess(bot.current_live_bankroll(self.state), Decimal("70"))

    def test_partial_final_award_respects_total_cap(self):
        self.state["realized_pnl_usdt"] = "45"
        for _ in range(9):
            self.close("0.01")
        self.assertEqual(self.close("0.01"), Decimal("4.90"))
        self.assertEqual(bot.current_live_bankroll(self.state), Decimal("70"))
        self.assertTrue(self.state["bankroll_streak_cap_reached"])

    def test_missing_state_is_safe_and_does_not_backfill_old_wins(self):
        self.assertEqual(self.state.get("bankroll_win_streak", 0), 0)
        self.assertEqual(self.close("-0.01"), 0)
        self.assertEqual(self.state["bankroll_win_streak"], 0)


if __name__ == "__main__":
    unittest.main()
