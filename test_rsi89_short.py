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

    def test_rsi_above_89_qualifies_short_without_volume_or_tunnel_gate(self):
        closes = [100 + i * 0.05 for i in range(70)] + [103.3, 104.3]
        bars = candle_data(closes)
        close_ms = len(bars) * strategy.FIVE
        sig = strategy.short_rsi89_signal(self.bot, bars, close_ms, 17, "TEST-USDT")
        self.assertIsNotNone(sig)
        self.assertEqual(sig["side"], "SHORT")
        self.assertTrue(sig["rsi89_short_entry"])
        self.assertLess(sig["rsi14_previous"], 89)
        self.assertGreater(sig["rsi14"], 89)
        # The next fully closed candle may qualify even if RSI remains above 89.
        # A second position in the same coin is prevented by the live account gate.
        more = candle_data(closes + [104.35])
        another = strategy.short_rsi89_signal(
            self.bot, more, len(more) * strategy.FIVE, 17, "TEST-USDT")
        self.assertIsNotNone(another)
        self.assertGreater(another["rsi14"], 89)

    def test_already_above_89_is_eligible_without_upward_cross(self):
        bars = candle_data([100 + i * 0.05 for i in range(25)])
        bot = SimpleNamespace(rsi_wilder=lambda rows, period:
                              [None] * (len(rows)-2) + [90.1, 91.2])
        sig = strategy.short_rsi89_signal(
            bot, bars, len(bars) * strategy.FIVE, 1, "TEST-USDT")
        self.assertIsNotNone(sig)
        self.assertGreater(sig["rsi14_previous"], 89)
        self.assertGreater(sig["rsi14"], 89)

    def test_exactly_89_and_below_do_not_qualify(self):
        bars = candle_data([100 + i * 0.05 for i in range(25)])
        for last_rsi in (89.0, 88.99):
            bot = SimpleNamespace(rsi_wilder=lambda rows, period:
                                  [None] * (len(rows)-2) + [90.1, last_rsi])
            self.assertIsNone(strategy.short_rsi89_signal(
                bot, bars, len(bars) * strategy.FIVE, 1, "TEST-USDT"))

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



