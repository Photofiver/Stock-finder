import blofin_backtest_v04_spikefilter as bt


class Cap(float):
    def __format__(self, spec):
        if spec == ".0f":
            return f"{float(self):g}"
        return super().__format__(spec)


bt.SPIKE_CAPS = [None, Cap(2.0), Cap(2.25), Cap(2.5), Cap(2.75), Cap(3.0)]

if __name__ == "__main__":
    bt.main()
