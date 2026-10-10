"""Tests for one-way broker trailing stop 1% beyond LIVE SMA10."""
import unittest
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import blofin_ma10_ma20_live as ma


class BrokerStub:
    ROUND_CEILING = ROUND_CEILING
    ROUND_FLOOR = ROUND_FLOOR
    MAX_ENTRY_QUOTE_AGE_MS = 30000

    def __init__(self, last="100"):
        self.last = last
        self.amendments = []
        self.closes = []
        self.cancels = []

    @staticmethod
    def d(v):
        return Decimal(str(v))

    @staticmethod
    def clean_decimal(v):
        return str(v)

    @staticmethod
    def price_step(v, tick, rounding):
        tick = Decimal(str(tick))
        return (v / tick).quantize(Decimal("1"), rounding=rounding) * tick

    @staticmethod
    def bar_close_ms(row):
        return row["close_ms"]

    @staticmethod
    def now_ms():
        return 100000

    def market_get(self, route, query):
        self.assert_called_on_quote = True
        return [{"instId": "A-USDT", "last": self.last, "ts": "100000"}]

    def private_request(self, method, route, body):
        self.amendments.append((method, route, body))
        return [{"code": "0"}]

    def cancel_specific_tpsl(self, position):
        self.cancels.append(position["tpsl_id"])

    def _run_for_tracked_position(self, state, inst, func, reason):
        self.closes.append((inst, reason))

    @staticmethod
    def close_tracked_position(*args):
        pass


def bars(value=100):
    return [
        {"c": float(value), "close_ms": (i + 1) * 600000}
        for i in range(21)
    ]


def pos(side, stop):
    return {
        "inst": "A-USDT",
        "side": side,
        "sl": str(stop),
        "tp": "102" if side == "LONG" else "98",
        "signal_close_ms": 1,
        "tpsl_id": "test-order",
    }


class Ma10TrailingStopTests(unittest.TestCase):
    def test_long_stop_exactly_one_percent_below_live_ma10(self):
        b = BrokerStub()
        target = ma.ma10_trailing_one_percent_stop_price(b, "LONG", 900, Decimal("0.00000001"))
        ma10_when_triggered = (Decimal(900) + target) / 10
        self.assertAlmostEqual(float(target / ma10_when_triggered), 0.99, places=8)
        self.assertLess(target, 100)

    def test_short_stop_exactly_one_percent_above_live_ma10(self):
        b = BrokerStub()
        target = ma.ma10_trailing_one_percent_stop_price(b, "SHORT", 900, Decimal("0.00000001"))
        ma10_when_triggered = (Decimal(900) + target) / 10
        self.assertAlmostEqual(float(target / ma10_when_triggered), 1.01, places=8)
        self.assertGreater(target, 100)

    def test_long_must_not_lower_stop(self):
        b = BrokerStub()
        p = pos("LONG", "99")
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, bars(), 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "HOLDING_MA10_TRAIL_NO_LOOSENING")
        self.assertEqual(p["sl"], "99")
        self.assertEqual(b.amendments, [])

    def test_short_must_not_raise_stop(self):
        b = BrokerStub()
        p = pos("SHORT", "100")
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, bars(), 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "HOLDING_MA10_TRAIL_NO_LOOSENING")
        self.assertEqual(p["sl"], "100")
        self.assertEqual(b.amendments, [])

    def test_long_tightens_on_exchange_without_cancelling(self):
        b = BrokerStub()
        p = pos("LONG", "97")
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, bars(), 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "MA10_BROKER_STOP_AMENDED")
        self.assertEqual(p["sl"], "98.90")
        self.assertEqual(p["sl_policy"], "SL_MA10_PCT1_TRAIL_FAVOURABLE_ONLY_10M")
        self.assertEqual(len(b.amendments), 1)
        self.assertEqual(b.amendments[0][1], "/api/v1/trade/amend-tpsl")
        self.assertEqual(b.amendments[0][2]["newSlTriggerPrice"], "98.90")
        self.assertEqual(b.cancels, [])
        self.assertEqual(b.closes, [])

    def test_short_tightens_on_exchange_without_cancelling(self):
        b = BrokerStub()
        p = pos("SHORT", "103")
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, bars(), 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "MA10_BROKER_STOP_AMENDED")
        self.assertEqual(p["sl"], "101.11")
        self.assertEqual(len(b.amendments), 1)
        self.assertEqual(b.amendments[0][2]["newSlTriggerPrice"], "101.11")
        self.assertEqual(b.cancels, [])
        self.assertEqual(b.closes, [])


if __name__ == "__main__":
    unittest.main()
