import json
import statistics
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
D1H_MS = 60 * 60 * 1000
VOL_LOOKBACK = 3
SPIKE_CAP = 2.5
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
RSI_PERIOD = 14

# Three hourly TOP10 snapshots saved by the LIVE signal watcher.
SNAPSHOTS = [
    {
        "slot_uk": "2026-09-17 19:01",
        "close_ms": 1789668000000,
        "top10": [
            "ONE-USDT", "CASHCAT-USDT", "MARSCOIN-USDT", "KSM-USDT", "UNI-USDT",
            "NEAR-USDT", "GENIUS-USDT", "ZEC-USDT", "ARB-USDT", "DRIFT-USDT",
        ],
    },
    {
        "slot_uk": "2026-09-17 20:01",
        "close_ms": 1789671600000,
        "top10": [
            "ONE-USDT", "GENIUS-USDT", "MARSCOIN-USDT", "COTI-USDT", "KSM-USDT",
            "CASHCAT-USDT", "CNPY-USDT", "UNI-USDT", "NEAR-USDT", "GALA-USDT",
        ],
    },
    {
        "slot_uk": "2026-09-17 21:01",
        "close_ms": 1789675200000,
        "top10": [
            "ONE-USDT", "CNPY-USDT", "COTI-USDT", "MARSCOIN-USDT", "CASHCAT-USDT",
            "GENIUS-USDT", "UNI-USDT", "NEAR-USDT", "GALA-USDT", "KSM-USDT",
        ],
    },
]


def market_get(inst):
    r = requests.get(
        BASE + "/api/v1/market/candles",
        params={"instId": inst, "bar": "1H", "limit": "240"},
        timeout=25,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 BloFinValidator/1.0"},
    )
    r.raise_for_status()
    payload = r.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError(payload)
    return payload.get("data", [])


def parse_candles(raw):
    out = []
    for row in raw:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        out.append({
            "ts": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]),
        })
    return sorted(out, key=lambda x: x["ts"])


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        if any(v is None for v in w):
            continue
        out[i] = sum(w) / period
    return out


def stochastic(bars):
    raw_k = [None] * len(bars)
    for i in range(STOCH_K_PERIOD - 1, len(bars)):
        w = bars[i - STOCH_K_PERIOD + 1:i + 1]
        hh = max(b["h"] for b in w)
        ll = min(b["l"] for b in w)
        raw_k[i] = 50.0 if hh == ll else 100.0 * (bars[i]["c"] - ll) / (hh - ll)
    k = sma(raw_k, STOCH_K_SMOOTH)
    d = sma(k, STOCH_D_PERIOD)
    return k, d


def rsi_wilder(bars, period=14):
    out = [None] * len(bars)
    if len(bars) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        change = bars[i]["c"] - bars[i - 1]["c"]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    out[period] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(period + 1, len(bars)):
        change = bars[i]["c"] - bars[i - 1]["c"]
        gain, loss = max(change, 0.0), max(-change, 0.0)
        avg_g = (avg_g * (period - 1) + gain) / period
        avg_l = (avg_l * (period - 1) + loss) / period
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def decorate(bars):
    k, d = stochastic(bars)
    rsi = rsi_wilder(bars, RSI_PERIOD)
    for i, bar in enumerate(bars):
        bar["k"] = k[i]
        bar["d"] = d[i]
        bar["rsi"] = rsi[i]
    return bars


def volume_ok(bars, i):
    if i < VOL_LOOKBACK or bars[i]["v"] <= bars[i - 1]["v"]:
        return False
    med = statistics.median([b["v"] for b in bars[i - VOL_LOOKBACK:i]])
    return med > 0 and bars[i]["v"] / med <= SPIKE_CAP


def raw_stoch_cross(bars, i):
    if i < 1:
        return None
    prev, cur = bars[i - 1], bars[i]
    if None in (prev.get("k"), prev.get("d"), cur.get("k"), cur.get("d")):
        return None
    if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
        return "LONG"
    if prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
        return "SHORT"
    return None


def candle_ok(cur, side):
    if side == "LONG":
        return cur["c"] > cur["o"]
    if side == "SHORT":
        return cur["c"] < cur["o"]
    return False


def rsi_cross(bars, i):
    if i < 1:
        return None
    rp, rc = bars[i - 1].get("rsi"), bars[i].get("rsi")
    if rp is None or rc is None:
        return None
    if rp <= 50 < rc:
        return "LONG"
    if rp >= 50 > rc:
        return "SHORT"
    return None