class TestRsi89StopLoss3(unittest.TestCase):
    def setUp(self):
        self.detail = {
            "state": "live", "tpslId": "oco-123",
            "tpTriggerPrice": "97.5", "tpTriggerPriceType": "last",
            "slTriggerPrice": "0", "slTriggerPriceType": "",
        }
        self.posts = []
        self.saves = 0

        def private_request(method, path, params=None, body=None):
            if method == "GET" and path == "/api/v1/trade/order-tpsl-detail":
                return dict(self.detail)
            if method == "POST" and path == "/api/v1/trade/amend-tpsl":
                self.posts.append((path, dict(body)))
                if "newTpTriggerPrice" in body:
                    self.detail["tpTriggerPrice"] = body["newTpTriggerPrice"]
                    self.detail["tpTriggerPriceType"] = body["newTpTriggerPriceType"]
                if "newSlTriggerPrice" in body:
                    self.detail["slTriggerPrice"] = body["newSlTriggerPrice"]
                    self.detail["slTriggerPriceType"] = body["newSlTriggerPriceType"]
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

    def test_short_stop_loss_is_three_percent_above_entry(self):
        self.assertEqual(
            strategy._protection_prices(self.bot, self.pos, self.meta),
            (Decimal("97.50"), Decimal("103")))

    def test_existing_tp_only_order_adds_stop_without_cancelling_tp(self):
        self.assertTrue(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)
        path, body = self.posts[0]
        self.assertEqual(path, "/api/v1/trade/amend-tpsl")
        self.assertEqual(body["newSlTriggerPrice"], "103")
        self.assertEqual(body["newSlTriggerPriceType"], "last")
        self.assertEqual(self.detail["tpTriggerPrice"], "97.5")
        self.assertEqual(self.pos["sl_policy"], "SL3")
        self.assertEqual(self.pos["protection_status"], "RSI89_TP2_5_SL3_LAST_ACTIVE")
        self.assertTrue(strategy._ensure_protection(self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)

    def test_new_order_places_broker_tp_and_sl_together(self):
        self.pos.pop("tpsl_id")
        self.assertTrue(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)
        path, body = self.posts[0]
        self.assertEqual(path, "/api/v1/trade/order-tpsl")
        self.assertEqual(body["tpTriggerPrice"], "97.5")
        self.assertEqual(body["slTriggerPrice"], "103")
        self.assertEqual(body["slTriggerPriceType"], "last")
        self.assertEqual(body["slOrderPrice"], "-1")
        self.assertEqual(self.pos["sl_policy"], "SL3")

    def test_existing_tp1_sl3_is_amended_to_tp2_5_on_same_broker_oco(self):
        self.pos.update(
            tp="99", sl="103",
            rsi89_tp_gross_pct="0.01", rsi89_sl_gross_pct="0.03")
        self.detail.update(
            tpTriggerPrice="99", slTriggerPrice="103", slTriggerPriceType="last")
        self.assertTrue(strategy._ensure_protection(self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)
        path, body = self.posts[0]
        self.assertEqual(path, "/api/v1/trade/amend-tpsl")
        self.assertEqual(body["tpslId"], "oco-123")
        self.assertEqual(body["newTpTriggerPrice"], "97.5")
        self.assertNotIn("newSlTriggerPrice", body)
        self.assertEqual(self.detail["tpTriggerPrice"], "97.5")
        self.assertEqual(self.detail["slTriggerPrice"], "103")
        self.assertEqual(self.pos["rsi89_tp_gross_pct"], "0.025")
        self.assertEqual(self.pos["rsi89_sl_gross_pct"], "0.03")
        self.assertEqual(self.pos["protection_status"], "RSI89_TP2_5_SL3_LAST_ACTIVE")
        self.assertTrue(strategy._ensure_protection(self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)

    def test_rejected_tp_2_5_preserves_old_oco_and_defers_entries(self):
        self.pos.update(
            tp="99", sl="103",
            rsi89_tp_gross_pct="0.01", rsi89_sl_gross_pct="0.03")
        self.detail.update(
            tpTriggerPrice="99", slTriggerPrice="103", slTriggerPriceType="last")
        original_request = self.bot.private_request

        def rejects(method, path, params=None, body=None):
            if method == "POST" and path == "/api/v1/trade/amend-tpsl":
                return [{"code": "500", "msg": "not accepted"}]
            return original_request(method, path, params=params, body=body)

        self.bot.private_request = rejects
        self.assertTrue(strategy._ensure_protection(self.bot, {}, self.pos, self.meta))
        self.assertEqual(self.detail["tpTriggerPrice"], "99")
        self.assertEqual(self.detail["slTriggerPrice"], "103")
        self.assertTrue(self.pos["trigger_migration_deferred"])
        self.assertEqual(self.pos["rsi89_tp_gross_pct"], "0.01")
        self.assertNotEqual(self.pos["protection_status"], "RSI89_TP2_5_SL3_LAST_ACTIVE")

    def test_existing_sl8_is_amended_to_sl3_on_same_broker_oco(self):
        self.pos.update(
            sl="108", tp="99",
            rsi89_tp_gross_pct="0.01", rsi89_sl_gross_pct="0.08")
        self.detail.update(
            tpTriggerPrice="99", slTriggerPrice="108", slTriggerPriceType="last")
        self.assertTrue(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][0], "/api/v1/trade/amend-tpsl")
        self.assertEqual(self.posts[0][1]["tpslId"], "oco-123")
        self.assertEqual(self.posts[0][1]["newSlTriggerPrice"], "103")
        self.assertEqual(self.posts[0][1]["newTpTriggerPrice"], "97.5")
        self.assertEqual(self.detail["tpTriggerPrice"], "97.5")
        self.assertEqual(self.pos["rsi89_sl_gross_pct"], "0.03")
        self.assertEqual(self.pos["sl_policy"], "SL3")
        self.assertEqual(self.pos["protection_status"], "RSI89_TP2_5_SL3_LAST_ACTIVE")
        self.assertTrue(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertEqual(len(self.posts), 1)

    def test_broker_rejects_migration_keeps_existing_sl8_and_blocks_new_entries(self):
        self.pos.update(
            sl="108", tp="99",
            rsi89_tp_gross_pct="0.01", rsi89_sl_gross_pct="0.08")
        self.detail.update(
            tpTriggerPrice="99", slTriggerPrice="108", slTriggerPriceType="last")
        original_request = self.bot.private_request

        def rejects(method, path, params=None, body=None):
            if method == "POST" and path == "/api/v1/trade/amend-tpsl":
                return [{"code": "500", "msg": "not accepted"}]
            return original_request(method, path, params=params, body=body)

        self.bot.private_request = rejects
        self.assertTrue(strategy._ensure_protection(
            self.bot, {}, self.pos, self.meta))
        self.assertEqual(self.detail["slTriggerPrice"], "108")
        self.assertTrue(self.pos["trigger_migration_deferred"])
        self.assertEqual(self.pos["rsi89_sl_gross_pct"], "0.08")
        self.assertNotEqual(self.pos["protection_status"],
                            "RSI89_TP2_5_SL3_LAST_ACTIVE")

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
                            "RSI89_TP2_5_SL3_LAST_ACTIVE")
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


class TestRsi89Targets3(unittest.TestCase):
    def setUp(self):
        self.bot = SimpleNamespace(d=live.d)
        self.meta = {"tickSize": "0.01"}

    def test_new_rsi89_short_has_tp2_5_and_sl3(self):
        pos = {"side": "SHORT", "reference_entry": "100",
               "tp_sl_mode": strategy.RSI89_TP_ONLY_MODE}
        self.assertEqual(strategy._protection_prices(self.bot, pos, self.meta),
                         (Decimal("97.50"), Decimal("103")))
        self.assertEqual(strategy._rsi89_policy_labels(self.bot, pos),
                         ("TP2_5", "SL3"))

    def test_legacy_open_rsi89_short_uses_requested_new_stop(self):
        pos = {"side": "SHORT", "reference_entry": "100",
               "tp_sl_mode": strategy.RSI89_TP_ONLY_MODE, "order_id": "old-live"}
        self.assertEqual(strategy._protection_prices(self.bot, pos, self.meta),
                         (Decimal("97.50"), Decimal("103")))
        self.assertEqual(strategy._rsi89_policy_labels(self.bot, pos),
                         ("TP2_5", "SL3"))

    def test_previous_tp143_sl525_position_keeps_broker_targets(self):
        pos = {"side": "SHORT", "reference_entry": "100",
               "tp_sl_mode": strategy.RSI89_TP_ONLY_MODE, "order_id": "new-live",
               "rsi89_tp_gross_pct": "0.0143", "rsi89_sl_gross_pct": "0.0525"}
        self.assertEqual(strategy._protection_prices(self.bot, pos, self.meta),
                         (Decimal("98.57"), Decimal("105.25")))
        self.assertEqual(strategy._rsi89_policy_labels(self.bot, pos),
                         ("TP1_43", "SL5_25"))

    def test_restored_target_uses_original_tick_alignment(self):
        pos = {"side": "SHORT", "reference_entry": "100.01",
               "tp_sl_mode": strategy.RSI89_TP_ONLY_MODE}
        tp, sl = strategy._protection_prices(self.bot, pos, self.meta)
        self.assertEqual((tp, sl), (Decimal("97.50"), Decimal("103.02")))


if __name__ == "__main__":
    unittest.main()
