import itertools,json,tempfile,unittest
from pathlib import Path
import search

class SearchTests(unittest.TestCase):
    def toy(self):
        rows=[{'features':{'a':v,'b':b},'opened_ms':i*10,'closed_ms':i*10+5,'split':'train','id':str(i),'net_rate':rate,'fee_rate':-.001} for i,(v,b,rate) in enumerate([(0,'L',.02),(1,'S',-.01),(2,'L',.03)])]
        specs=[{'name':'a','kind':'numeric','values':[0,1,2],'radix':30}, {'name':'b','kind':'category','values':['L','S'],'radix':5}]
        return search.Space({'features':specs},rows)
    def test_unrank_bijection_all_depths(self):
        s=self.toy();actual=[tuple(s.unrank(i)) for i in range(s.total)]
        expected=set(itertools.product(range(30),range(5)))-{(0,0)}
        self.assertEqual(set(actual),expected);self.assertEqual(len(actual),len(expected))
        self.assertEqual([sum(x!=0 for x in c) for c in actual],sorted(sum(x!=0 for x in c) for c in actual))
    def test_every_interval_and_boundaries(self):
        s=self.toy();spec=s.specs[0]
        self.assertEqual([s.interval(spec,o) for o in range(2,30)],[(a,b) for a in range(7) for b in range(a,7)])
        for o in range(2,30):
            rule=s.describe([o,0])[0];expected=0
            for j,r in enumerate(s.rows):
                x=r['features']['a'];lo=rule['lower'];hi=rule['upper']
                lower=lo is None or x>lo or (x==lo and rule['lower_inclusive'])
                upper=hi is None or x<hi or (x==hi and rule['upper_inclusive'])
                if lower and upper:expected|=1<<j
            self.assertEqual(s.optionmask(0,o),expected)
    def test_masks_match_bruteforce_intersection(self):
        s=self.toy()
        for i in range(s.total):
            choices=s.unrank(i);self.assertEqual(s.mask(choices),s.optionmask(0,choices[0])&s.optionmask(1,choices[1]))
    def test_common_capital_overlap_and_fees(self):
        s=self.toy();s.rows[1]['opened_ms']=1
        r=search.portfolio(s.rows,7,'train')
        self.assertEqual(r['wins'],2);self.assertEqual(r['losses'],0);self.assertEqual(r['capital_skips'],1)
        self.assertAlmostEqual(r['net_usdt'],1.);self.assertAlmostEqual(r['fees_signed_usdt'],-.04)
    def test_intervals_on_unseen_values_and_missing(self):
        spec={'name':'a','kind':'numeric','values':[0,1,2],'radix':30}
        values=[-1,0,.5,1,1.5,2,3,None]
        sp=search.Space({'features':[spec]},[{'features':{} if v is None else {'a':v}} for v in values])
        self.assertEqual(sp.optionmask(0,1),1<<7)
        for option in range(2,30):
            rule=sp.describe([option])[0];expected=0
            for j,v in enumerate(values):
                if v is None:continue
                lo,hi=rule['lower'],rule['upper']
                if (lo is None or v>lo or (v==lo and rule['lower_inclusive'])) and (hi is None or v<hi or (v==hi and rule['upper_inclusive'])):expected|=1<<j
            self.assertEqual(sp.optionmask(0,option),expected)
    def test_incomplete_worker_batch_cannot_advance(self):
        with tempfile.TemporaryDirectory() as d:
            con=search.db_open(d,{'manifest_sha256':'test'})
            with self.assertRaises(AssertionError):search.commit_batch(con,0,2,[('0',0,0,0,'0','{}')])
            self.assertEqual(con.execute('SELECT count(*) FROM ledger').fetchone()[0],0)
            con.close()
    def test_future_pivot_rejected(self):
        self.assertFalse(search.check_times({'p':[{'confirmed_close_ms':11}]},10))
    def test_after_entry_fields_excluded(self):
        f=search.flatten({'actual_fill_adverse_gap_pct':5,'captured_ms':999,'pre_order_quote':{'quote_age_ms':12},'rsi14':51})
        self.assertEqual(f,{'pre_order_quote.quote_age_ms':12.,'rsi14':51.})
    def test_resume_and_complete_certificate(self):
        s=self.toy()
        with tempfile.TemporaryDirectory() as d:
            m={'features':s.specs,'total_combinations':str(s.total),'rows_sha256':search.digest(s.rows),'scope':'toy','split_counts':{},'limitations':[],'source_commit':'toy'}
            m['manifest_sha256']=search.digest(m)
            Path(d,'manifest.json').write_text(json.dumps(m));Path(d,'rows.json').write_text(json.dumps(s.rows))
            search.run(d,2,0,7)
            self.assertFalse(json.loads(Path(d,'report.json').read_text())['complete'])
            search.run(d,2,10,7);report=json.loads(Path(d,'report.json').read_text())
            self.assertTrue(report['complete']);self.assertEqual(report['actually_evaluated'],149)
            search.run(d,2,10,7)
            self.assertEqual(json.loads(Path(d,'report.json').read_text())['actually_evaluated'],149)
            Path(d,'progress.sqlite').unlink()
            search.run(d,2,0,7)
            self.assertTrue(json.loads(Path(d,'report.json').read_text())['complete'])
            Path(d,'rows.json').write_text('[]')
            with self.assertRaises(ValueError):search.load(d)

if __name__=='__main__':unittest.main()
