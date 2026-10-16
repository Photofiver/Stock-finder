"""Regression tests: LIVE SMA no ADX; current permitted ranks are 51..100.

Fixtures include a confirmed-candle timestamp and all current strength
indicators. The LIVE strategy is NOT changed to make these tests pass.
"""
import unittest
from unittest.mock import MagicMock, patch

import blofin_ma10_ma20_live as ma


class FakeBot:
    SIGNAL_MINUTES = 5
    SIGNAL_LABEL = "5m"
    SIGNAL_MS = 300000

    def __init__(self, candles):
        self.candles = candles
        self.saved = None

    def fetch_signal_bars(self, inst):
        return self.candles[inst]

    @staticmethod
    def bar_close_ms(bar):
        return bar["close_ms"]

    @staticmethod
    def stochastic_kd_series(bars, k_period, d_period):
        assert (k_period, d_period) == (14, 3)
        return ([40.0], [60.0]) if bars[-1]["c"] >= 10 else ([60.0], [40.0])

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


def candles_with_cross(last_close, interval_ms=300000):
    prices = [10.0] * 20 + [last_close]
    return [
        {"ts": i * interval_ms, "close_ms": (i + 1) * interval_ms,
         "h": price * 1.0005, "l": price * 0.9995,
         "c": price, "rsi": 55.0 if last_close >= 10 else 45.0}
        for i, price in enumerate(prices)
    ]


def confirmed(bot):
    return lambda insts, expected: {inst: bot.candles[inst] for inst in insts}


def complete_signal(side="LONG", rsi=55.0):
    return {
        "inst": "A-USDT", "side": side,
        "signal_close_ms": 300000,
        "close": 10.01 if side == "LONG" else 9.99,
        "ma10": 10.0,
        "sma_separation_pct": 0.05,
        "stoch_k": 40 if side == "LONG" else 60,
        "stoch_d": 60 if side == "LONG" else 40,
        "rsi14": rsi,
        "sr_levels": {"resistance": [], "support": []},
    }


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
                self.assertIsNone(ma.entry_rsi_rejection(signal))
                self.assertIsNone(ma.entry_strength_rejection(signal))

    def test_five_minute_sma_rank_51_opens_without_adx(self):
        flat = candles_with_cross(10.0)
        crossed = candles_with_cross(10.01)
        names = [f"RANK{i}-USDT" for i in range(1, 53)]
        bot = FakeBot({name: crossed if i == 51 else flat
                       for i, name in enumerate(names, 1)})
        state = {"positions": {}}
        opened = []

        with patch.object(ma, "open_position",
                          side_effect=lambda b, s, sig, rank, m: opened.append((rank, sig)) or True):
            ma.run(
                bot, state, names, {}, {name: {} for name in names},
                crossed[-1]["close_ms"], confirmed(bot),
            )

        self.assertEqual(state["ma_cross_last_scan"]["status"], "OPENED")
        self.assertEqual([row[0] for row in opened], [51])
        self.assertEqual(opened[0][1]["side"], "LONG")
        self.assertNotIn("adx_filter", opened[0][1])

    def test_only_current_rank_51_through_100_permitted(self):
        self.assertEqual(ma.ENTRY_RANK_MIN, 51)
        self.assertEqual(ma.EXTENDED_ENTRY_RANK_MAX, 100)
        self.assertIsNone(ma.rank_entry_rejection(51))
        self.assertIsNone(ma.rank_entry_rejection(100))
        for rank in (0, 1, 2, 3, 50, 101, None, "51", True):
            with self.subTest(rank=rank):
                self.assertIsNotNone(ma.rank_entry_rejection(rank))
                bot = MagicMock()
                self.assertFalse(
                    ma.open_position(bot, {}, complete_signal(), rank, {})
                )
                bot.private_request.assert_not_called()
                bot.get_open_positions.assert_not_called()
                bot.risk_stop_active.assert_not_called()

    def test_current_rank_50_is_rejected_before_rank_51(self):
        flat = candles_with_cross(10.0)
        crossed = candles_with_cross(10.01)
        names = [f"RANK{i}-USDT" for i in range(1, 53)]
        bot = FakeBot({name: crossed if i in (1, 50, 51) else flat
                       for i, name in enumerate(names, 1)})
        state = {"positions": {}}
        opened = []
        with patch.object(ma, "open_position",
                          side_effect=lambda b, s, sig, rank, m: opened.append(rank) or True):
            ma.run(bot, state, names, {}, {n: {} for n in names},
                   crossed[-1]["close_ms"], confirmed(bot))
        self.assertEqual(opened, [51])
        rejected = [e["rank"] for e in state["ma_cross_last_scan"]["events"]
                    if e.get("status") == "RANK_NOT_ALLOWED_TRY_NEXT"]
        self.assertEqual(rejected, [1, 50])

    def test_rank_100_is_permitted_if_other_99_have_no_cross(self):
        flat = candles_with_cross(10.0)
        crossed = candles_with_cross(10.01)
        names = [f"RANK{i}-USDT" for i in range(1, 101)]
        bot = FakeBot({name: crossed if i == 100 else flat
                       for i, name in enumerate(names, 1)})
        opened = []
        state = {"positions": {}}
        with patch.object(ma, "open_position",
                          side_effect=lambda b, s, sig, rank, m: opened.append(rank) or True):
            ma.run(bot, state, names, {}, {n: {} for n in names},
                   crossed[-1]["close_ms"], confirmed(bot))
        self.assertEqual(opened, [100])

    def test_entry_risk_controls_still_run(self):
        """A valid rank/RSI/Stoch/SR signal still hits the drawdown gate."""
        bot = MagicMock()
        bot.risk_stop_active.return_value = True
        bot.get_tracked_positions.return_value = {}
        bot.get_open_positions.return_value = []
        self.assertFalse(ma.open_position(
            bot, {}, complete_signal(), 51, {},
        ))
        bot.risk_stop_active.assert_called_once()
        bot.private_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
