"""Offline RSI89-only SHORT signal and one-time risk reset tests."""
import unittest
from decimal import Decimal
from types import SimpleNamespace
import blofin_live_hourly as live
import blofin_volume_flip_both_live as strategy


def candle_data(closes):
    result = []
    for i, close in enumerate(closes):
        opening = closes[i - 1] if i else close
        result.append({
            "ts": i * strategy.FIVE,
            "o": opening,
            "h": max(opening, close) + 0.01,
            "l": min(opening, close) - 0.01,
            "c": close,
            "v": 100 + i,
        })
    return result


class TestShortRsi89(unittest.TestCase):
    def setUp(self):
        self.bot = SimpleNamespace(rsi_wilder=live.rsi_wilder)

    def test_upward_crossing_opens_one_short_without_volume_or_tunnel_gate(self):
        closes = [100 + i * 0.05 for i in range(70)] + [103.3, 104.3]
        bars = candle_data(closes)
        close_ms = len(bars) * strategy.FIVE
        sig = strategy.short_rsi89_signal(self.bot, bars, close_ms, 17, "TEST-USDT")
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "SHORT")
        self.assertTrue(sig["rsi89_short_entry"])
        self.assertLess(sig["rsi14_previous"], 89)
        self.assertGreaterEqual(sig["rsi14"], 89)
        # A subsequent candle staying above 89 must not repeat the entry.
        more = candle_data(closes + [104.35])
        self.assertIsNone(strategy.short_rsi89_signal(
            self.bot, more, len(more) * strategy.FIVE, 17, "TEST-USDT"))

    def test_never_use_unclosed_future_candle(self):
        closes = [100 + i * 0.05 for i in range(70)] + [103.3, 104.3]
        bars = candle_data(closes)
        prior_close = (len(bars) - 1) * strategy.FIVE
        self.assertIsNone(strategy.short_rsi89_signal(
            self.bot, bars, prior_close, 1, "TEST-USDT"))

    def test_reset_is_once_and_keeps_trade_history(self):
        saves = []
        bot = SimpleNamespace(
            MAX_NOTIONAL_USDT=Decimal("20"),
            clean_decimal=lambda v: format(v, "f"),
            now_ms=lambda: 123,
            save_state=lambda state: saves.append(dict(state)),
        )
        state = {
            "realized_pnl_usdt": "-3.5",
            "bankroll_pnl_baseline_usdt": "0",
            "risk_stop_triggered": True,
            "trades": {"total": 100},
            "trade_history": [1, 2],
        }
        strategy.reset_rsi89_risk_limit_once(bot, state)
        self.assertFalse(state["risk_stop_triggered"])
        self.assertEqual(state["bankroll_pnl_baseline_usdt"], "-3.5")
        self.assertEqual(state["bankroll_start_usdt"], "20")
        self.assertEqual(state["trade_history"], [1, 2])
        state["realized_pnl_usdt"] = "-4"
        strategy.reset_rsi89_risk_limit_once(bot, state)
        self.assertEqual(len(saves), 1)
        self.assertEqual(state["bankroll_pnl_baseline_usdt"], "-3.5")