def latest_rsi_arm(bars, i):
    direction = None
    last_cross_close_ms = None
    for j in range(1, i + 1):
        cross = rsi_cross(bars, j)
        if cross:
            direction = cross
            last_cross_close_ms = bars[j]["ts"] + D1H_MS
    return direction, last_cross_close_ms


def analyze(inst, close_ms, all_bars):
    target_ts = close_ms - D1H_MS
    prior = [b.copy() for b in all_bars if b["ts"] <= target_ts]
    if not prior or prior[-1]["ts"] != target_ts:
        raise RuntimeError(f"missing target candle {target_ts}")
    # Reproduce the bot's 120-candle calculation window at that historical run.
    bars = decorate(prior[-120:])
    i = len(bars) - 1
    cur = bars[i]
    cross = raw_stoch_cross(bars, i)
    vol = volume_ok(bars, i)
    c_ok = candle_ok(cur, cross) if cross else False
    final_side = cross if cross and c_ok and vol else None
    arm, arm_cross_ms = latest_rsi_arm(bars, i)
    return {
        "inst": inst,
        "close_ms": close_ms,
        "close_utc": datetime.fromtimestamp(close_ms / 1000, timezone.utc).isoformat(),
        "open": cur["o"],
        "close": cur["c"],
        "volume": cur["v"],
        "prev_volume": bars[i - 1]["v"],
        "median_prev3_volume": statistics.median([b["v"] for b in bars[i - 3:i]]),
        "k_prev": bars[i - 1]["k"],
        "d_prev": bars[i - 1]["d"],
        "k": cur["k"],
        "d": cur["d"],
        "raw_stoch_cross": cross,
        "candle_direction_ok": c_ok,
        "volume_ok": vol,
        "stoch_volume_side": final_side,
        "rsi": cur["rsi"],
        "rsi_arm": arm,
        "rsi_arm_cross_close_ms": arm_cross_ms,
        "full_signal": final_side if final_side and arm == final_side else None,
        "reason_no_final": (
            "NO_STOCH_CROSS" if not cross else
            "WRONG_CANDLE_DIRECTION" if not c_ok else
            "VOLUME_FILTER" if not vol else
            "RSI_ARM_MISMATCH" if arm != final_side else
            None
        ),
    }


def main():
    instruments = sorted({inst for snap in SNAPSHOTS for inst in snap["top10"]})
    history = {}
    errors = {}
    for inst in instruments:
        try:
            history[inst] = parse_candles(market_get(inst))
        except Exception as exc:
            errors[inst] = str(exc)

    observations = []
    for snap in SNAPSHOTS:
        for rank, inst in enumerate(snap["top10"], 1):
            if inst in errors:
                observations.append({"slot_uk": snap["slot_uk"], "rank": rank, "inst": inst, "error": errors[inst]})
                continue
            row = analyze(inst, snap["close_ms"], history[inst])
            row["slot_uk"] = snap["slot_uk"]
            row["rank"] = rank
            observations.append(row)

    good = [r for r in observations if "error" not in r]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "Recomputed exact LIVE bot Stochastic(14,3,3), candle direction, volume > previous and <=2.5x median previous 3, plus RSI(14) 50-cross arm on 120 closed 1H candles ending at each historical slot.",
        "observations": len(observations),
        "valid_observations": len(good),
        "errors": errors,
        "summary": {
            "raw_stoch_long": sum(r.get("raw_stoch_cross") == "LONG" for r in good),
            "raw_stoch_short": sum(r.get("raw_stoch_cross") == "SHORT" for r in good),
            "volume_ok": sum(bool(r.get("volume_ok")) for r in good),
            "stoch_volume_long": sum(r.get("stoch_volume_side") == "LONG" for r in good),
            "stoch_volume_short": sum(r.get("stoch_volume_side") == "SHORT" for r in good),
            "rsi_arm_long": sum(r.get("rsi_arm") == "LONG" for r in good),
            "rsi_arm_short": sum(r.get("rsi_arm") == "SHORT" for r in good),
            "full_long_signals": sum(r.get("full_signal") == "LONG" for r in good),
            "full_short_signals": sum(r.get("full_signal") == "SHORT" for r in good),
        },
        "observations_detail": observations,
    }
    with open("blofin_last3_validation.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
