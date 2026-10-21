import unittest
from blofin_close_bulk_backfill import match_close,fetch_bulk_orders,backfill


def order(**overrides):
    base={"orderId":"500","instId":"ILV-USDT","side":"buy",
          "orderCategory":"sl","state":"filled","filledSize":"3",
          "updateTime":"1060000","averagePrice":"3.397"}
    return {**base,**overrides}


class BulkCloseAudit(unittest.TestCase):
    trade={"inst":"ILV-USDT","side":"SHORT","opened_ms":1000000,
            "closed_ms":1060000,"reason":"external close"}

    def test_exchange_category_proven(self):
        self.assertEqual(match_close(self.trade,[order()])["reason"],"SL")

    def test_no_guess_from_fill_price(self):
        self.assertEqual(match_close(self.trade,[order(side="sell")])["status"],"unresolved")

    def test_duplicate_ambiguous(self):
        self.assertEqual(match_close(self.trade,[order(),order(orderId="501")])["status"],"unresolved")

    def test_page_limit_and_cursor(self):
        first=[order(orderId=str(i)) for i in range(200,100,-1)]
        second=[order(orderId="100")]
        calls=[]
        def f(params):
            calls.append(params.copy())
            return first if len(calls)==1 else second
        rows,pages,complete=fetch_bulk_orders(f,1,1000000)
        self.assertEqual((len(rows),pages,complete),(101,2,True))
        self.assertEqual(calls[1]["after"],"101")
        self.assertEqual(calls[0]["limit"],"100")

    def test_does_not_override_confirmed(self):
        import blofin_close_bulk_backfill as mod
        t={**self.trade,"opened_ms":mod.MIN_START_MS+10000,
           "closed_ms":mod.MIN_START_MS+50000}
        existing={str(t["opened_ms"])+":ILV-USDT":{"status":"verified","reason":"TP"}}
        updated,stats=backfill({"trade_history":[t]},existing,[])
        self.assertEqual(updated,existing)
        self.assertEqual(stats["eligible"],1)


if __name__ == "__main__":
    unittest.main()
