"""Offline regression tests for legacy sync cancelling RSI89 broker protection."""
import copy
import unittest
from unittest.mock import patch

import blofin_live_hourly as live


class TestRsi89SyncProtection(unittest.TestCase):
    def position(self):
        return {
            "inst": "BEAM-USDT", "side": "SHORT",
            "strategy": "VOLUME_FLIP_BOTH_5M_V1",
            "hold_policy": "VOLUME_FLIP_BOTH_NO_TPSL",
            "rsi89_short_entry": True,
            "tp_sl_mode": "RSI89_SHORT_TP_ONLY",
            "tp_policy": "TP2_5", "sl_policy": "SL3",
            "risk_profile": "1X_RSI89_SHORT_TP2_5_SL3",
            "tp": "0.0035461", "sl": "0.0037463",
            "tpsl_id": "broker-oco", "tpsl_client_order_id": "client-oco",
            "protection_status": "RSI89_TP2_5_SL3_LAST_ACTIVE",
        }

    def test_repeated_multi_position_sync_preserves_rsi_orders(self):
        pos = self.position()
        state = {"positions": {pos["inst"]: pos}, "position": None}
        before = copy.deepcopy(state)
        with patch.object(live, "get_open_positions", return_value=[{"instId": pos["inst"]}]), \
                patch.object(live, "private_request") as request, \
                patch.object(live, "notify") as notify:
            for _ in range(4):
                live.sync_all_tracked_positions(state)
            request.assert_not_called()
            notify.assert_not_called()
        self.assertEqual(state, before)

    def test_either_rsi_marker_preserves_protection_even_without_order_id(self):
        for marker in ("rsi89_short_entry", "tp_sl_mode"):
            for tpsl_id in ("broker-oco", ""):
                with self.subTest(marker=marker, tpsl_id=tpsl_id):
                    pos = self.position()
                    pos.pop(marker)
                    pos["tpsl_id"] = tpsl_id
                    state = {"position": pos}
                    before = copy.deepcopy(state)
                    with patch.object(live, "private_request") as request, \
                            patch.object(live, "notify") as notify:
                        self.assertFalse(live.cancel_tracked_tpsl(state))
                        request.assert_not_called()
                        notify.assert_not_called()
                    self.assertEqual(state, before)

    def test_actual_legacy_volume_position_still_cancels_its_old_oco(self):
        pos = {"inst": "LEGACY-USDT", "side": "SHORT",
               "hold_policy": "VOLUME_FLIP_BOTH_NO_TPSL", "tpsl_id": "old-oco"}
        with patch.object(live, "private_request", return_value=[{"code": "0"}]) as request, \
                patch.object(live, "notify"):
            self.assertTrue(live.cancel_tracked_tpsl({"position": pos}))
            self.assertEqual(request.call_args.args[:2],
                             ("POST", "/api/v1/trade/cancel-tpsl"))
        self.assertEqual(pos["tpsl_id"], "")

    def test_legacy_tp1_migration_preserves_rsi_targets(self):
        pos = self.position()
        pos["reference_entry"] = "0.0036371"
        state = {"positions": {pos["inst"]: pos}, "position": None}
        before = copy.deepcopy(state)
        with patch.object(live, "get_open_positions", return_value=[{"instId": pos["inst"]}]), \
                patch.object(live, "get_universe", return_value=([], {}, {
                    pos["inst"]: {"tickSize": "0.0000001"}})), \
                patch.object(live, "private_request") as request, \
                patch.object(live, "place_tpsl_for_position") as place, \
                patch.object(live, "notify") as notify:
            self.assertEqual(live.ensure_tp1_for_all_tracked_positions(state), [])
            request.assert_not_called()
            place.assert_not_called()
            notify.assert_not_called()
        self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
