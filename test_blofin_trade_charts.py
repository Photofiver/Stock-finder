"""Offline regression tests: chart rendering must not require BloFin credentials."""
import math
import tempfile
import unittest
from pathlib import Path

import blofin_trade_charts as charts


class TradeChartsTests(unittest.TestCase):
    def setUp(self):
        self.base = 1791810000000
        self.bars = []
        for i in range(110):
            ts = self.base + i * charts.BAR_MS
            price = 0.025 + i * 0.000012 + 0.0001 * math.sin(i / 3)
            next_price = price + 0.00003 * math.cos(i)
            self.bars.append([
                str(ts), str(price),
                str(max(price, next_price) + 0.000015),
                str(min(price, next_price) - 0.000015),
                str(next_price), str(300 + i * 5),
                "0", "0", "1",
            ])
        self.when = self.base + (len(self.bars) - 1) * charts.BAR_MS + 60000

    def test_candle_selection_does_not_include_future(self):
        future = [
            str(self.when + charts.BAR_MS * 2),
            ".03", ".031", ".029", ".030", "10", "0", "0", "1",
        ]
        found = charts._normalize(self.bars + [future], self.when)
        self.assertEqual(len(found), 110)

    def test_entry_and_exit_png_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            for event in ("ENTRY", "EXIT"):
                path = charts.render_trade_chart(
                    self.bars, "BTC-USDT", "LONG", event, self.when,
                    float(self.bars[-1][4]), self.when - 10 * 60000,
                    entry_price=float(self.bars[-3][4]),
                    tp=0.028, sl=0.025, pnl=-0.07, reason="test",
                    chart_root=Path(tmp),
                )
                self.assertTrue(Path(path).is_file(), path)
                self.assertGreater(Path(path).stat().st_size, 15000)
                self.assertIn(event.lower(), path)

    def test_indicators_have_expected_lengths(self):
        close = [float(row[4]) for row in self.bars]
        self.assertEqual(len(charts._rsi(close)), len(close))
        self.assertTrue(math.isnan(charts._sma(close, 20)[0]))
        self.assertTrue(math.isfinite(charts._sma(close, 20)[-1]))


if __name__ == "__main__":
    unittest.main()
