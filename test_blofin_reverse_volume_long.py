import unittest
from blofin_reverse_volume_long import STEP, Position, entry_signal, update_exit


class ReverseVolumeTests(unittest.TestCase):
    def setUp(self):
        self.ts = 318*STEP
        self.rows = {i*STEP: (100, 102, 98, 100, 10) for i in range(322)}
        self.btc = dict(self.rows)
        self.rows[self.ts-STEP] = (99, 100, 98, 100, 1)
        self.rows[self.ts] = (101, 102, 99, 100, 3)

    def test_long_on_red_signal(self):
        self.assertTrue(entry_signal(self.rows, self.btc, self.ts, 200))

    def test_rank_and_ratio_boundaries(self):
        self.assertFalse(entry_signal(self.rows, self.btc, self.ts, 201))
        self.rows[self.ts] = (101, 102, 99, 100, 4)
        self.assertFalse(entry_signal(self.rows, self.btc, self.ts, 1))

    def test_missing_btc_fails_closed(self):
        del self.btc[self.ts-3*STEP]
        self.assertFalse(entry_signal(self.rows, self.btc, self.ts, 1))

    def test_large_volume_not_used_for_exit(self):
        pos = Position(self.ts+STEP, 100, 3, self.ts)
        self.rows[self.ts+STEP] = (100, 102, 99, 101, 100)
        self.assertEqual(update_exit(pos, self.rows, self.ts+STEP), 'HOLD')
        self.assertEqual(pos.last_red_volume, 3)

    def test_exit_green_above_last_red_and_idempotence(self):
        pos = Position(self.ts+STEP, 100, 3, self.ts)
        self.rows[self.ts+STEP] = (101, 102, 99, 100, 2)
        self.assertEqual(update_exit(pos, self.rows, self.ts+STEP), 'HOLD')
        self.assertEqual(pos.last_red_volume, 2)
        self.rows[self.ts+2*STEP] = (100, 102, 99, 101, 2.5)
        self.assertEqual(update_exit(pos, self.rows, self.ts+2*STEP), 'EXIT')
        self.assertEqual(update_exit(pos, self.rows, self.ts+2*STEP), 'HOLD')

    def test_gap_does_not_invent_result(self):
        pos = Position(self.ts+STEP, 100, 3, self.ts)
        self.assertEqual(update_exit(pos, self.rows, self.ts+2*STEP), 'GAP')


if __name__ == '__main__':
    unittest.main()