class TestRsi89StopLoss8(unittest.TestCase):
    def setUp(self):
        self.detail = {
            "state": "live", "tpslId": "oco-123",
            "tpTriggerPrice": "99", "tpTriggerPriceType": "last",
            "slTriggerPrice": "0", "slTriggerPriceType": "",
        }
        self.posts = []
        self.saves = 0

        def private_request(method, path, params=None, body=None):
            if method == "GET" and path == "/api/v1/trade/order-tpsl-detail":
                return dict(self.detail)
            if method == "POST" and path == "/api/v1/trade/amend-tpsl":
                self.posts.append((path, dict(body)))
                self.detail.update(
                    slTriggerPrice=body["newSlTriggerPrice"],
                    slTriggerPriceType=body["newSlTriggerPriceType"])
                return [{"code": "0", "tpslId": "oco-123"}]
            if method == "POST" and path == "/api/v1/trade/order-tpsl":
                self.posts.append((path, dict(body)))
                self.detail.update(
                    tpTriggerPrice=body["tpTriggerPrice"],
                    tpTriggerPriceType=body["tpTriggerPriceType"],
                    slTriggerPrice=body["slTriggerPrice"],
                    slTriggerPriceType=body["slTriggerPriceType"])
                return [{"code": "0", "tpslId": "oco-123"}]
            raise AssertionError((method, path))

        self.bot = SimpleNamespace(
            d=live.d, clean_decimal=live.clean_decimal, MARGIN_MODE="isolated",
            private_request=private_request, save_state=lambda _: None)
        self.pos = {
            "inst": "TEST-USDT", "side": "SHORT",
            "strategy": strategy.STRATEGY,
            "reference_entry": "100",
            "tp_sl_mode": strategy.RSI89_TP_ONLY_MODE,
            "tpsl_id": "oco-123",
        }
        self.meta = {"tickSize": "0.01"}

    def test_short_stop_loss_is_eight_percent_above_entry(self):
        self.assertEqual(
            strategy._protection_prices(self.bot, self.pos, self.meta),
            (Decimal("99"), Decimal("108")))

    def test_existing_tp_only_order_adds_stop_without_cancelling_tp(self):
        self.assertTrue(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)
        path, body = self.posts[0]
        self.assertEqual(path, "/api/v1/trade/amend-tpsl")
        self.assertEqual(body["newSlTriggerPrice"], "108")
        self.assertEqual(body["newSlTriggerPriceType"], "last")
        self.assertEqual(self.detail["tpTriggerPrice"], "99")
        self.assertEqual(self.pos["sl_policy"], "SL8")
        self.assertEqual(self.pos["protection_status"], "RSI89_TP1_SL8_LAST_ACTIVE")
        self.assertTrue(strategy._ensure_protection(self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)

    def test_new_order_places_broker_tp_and_sl_together(self):
        self.pos.pop("tpsl_id")
        self.assertTrue(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)
        path, body = self.posts[0]
        self.assertEqual(path, "/api/v1/trade/order-tpsl")
        self.assertEqual(body["tpTriggerPrice"], "99")
        self.assertEqual(body["slTriggerPrice"], "108")
        self.assertEqual(body["slTriggerPriceType"], "last")
        self.assertEqual(body["slOrderPrice"], "-1")
        self.assertEqual(self.pos["sl_policy"], "SL8")

    def test_unconfirmed_amend_never_claims_stop_loss_active(self):
        original_request = self.bot.private_request

        def rejects(method, path, params=None, body=None):
            if method == "POST" and path == "/api/v1/trade/amend-tpsl":
                return [{"code": "500", "msg": "not accepted"}]
            return original_request(method, path, params=params, body=body)

        self.bot.private_request = rejects
        self.assertFalse(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertNotEqual(self.pos.get("protection_status"),
                            "RSI89_TP1_SL8_LAST_ACTIVE")
        self.assertEqual(self.detail["slTriggerPrice"], "0")


class TestCompoundedBankroll(unittest.TestCase):
    def test_initial_capital_uses_five_per_coin(self):
        self.assertEqual(
            strategy.compounded_position_target("20", "0", "20"),
            Decimal("5"))

    def test_realized_net_profit_is_reinvested(self):
        self.assertEqual(
            strategy.compounded_position_target("20.45", "0", "20.45"),
            Decimal("5.1125"))
        self.assertEqual(
            strategy.compounded_position_target("24", "0", "24"),
            Decimal("6"))

    def test_new_deposit_does_not_raise_budget(self):
        self.assertEqual(
            strategy.compounded_position_target("20", "0", "100"),
            Decimal("5"))

    def test_existing_exposure_and_cash_buffer_are_respected(self):
        self.assertEqual(
            strategy.compounded_position_target("20.45", "5.1125", "15.3375"),
            Decimal("5.1125"))
        self.assertEqual(
            strategy.compounded_position_target("20.45", "15.3375", "5.1125"),
            Decimal("0"))
        self.assertEqual(
            strategy.compounded_position_target("20.45", "0", "5.25"),
            Decimal("0"))

    def test_loss_never_unlocks_extra_leverage_or_capital(self):
        self.assertEqual(
            strategy.compounded_position_target("19.8", "0", "19.8"),
            Decimal("5"))
        self.assertEqual(
            strategy.compounded_position_target("4", "0", "100"),
            Decimal("0"))


if __name__ == "__main__":
    unittest.main()
