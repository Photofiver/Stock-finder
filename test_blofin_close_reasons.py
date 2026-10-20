import unittest
from blofin_close_reasons_audit import classify, audit


class BrokerCloseAuditTests(unittest.TestCase):
    trade = {"inst": "TEST-USDT", "side": "LONG", "opened_ms": 1000000,
             "closed_ms": 1060000, "reason": "external close",
             "open_price": "2", "close_price": "1.9", "net_pnl_usdt": -0.01}

    def order(self, **values):
        row = {"instId": "TEST-USDT", "side": "sell", "state": "filled",
               "filledSize": "2", "updateTime": "1060000",
               "orderCategory": "sl", "orderId": "456"}
        row.update(values)
        return row

    def test_broker_sl_identified(self):
        result = classify(self.trade, [self.order()])
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["reason"], "SL")

    def test_normal_order_not_assumed_manual(self):
        result = classify(self.trade, [self.order(orderCategory="normal")])
        self.assertEqual(result["source"], "NOT_PROVEN")

    def test_no_false_match_on_entry_or_distant(self):
        result = classify(self.trade, [
            self.order(side="buy"),
            self.order(updateTime="1560000"),
            self.order(state="canceled", filledSize="0"),
        ])
        self.assertEqual(result["status"], "unresolved")

    def test_multiple_close_orders_not_guessed(self):
        result = classify(self.trade, [self.order(orderId="1"),
                                       self.order(orderId="2")])
        self.assertEqual(result["status"], "unresolved")

    def test_backfill_only_external_and_only_get(self):
        trade = dict(self.trade)
        state = {"bankroll_rebased_at_ms": 1, "trade_history": [trade]}
        called = []
        def fetch(method, path, params):
            called.append((method, path))
            return [self.order()]
        result, processed = audit(state, {}, fetch)
        self.assertEqual(processed, 1)
        self.assertEqual(result["1000000:TEST-USDT"]["reason"], "SL")
        self.assertEqual(called, [("GET", "/api/v1/trade/orders-history")])
        updated, again = audit(state, result, fetch)
        self.assertEqual(again, 0)


if __name__ == "__main__":
    unittest.main()
