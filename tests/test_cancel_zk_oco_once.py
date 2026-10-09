"""Offline safety tests for targeted OCO cancellation. Never contacts BloFin."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import cancel_zk_oco_once as target


class CancelOrphanOCOTests(unittest.TestCase):
    ORDER = {
        "instId": "ZK-USDT",
        "tpslId": "10028562149",
        "side": "sell",
        "reduceOnly": "true",
        "tpTriggerPrice": "0.013628000",
        "slTriggerPrice": "0.013358",
        "state": "live",
    }

    def invoke(self, positions=None, pending=None, cancel_result=None):
        output = io.StringIO()
        requests = []

        def exchange(method, path, params=None, body=None):
            requests.append((method, path, params, body))
            if path == "/api/v1/trade/orders-tpsl-pending":
                return list(pending or [])
            if path == "/api/v1/trade/order-tpsl-detail":
                return dict(self.ORDER, state="canceled")
            if path == "/api/v1/trade/cancel-tpsl":
                return cancel_result or [{
                    "tpslId": self.ORDER["tpslId"],
                    "code": "0",
                    "msg": "success",
                }]
            raise AssertionError("Unexpected API endpoint: " + path)

        with patch.object(target.bot, "get_open_positions",
                          return_value=positions or []), \
             patch.object(target.bot, "private_request",
                          side_effect=exchange), \
             redirect_stdout(output):
            target.run()
        return [json.loads(line) for line in output.getvalue().splitlines()], requests

    def test_blocks_cancel_when_zk_position_open(self):
        lines, requests = self.invoke(
            positions=[{"instId": "ZK-USDT", "positions": "100"}],
            pending=[self.ORDER],
        )
        self.assertEqual(lines[-1]["result"], "ABORT_ZK_POSITION_OPEN")
        self.assertEqual(requests, [])

    def test_absent_order_is_not_canceled(self):
        lines, requests = self.invoke()
        self.assertEqual(lines[-1]["result"], "ALREADY_NOT_PENDING")
        self.assertFalse(any(path == "/api/v1/trade/cancel-tpsl"
                             for _, path, _, _ in requests))

    def test_mismatched_tp_sl_prevents_cancellation(self):
        wrong = dict(self.ORDER, tpTriggerPrice="0.030000")
        lines, requests = self.invoke(pending=[wrong])
        self.assertEqual(lines[-1]["result"], "ABORT_ORDER_MISMATCH")
        self.assertFalse(any(path == "/api/v1/trade/cancel-tpsl"
                             for _, path, _, _ in requests))

    def test_only_exact_order_id_is_in_cancel_body(self):
        first = [dict(self.ORDER)]
        requests = []
        calls = 0

        def exchange(method, path, params=None, body=None):
            nonlocal calls
            requests.append((method, path, params, body))
            if path == "/api/v1/trade/orders-tpsl-pending":
                calls += 1
                return first if calls == 1 else []
            if path == "/api/v1/trade/cancel-tpsl":
                return [{"code": "0", "msg": "success"}]
            if path == "/api/v1/trade/order-tpsl-detail":
                return dict(self.ORDER, state="canceled")
            raise AssertionError(path)

        with patch.object(target.bot, "get_open_positions", return_value=[]), \
             patch.object(target.bot, "private_request", side_effect=exchange):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                target.run()
        data = [json.loads(line) for line in buffer.getvalue().splitlines()]
        self.assertEqual(data[-1]["result"], "CANCELED_CONFIRMED")
        posts = [row for row in requests if row[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/api/v1/trade/cancel-tpsl")
        self.assertEqual(posts[0][3], [{
            "instId": "ZK-USDT",
            "tpslId": "10028562149",
            "clientOrderId": "",
        }])

    def test_failed_cancel_is_reported_not_declared_success(self):
        lines, requests = self.invoke(
            pending=[self.ORDER],
            cancel_result=[{"code": "500", "msg": "already triggered"}],
        )
        self.assertEqual(lines[-1]["result"], "CANCEL_FAILED_ORDER_STILL_ACTIVE")


if __name__ == "__main__":
    unittest.main()
