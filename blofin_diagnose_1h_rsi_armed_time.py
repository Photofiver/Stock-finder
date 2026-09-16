import blofin_backtest_rsi_armed_same_data as base
import statistics


def main():
    data = {}
    idx_by_close = {}
    for inst in base.SYMBOLS:
        bars = base.fetch_1h(inst)
        k, d = base.stochastic(bars)
        rsi = base.rsi_wilder(bars, base.RSI_PERIOD)
        for i, b in enumerate(bars):
            b["k"], b["d"], b["rsi"] = k[i], d[i], rsi[i]
        data[inst] = bars
        idx_by_close[inst] = {b["ts"] + base.D1H: i for i, b in enumerate(bars)}

    cache_1m = {}
    def get_minutes(inst, entry_t):
        key = (inst, entry_t)
        if key not in cache_1m:
            end_t = entry_t + base.HOLD_HOURS * base.D1H
            bars = base.fetch_1m(inst, end_t, 180)
            cache_1m[key] = [b for b in bars if entry_t + base.D1M <= b["ts"] < end_t]
        return cache_1m[key]

    def volume_ok(bars, i):
        cur, prev = bars[i], bars[i - 1]
        if cur["v"] <= prev["v"]:
            return False
        med = statistics.median([b["v"] for b in bars[i - base.VOL_LOOKBACK:i]])
        return med > 0 and cur["v"] / med <= base.SPIKE_CAP

    def stoch_side(bars, i):
        prev, cur = bars[i - 1], bars[i]
        if None in (prev["k"], prev["d"], cur["k"], cur["d"]):
            return None
        if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
            side = "LONG"
        elif prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
            side = "SHORT"
        else:
            return None
        if side == "LONG" and cur["c"] <= cur["o"]:
            return None
        if side == "SHORT" and cur["c"] >= cur["o"]:
            return None
        if not volume_ok(bars, i):
            return None
        return side

    def rsi_cross(bars, i):
        prev, cur = bars[i - 1], bars[i]
        rp, rc = prev["rsi"], cur["rsi"]
        if rp is None or rc is None:
            return None
        if rp <= 50 < rc:
            return "LONG"
        if rp >= 50 > rc:
            return "SHORT"
        return None

    def resolve(inst, side, entry_t, entry_px):
        bars = get_minutes(inst, entry_t)
        tp = entry_px * (1 + base.TP_PCT if side == "LONG" else 1 - base.TP_PCT)
        sl = entry_px * (1 - base.SL_PCT if side == "LONG" else 1 + base.SL_PCT)
        for b in bars:
            if side == "LONG":
                ht, hs = b["h"] >= tp, b["l"] <= sl
            else:
                ht, hs = b["l"] <= tp, b["h"] >= sl
            if ht and hs:
                return "LOSS", b["ts"], sl
            if ht:
                return "WIN", b["ts"], tp
            if hs:
                return "LOSS", b["ts"], sl
        exit_t = entry_t + base.HOLD_HOURS * base.D1H
        exit_px = bars[-1]["c"] if bars else entry_px
        return "TIME", exit_t, exit_px

    armed = {inst: None for inst in base.SYMBOLS}
    used = {inst: False for inst in base.SYMBOLS}
    busy_until = -1
    trades = []
    t = base.START_TS
    while t <= base.END_TS:
        for inst in base.SYMBOLS:
            i = idx_by_close[inst].get(t)
            if i is None or i < 30:
                continue
            cross = rsi_cross(data[inst], i)
            if cross:
                armed[inst] = cross
                used[inst] = False

        cs = []
        for inst in base.SYMBOLS:
            i = idx_by_close[inst].get(t)
            if i is None or i < 30:
                continue
            ss = stoch_side(data[inst], i)
            if ss and armed[inst] == ss and not used[inst]:
                cs.append((base.RANK[inst], inst, ss, data[inst][i]["c"]))
        cs.sort()
        if cs and t >= busy_until:
            _, inst, side, entry_px = cs[0]
            reason, exit_t, exit_px = resolve(inst, side, t, entry_px)
            pnl_pct = (exit_px / entry_px - 1.0) if side == "LONG" else (1.0 - exit_px / entry_px)
            trades.append((inst, side, t, reason, pnl_pct, entry_px, exit_px))
            used[inst] = True
            busy_until = exit_t
        t += base.D1H

    times = [x for x in trades if x[3] == "TIME"]
    profit = [x for x in times if x[4] > 0]
    loss = [x for x in times if x[4] < 0]
    flat = [x for x in times if abs(x[4]) < 1e-12]
    total_usdt = sum(base.NOTIONAL * x[4] for x in times)
    print(f"TIME_TOTAL={len(times)} PROFIT={len(profit)} LOSS={len(loss)} FLAT={len(flat)} TIME_PNL={total_usdt:+.4f} USDT")
    for x in times:
        inst, side, t0, _, pnl_pct, entry_px, exit_px = x
        print(f"TIME {base.fmt(t0)} {inst} {side} pnl_pct={pnl_pct*100:+.4f}% pnl_usdt={base.NOTIONAL*pnl_pct:+.4f} entry={entry_px} exit={exit_px}")

if __name__ == "__main__":
    main()
