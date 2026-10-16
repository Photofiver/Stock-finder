"""Tests for 0.25% entry cap, one-way SMA10 broker stops and SHORT MA10-touch exits."""
import unittest
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import blofin_ma10_ma20_live as ma


class BrokerStub:
    ROUND_CEILING = ROUND_CEILING
    ROUND_FLOOR = ROUND_FLOOR
    MAX_ENTRY_QUOTE_AGE_MS = 30000
    SIGNAL_MINUTES = 10
    SIGNAL_LABEL = "10m"

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
        "reference_entry": "100",
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
        p = pos("LONG", "99.80")
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, bars(), 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "HOLDING_MA10_TRAIL_NO_LOOSENING")
        self.assertEqual(p["sl"], "99.80")
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
        self.assertEqual(p["sl"], "99.75")
        self.assertEqual(p["sl_policy"], "SL_MA10_PCT1_TRAIL_ENTRY_PCT025_CAP_10M")
        self.assertEqual(len(b.amendments), 1)
        self.assertEqual(b.amendments[0][1], "/api/v1/trade/amend-tpsl")
        self.assertEqual(b.amendments[0][2]["newSlTriggerPrice"], "99.75")
        self.assertEqual(b.cancels, [])
        self.assertEqual(b.closes, [])

    def test_five_minute_stop_policy_and_intrabroker_amendment(self):
        b = BrokerStub()
        b.SIGNAL_MINUTES = 5
        b.SIGNAL_LABEL = "5m"
        p = pos("LONG", "97")
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, bars(), 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "MA10_BROKER_STOP_AMENDED")
        self.assertEqual(p["sl_policy"], "SL_MA10_PCT1_TRAIL_ENTRY_PCT025_CAP_5M")
        self.assertEqual(len(b.amendments), 1)

    def test_short_tightens_to_live_ma10_touch_without_cancelling(self):
        b = BrokerStub(last="100")
        p = pos("SHORT", "103")
        short_bars = bars(100.1)
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, short_bars, 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "MA10_BROKER_STOP_AMENDED")
        self.assertEqual(p["sl"], "100.10")
        self.assertEqual(p["sl_policy"], "SL_MA10_TOUCH_OR_CURRENT_SHORT_10M")
        self.assertEqual(len(b.amendments), 1)
        self.assertEqual(b.amendments[0][2]["newSlTriggerPrice"], "100.10")
        self.assertEqual(b.cancels, [])
        self.assertEqual(b.closes, [])

    def test_long_stop_capped_quarter_percent_below_actual_fill(self):
        b = BrokerStub()
        target = ma.bounded_ma10_stop_price(
            b, "LONG", Decimal("900"), Decimal("100"), Decimal("0.01")
        )
        self.assertEqual(target, Decimal("99.75"))

    def test_short_stop_capped_quarter_percent_above_actual_fill(self):
        b = BrokerStub()
        target = ma.bounded_ma10_stop_price(
            b, "SHORT", Decimal("900"), Decimal("100"), Decimal("0.01")
        )
        self.assertEqual(target, Decimal("100.25"))

    def test_long_ma10_stop_remains_tighter_than_entry_cap(self):
        b = BrokerStub()
        target = ma.bounded_ma10_stop_price(
            b, "LONG", Decimal("900"), Decimal("99"), Decimal("0.01")
        )
        self.assertEqual(target, Decimal("98.90"))

    def test_short_ma10_stop_remains_tighter_than_entry_cap(self):
        b = BrokerStub()
        target = ma.bounded_ma10_stop_price(
            b, "SHORT", Decimal("900"), Decimal("101"), Decimal("0.01")
        )
        self.assertEqual(target, Decimal("101.11"))

    def test_short_active_stop_uses_live_ma10_touch_when_tighter(self):
        b = BrokerStub()
        target = ma.active_dynamic_stop_price(
            b, "SHORT", Decimal("900.9"), Decimal("100"), Decimal("0.01")
        )
        self.assertEqual(target, Decimal("100.25"))

    def test_short_active_stop_keeps_existing_stop_when_it_triggers_first(self):
        b = BrokerStub()
        target = ma.active_dynamic_stop_price(
            b, "SHORT", Decimal("927"), Decimal("100"), Decimal("0.01")
        )
        self.assertEqual(target, Decimal("100.25"))

    def test_long_active_stop_is_unchanged(self):
        b = BrokerStub()
        target = ma.active_dynamic_stop_price(
            b, "LONG", Decimal("900"), Decimal("100"), Decimal("0.01")
        )
        self.assertEqual(target, Decimal("99.75"))

    def test_missing_entry_price_fails_safe_instead_of_amending(self):
        b = BrokerStub()
        p = pos("LONG", "97")
        del p["reference_entry"]
        result = ma.refresh_dynamic_ma10_stop(
            b, {}, p, bars(), 12600000, {"A-USDT": {"tickSize": "0.01"}}
        )
        self.assertEqual(result, "MA10_MISSING_ENTRY_PRICE_CLOSE_ATTEMPTED")
        self.assertEqual(len(b.closes), 1)
        self.assertEqual(b.amendments, [])


if __name__ == "__main__":
    unittest.main()
