"""Regression tests: LIVE SMA entries must not be gated on ADX."""
import unittest
from unittest.mock import patch

import blofin_ma10_ma20_live as ma


class FakeBot:
    SIGNAL_MINUTES = 10

    def __init__(self, candles):
        self.candles = candles
        self.saved = None

    def fetch_signal_bars(self, inst):
        return self.candles[inst]

    @staticmethod
    def bar_close_ms(bar):
        return bar["close_ms"]

    @staticmethod
    def get_tracked_positions(state):
        return state.setdefault("positions", {})

    @staticmethod
    def get_open_positions():
        return []

    @staticmethod
    def risk_stop_active(state):
        return False

    @staticmethod
    def sync_all_tracked_positions(state):
        pass

    @staticmethod
    def evaluate_all_tracked_exit_signals(state, expected_close_ms):
        pass

    @staticmethod
    def now_ms():
        return 12345

    def save_state(self, state):
        self.saved = state.copy()


def candles_with_cross(last_close):
    values = [10.0] * 20 + [last_close]
    return [
        {"ts": i * 600000, "close_ms": (i + 1) * 600000, "c": value}
        for i, value in enumerate(values)
    ]


class RemoveAdxLiveTests(unittest.TestCase):
    def test_long_short_cross_without_adx_data(self):
        for last_close, side in ((12.0, "LONG"), (8.0, "SHORT")):
            with self.subTest(side=side):
                bars = candles_with_cross(last_close)
                bot = FakeBot({"A-USDT": bars})
                signal = ma.crossover(bot, "A-USDT", bars[-1]["close_ms"])
                self.assertEqual(signal["side"], side)
                self.assertNotIn("adx_entry_allowed", signal)
                self.assertNotIn("adx14", signal)
                self.assertIn("ma10_live_cross_level", signal)

    def test_scan_considers_signal_without_adx(self):
        bars = candles_with_cross(12.0)
        bot = FakeBot({"A-USDT": bars})
        state = {"positions": {}}
        selected = []

        def simulated_open(bot, state, signal, rank, instruments):
            selected.append(signal)
            return True

        with patch.object(ma, "open_position", side_effect=simulated_open):
            ma.run(
                bot, state, ["A-USDT"], {}, {"A-USDT": {}},
                bars[-1]["close_ms"], lambda insts, close: None,
            )

        self.assertEqual(state["ma_cross_last_scan"]["status"], "OPENED")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["side"], "LONG")
        self.assertNotIn("adx_filter", selected[0])

    def test_entry_risk_controls_still_run(self):
        """The first remaining entry gate is the existing drawdown control."""
        class StopBot:
            @staticmethod
            def risk_stop_active(state):
                return True
        signal = {"inst": "A-USDT", "side": "LONG"}
        self.assertFalse(ma.open_position(StopBot(), {}, signal, 1, {}))


if __name__ == "__main__":
    unittest.main()
