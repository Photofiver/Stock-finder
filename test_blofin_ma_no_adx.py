"""Regression tests: LIVE SMA entries must not be gated on ADX."""
import unittest
from unittest.mock import patch

import blofin_ma10_ma20_live as ma


class FakeBot:
    SIGNAL_MINUTES = 10
    SIGNAL_LABEL = "10m"

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


class FakeBot5(FakeBot):
    SIGNAL_MINUTES = 5
    SIGNAL_LABEL = "5m"


def candles_with_cross(last_close, interval_ms=600000):
    values = [10.0] * 20 + [last_close]
    return [
        {"ts": i * interval_ms, "close_ms": (i + 1) * interval_ms,
         "h": value * 1.0005, "l": value * 0.9995,
         "c": value, "rsi": 55.0}
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
        # A small real crossover stays within the independent MA10 1% entry cap.
        bars = candles_with_cross(10.01)
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

    def test_five_minute_sma_cross_opens_without_adx(self):
        bars = candles_with_cross(10.01, interval_ms=300000)
        bot = FakeBot5({"A-USDT": bars})
        state = {"positions": {}}
        selected = []

        with patch.object(ma, "open_position", side_effect=lambda *args: selected.append(args[2]) or True):
            ma.run(
                bot, state, ["A-USDT"], {}, {"A-USDT": {}},
                bars[-1]["close_ms"], lambda insts, close: None,
            )

        self.assertEqual(state["ma_cross_last_scan"]["status"], "OPENED")
        self.assertEqual(state["ma_cross_last_scan"]["strategy"], "SMA10_SMA20_5m")
        self.assertEqual(selected[0]["side"], "LONG")
        self.assertEqual(bars[-1]["close_ms"], 21 * 300000)

    def test_only_rank_one_or_two_can_open_live_orders(self):
        from unittest.mock import MagicMock
        for rank in (0, 3, 4, 5, 6, 7, 8, None, "1"):
            with self.subTest(rank=rank):
                bot = MagicMock()
                result = ma.open_position(
                    bot, {}, {"inst": "BLOCK-USDT", "side": "LONG"}, rank, {},
                )
                self.assertFalse(result)
                bot.private_request.assert_not_called()
                bot.get_open_positions.assert_not_called()
                bot.risk_stop_active.assert_not_called()
        self.assertIsNone(ma.rank_entry_rejection(1))
        self.assertIsNone(ma.rank_entry_rejection(2))

    def test_third_through_seventh_rank_signals_never_open(self):
        # All coins are scanned for diagnostics, but entry must not proceed
        # for lower ranked signals even when the SMA cross is valid.
        flat = candles_with_cross(10.0)
        crossed = candles_with_cross(10.01)
        names = [f"RANK{i}-USDT" for i in range(1, 8)]
        bot = FakeBot({name: flat if i < 3 else crossed
                       for i, name in enumerate(names, 1)})
        state = {"positions": {}}
        opened = []
        with patch.object(ma, "open_position",
                          side_effect=lambda *args: opened.append(args[3]) or True):
            ma.run(bot, state, names, {}, {n: {} for n in names},
                   crossed[-1]["close_ms"], lambda insts, close: None)
        self.assertEqual(opened, [])
        events = state["ma_cross_last_scan"]["events"]
        self.assertEqual(
            [e["rank"] for e in events if e.get("status") == "RANK_NOT_ALLOWED_TRY_NEXT"],
            [3, 4, 5, 6, 7],
        )
        self.assertEqual(state["ma_cross_last_scan"]["status"], "RANK_NOT_ALLOWED_TRY_NEXT")
        self.assertEqual(len(state["ma_cross_last_scan"]["signals"]), 5)

    def test_second_rank_can_open_even_if_third_has_signal(self):
        flat = candles_with_cross(10.0)
        crossed = candles_with_cross(10.01)
        bot = FakeBot({"FIRST-USDT": flat, "SECOND-USDT": crossed,
                       "THIRD-USDT": crossed})
        state = {"positions": {}}
        opened = []
        with patch.object(ma, "open_position",
                          side_effect=lambda *args: opened.append(args[3]) or True):
            ma.run(
                bot, state, ["FIRST-USDT", "SECOND-USDT", "THIRD-USDT"],
                {}, {"FIRST-USDT": {}, "SECOND-USDT": {}, "THIRD-USDT": {}},
                crossed[-1]["close_ms"], lambda insts, close: None,
            )
        self.assertEqual(opened, [2])
        self.assertEqual(state["ma_cross_last_scan"]["status"], "OPENED")
        self.assertEqual(state["ma_cross_last_scan"]["selected"]["inst"], "SECOND-USDT")

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
