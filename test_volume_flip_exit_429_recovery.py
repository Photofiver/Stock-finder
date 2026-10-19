"""NO ORDERS: regression suite for uncertain BloFin 429 volume exits.

Uses mocks only. Never fetches private API, never places real orders.
"""
import unittest
from unittest.mock import Mock, patch
import blofin_volume_flip_both_live as live
from test_volume_flip_both_live import FakeBot, F, T

COIN="VVV-USDT"


def make_pending(side="SHORT",size="3",pending="UNCERTAIN_CLOSE"):
    return {
        "inst":COIN, "side":side, "strategy":live.STRATEGY,
        "hold_policy":live.HOLD_POLICY, "exit_pending":pending,
        "filled_size":size, "opened_ms":T-12*F,
        "signal_close_ms":T-13*F, "signal_snapshot":{"signal_volume":323},
        "notional_usdt":"6.7",
    }


def broker(qty="-3",created=None,margin="isolated",side="net"):
    row={"instId":COIN,"positions":qty,"positionSide":side,"marginMode":margin}
    if created is not None: row["createTime"]=created
    return row


def new_scan():
    return {"exits":[],"rejections":[],"status":"PENDING"}


class Recovery429(unittest.TestCase):
    def setUp(self):
        self.fake=FakeBot()
        self.fake.now=T+F+1000
        self.state={"positions":{COIN:make_pending()}}
        self.scan=new_scan()

    def test_old_stuck_vvv_short_is_retried_and_reconciled(self):
        live._reconcile_pending_volume_closes(self.fake,self.state,[broker()],self.scan)
        self.assertEqual(self.scan["exits"],[COIN])
        self.assertNotIn(COIN,self.state["positions"])
        self.assertEqual(self.fake.closed,[COIN])
        self.assertGreaterEqual(self.fake.saved,1)

    def test_no_duplicate_when_broker_already_flat(self):
        live._reconcile_pending_volume_closes(self.fake,self.state,[],self.scan)
        self.assertEqual(self.fake.closed,[])
        self.assertIn(COIN,self.state["positions"])
        self.assertEqual(self.scan["rejections"][0]["reason"],"PENDING_EXIT_BROKER_ABSENT_OR_AMBIGUOUS")

    def test_reject_opposite_or_hedged_or_cross_broker_position(self):
        for row in (broker("3"),broker("-3",margin="cross"),broker("-3",side="short"),
                    broker("-4"),broker("0"),broker("n/a")):
            with self.subTest(row=row):
                self.scan=new_scan()
                live._reconcile_pending_volume_closes(self.fake,self.state,[row],self.scan)
                self.assertIn(COIN,self.state["positions"])
                self.assertEqual(self.fake.closed,[])
                self.assertEqual(self.scan["rejections"][0]["reason"],"PENDING_EXIT_BROKER_IDENTITY_MISMATCH")

    def test_reject_position_reopened_by_user_after_bot_entry(self):
        pos=self.state["positions"][COIN]
        created=pos["opened_ms"]+180000
        live._reconcile_pending_volume_closes(self.fake,self.state,[broker(created=created)],self.scan)
        self.assertEqual(self.fake.closed,[])
        self.assertIn(COIN,self.state["positions"])

    def test_uncertain_close_on_429_waits_before_retrying_with_fresh_snapshot(self):
        calls=[]
        def fails_then_succeeds(state,inst,func,*args):
            calls.append(inst)
            if len(calls)==1:raise RuntimeError("429 Client Error: Too Many Requests")
            del state["positions"][inst]
        self.fake._run_for_tracked_position=fails_then_succeeds
        live._reconcile_pending_volume_closes(self.fake,self.state,[broker()],self.scan)
        p=self.state["positions"][COIN]
        self.assertEqual(p["exit_pending"],"UNCERTAIN_CLOSE")
        self.assertEqual(p["exit_retry_count"],1)
        self.assertEqual(len(calls),1)
        self.assertEqual(p["exit_retry_next_ms"],self.fake.now+60000)
        live._reconcile_pending_volume_closes(self.fake,self.state,[broker()],new_scan())
        self.assertEqual(len(calls),1)
        self.fake.now+=60001
        scan=new_scan()
        live._reconcile_pending_volume_closes(self.fake,self.state,[broker()],scan)
        self.assertEqual(len(calls),2)
        self.assertEqual(scan["exits"],[COIN])
        self.assertNotIn(COIN,self.state["positions"])

    def test_retry_backoff_exponential_capped(self):
        self.fake._run_for_tracked_position=Mock(side_effect=RuntimeError("429"))
        expected=[60000,120000,240000,480000,600000,600000]
        for i,delta in enumerate(expected,1):
            t=self.fake.now
            live._reconcile_pending_volume_closes(self.fake,self.state,[broker()],new_scan())
            p=self.state["positions"][COIN]
            self.assertEqual(p["exit_retry_count"],i)
            self.assertEqual(p["exit_retry_next_ms"]-t,delta)
            self.fake.now=p["exit_retry_next_ms"]
        self.assertEqual(self.fake._run_for_tracked_position.call_count,6)

    def test_awaiting_confirmation_remains_blocked_if_close_not_reconciled(self):
        # Successful close-position HTTP request may take time to appear at broker.
        # The bot must not mark the order as flat or resume entries prematurely.
        self.fake._run_for_tracked_position=Mock(return_value=True)
        live._reconcile_pending_volume_closes(self.fake,self.state,[broker()],self.scan)
        self.assertEqual(self.state["positions"][COIN]["exit_pending"],"AWAITING_BROKER_FLAT")
        self.assertIn(COIN,self.state["positions"])
        self.assertEqual(self.scan["exits"],[COIN])

    def test_pending_only_scans_tracked_not_200_public_symbols_after_failure(self):
        self.fake._run_for_tracked_position=Mock(side_effect=RuntimeError("HTTP 429"))
        self.fake.account[COIN]=True
        self.fake.get_open_positions=lambda:[broker()]
        self.fake.now=T+F+1000
        expected=T+F
        rank=["A-USDT"]+[f"R{i}-USDT" for i in range(199)]
        received=[]
        def wait(ids,close):
            received.extend(ids)
            return {}
        with patch.dict("os.environ",{"LIVE_VOLUME_FLIP_BOTH_PAUSE_ENTRIES":"false"}):
            live.run(self.fake,self.state,rank,{}, {},expected,wait)
        self.assertEqual(received,[COIN])
        self.assertEqual(self.state["volume_long_last_scan"]["status"],"WAIT_FOR_EXIT_RECONCILIATION")
        self.assertEqual(self.fake._run_for_tracked_position.call_count,1)
        self.assertEqual(self.state["volume_long_last_scan"]["entries"],[])

    def test_backoff_prevents_retry_on_same_scan_restart(self):
        pos=self.state["positions"][COIN]
        pos["exit_retry_next_ms"]=self.fake.now+100000
        live._reconcile_pending_volume_closes(self.fake,self.state,[broker()],self.scan)
        self.assertEqual(self.fake.closed,[])
        self.assertEqual(self.scan["rejections"][0]["reason"],"PENDING_EXIT_RETRY_BACKOFF")

if __name__=="__main__":
    unittest.main()
