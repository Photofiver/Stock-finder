"""Offline safety tests for four confirmed 5m tunnel points."""
import unittest
from blofin_pivot_tunnel import FIVE, evaluate_tunnel, pivot_exit_transition

RAW = [
    (11, 9), (12, 9.5), (15, 10), (12, 8), (11, 5),
    (12, 8), (14, 9), (11, 6), (10, 4), (11, 5),
]

def make_rows(reverse=False, last_close=8):
    data = {}
    for i,(high,low) in enumerate(RAW):
        if reverse:
            high,low = 20-low,20-high
        middle=(high+low)/2
        close=last_close if i == 9 else middle
        if reverse and i == 9: close=20-last_close
        data[i*FIVE]=(middle,high,low,close,100+i)
    return data

class TestTunnel(unittest.TestCase):
    def test_short_four_points_entry_on_confirmation_only(self):
        rows=make_rows()
        result=evaluate_tunnel(rows,10*FIVE)
        self.assertTrue(result["entry_allowed"], result)
        self.assertEqual(result["tunnel_direction"],"SHORT")
        self.assertEqual(result["fourth_confirmed_close_ms"],10*FIVE)
        self.assertFalse(evaluate_tunnel(rows,9*FIVE)["entry_allowed"])
        rows[10*FIVE]=(8,9,7,8,130)
        self.assertFalse(evaluate_tunnel(rows,11*FIVE)["entry_allowed"])
    def test_long_four_points(self):
        result=evaluate_tunnel(make_rows(reverse=True),10*FIVE)
        self.assertTrue(result["entry_allowed"],result)
        self.assertEqual(result["tunnel_direction"],"LONG")
    def test_close_outside_blocks(self):
        self.assertFalse(evaluate_tunnel(make_rows(last_close=2),10*FIVE)["entry_allowed"])
    def test_restart_requires_four_new_swings(self):
        self.assertFalse(evaluate_tunnel(make_rows(),10*FIVE,after_ms=3*FIVE)["entry_allowed"])
    def test_wick_does_not_exit_short(self):
        rows=make_rows()
        position=evaluate_tunnel(rows,10*FIVE)
        rows[10*FIVE]=(13,16,12,13,100)
        self.assertFalse(pivot_exit_transition(position,rows,11*FIVE,"SHORT"))
        rows[11*FIVE]=(13,16,12,14.1,100)
        self.assertTrue(pivot_exit_transition(position,rows,12*FIVE,"SHORT"))
    def test_wick_does_not_exit_long(self):
        rows=make_rows(reverse=True)
        position=evaluate_tunnel(rows,10*FIVE)
        rows[10*FIVE]=(8,12,5.0,8,100)
        self.assertFalse(pivot_exit_transition(position,rows,11*FIVE,"LONG"))
        rows[11*FIVE]=(8,12,5.0,5.9,100)
        self.assertTrue(pivot_exit_transition(position,rows,12*FIVE,"LONG"))

if __name__=="__main__":
    unittest.main()
