import json
import os
import statistics
import time
from datetime import datetime, timezone

import requests

BASE = "https://openapi.blofin.com"
STATE_FILE = os.getenv("PAPER_STATE_FILE", "blofin_paper_state.json")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "blofin-nhd0jt7wspfnhtitdlaowk1n").strip()
START_BANKROLL = float(os.getenv("PAPER_START_BANKROLL_USDT", "10"))
TOP_N = 10
TP_PCT = 0.015
SL_PCT = 0.005
HOLD_HOURS = 5
VOL_LOOKBACK = 3
SPIKE_CAP = 2.5
STOCH_K_PERIOD = 14
STOCH_K_SMOOTH = 3
STOCH_D_PERIOD = 3
RSI_PERIOD = 14
D1M = 60_000
D1H = 60 * D1M
HTTP_TIMEOUT = 25
MAX_RETRIES = 5


def now_ms():
    return int(time.time() * 1000)


def api_get(path, params=None):
    last = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(BASE + path, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            payload = r.json()
            if str(payload.get("code")) != "0":
                raise RuntimeError(payload)
            return payload.get("data", [])
        except Exception as exc:
            last = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed {path}: {last}")


def notify(message, title="BloFin PAPER"):
    print(f"{title}: {message}")
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "3"},
            timeout=HTTP_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        print(f"NTFY ERROR: {exc}")


def parse_candles(raw):
    out = []
    for row in raw:
        if len(row) < 9 or str(row[8]) != "1":
            continue
        try:
            out.append({
                "ts": int(row[0]),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            })
        except Exception:
            pass
    return sorted(out, key=lambda x: x["ts"])


def sma(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period
    return out


def stochastic(bars):
    raw_k = [None] * len(bars)
    for i in range(STOCH_K_PERIOD - 1, len(bars)):
        window = bars[i - STOCH_K_PERIOD + 1:i + 1]
        hh = max(b["h"] for b in window)
        ll = min(b["l"] for b in window)
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


def stoch_side(bars, i):
    if i < 1:
        return None
    prev, cur = bars[i - 1], bars[i]
    if None in (prev.get("k"), prev.get("d"), cur.get("k"), cur.get("d")):
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
    if i < 1:
        return None
    prev, cur = bars[i - 1], bars[i]
    rp, rc = prev.get("rsi"), cur.get("rsi")
    if rp is None or rc is None:
        return None
    if rp <= 50 < rc:
        return "LONG"
    if rp >= 50 > rc:
        return "SHORT"
    return None


def get_universe():
    instruments = {}
    for row in api_get("/api/v1/market/instruments"):
        if (
            row.get("state") == "live"
            and row.get("instType") == "SWAP"
            and row.get("contractType") == "linear"
            and row.get("settleCurrency") == "USDT"
        ):
            inst = str(row.get("instId") or "")
            if inst:
                instruments[inst] = row

    tickers = {}
    ranked = []
    for row in api_get("/api/v1/market/tickers"):
        inst = str(row.get("instId") or "")
        if inst not in instruments:
            continue
        try:
            last = float(row.get("last"))
            open_24h = float(row.get("open24h"))
        except Exception:
            continue
        if last <= 0 or open_24h <= 0:
            continue
        change = (last / open_24h - 1.0) * 100.0
        tickers[inst] = {"last": last, "change": change}
        ranked.append((change, inst))
    ranked.sort(reverse=True)
    top = [inst for _, inst in ranked[:TOP_N]]
    return top, tickers


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("bad state")
        return state
    except Exception:
        return {
            "version": 1,
            "started_at_ms": now_ms(),
            "bankroll_usdt": START_BANKROLL,
            "realized_pnl_usdt": 0.0,
            "position": None,
            "arms": {},
            "last_processed_close_ms": {},
            "trades": {"total": 0, "wins": 0, "losses": 0, "time": 0},
        }


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


def init_arm_from_history(inst, bars, latest_index, state):
    arms = state.setdefault("arms", {})
    if inst in arms:
        return
    direction = None
    last_cross_close = None
    for i in range(1, latest_index):
        cross = rsi_cross(bars, i)
        if cross:
            direction = cross
            last_cross_close = bars[i]["ts"] + D1H
    arms[inst] = {
        "direction": direction,
        "used": False,
        "last_cross_close_ms": last_cross_close,
    }


def fetch_1h(inst):
    raw = api_get("/api/v1/market/candles", {"instId": inst, "bar": "1H", "limit": "120"})
    return decorate(parse_candles(raw))


def fetch_1m_until(inst, end_ms):
    raw = api_get(
        "/api/v1/market/candles",
        {"instId": inst, "bar": "1m", "after": str(end_ms), "limit": "360"},
    )
    return parse_candles(raw)


def close_position(state, reason, exit_px, exit_ms):
    pos = state["position"]
    side = pos["side"]
    entry_px = float(pos["entry_px"])
    notional = float(pos["notional_usdt"])
    if reason == "WIN":
        pct = TP_PCT
    elif reason == "LOSS":
        pct = -SL_PCT
    else:
        pct = (exit_px / entry_px - 1.0) if side == "LONG" else (1.0 - exit_px / entry_px)
    pnl = notional * pct
    state["bankroll_usdt"] = max(0.0, float(state["bankroll_usdt"]) + pnl)
    state["realized_pnl_usdt"] = float(state.get("realized_pnl_usdt", 0.0)) + pnl
    trades = state.setdefault("trades", {"total": 0, "wins": 0, "losses": 0, "time": 0})
    trades["total"] += 1
    if reason == "WIN":
        trades["wins"] += 1
    elif reason == "LOSS":
        trades["losses"] += 1
    else:
        trades["time"] += 1

    notify(
        f"{reason} {side} {pos['inst']} | entry {entry_px:.8g} -> exit {exit_px:.8g} | "
        f"PnL {pnl:+.4f} USDT | bankroll {state['bankroll_usdt']:.4f} USDT",
        "BloFin PAPER RESULT",
    )
    state["position"] = None
    return pnl


def resolve_open_position(state, tickers):
    pos = state.get("position")
    if not pos:
        return
    current = now_ms()
    entry_ms = int(pos["entry_ms"])
    hold_end = entry_ms + HOLD_HOURS * D1H
    scan_end = min(current, hold_end) + D1M
    bars = fetch_1m_until(pos["inst"], scan_end)
    bars = [b for b in bars if entry_ms + D1M <= b["ts"] < hold_end]
    entry_px = float(pos["entry_px"])
    side = pos["side"]
    tp = float(pos["tp_px"])
    sl = float(pos["sl_px"])

    for bar in bars:
        if side == "LONG":
            hit_tp = bar["h"] >= tp
            hit_sl = bar["l"] <= sl
        else:
            hit_tp = bar["l"] <= tp
            hit_sl = bar["h"] >= sl
        if hit_tp and hit_sl:
            close_position(state, "LOSS", sl, bar["ts"])
            return
        if hit_tp:
            close_position(state, "WIN", tp, bar["ts"])
            return
        if hit_sl:
            close_position(state, "LOSS", sl, bar["ts"])
            return

    if current >= hold_end:
        exit_px = bars[-1]["c"] if bars else tickers.get(pos["inst"], {}).get("last", entry_px)
        close_position(state, "TIME", float(exit_px), hold_end)


def evaluate_signals(state, top10):
    data = {}
    latest_idx = {}
    ranks = {inst: idx + 1 for idx, inst in enumerate(top10)}

    for inst in top10:
        try:
            bars = fetch_1h(inst)
            if len(bars) < 35:
                continue
            i = len(bars) - 1
            data[inst] = bars
            latest_idx[inst] = i
            init_arm_from_history(inst, bars, i, state)
        except Exception as exc:
            print(f"1H ERROR {inst}: {exc}")

    # Update RSI arms first for every newly closed 1H candle.
    for inst, bars in data.items():
        i = latest_idx[inst]
        close_ms = bars[i]["ts"] + D1H
        last_done = int(state.setdefault("last_processed_close_ms", {}).get(inst, 0) or 0)
        if close_ms <= last_done:
            continue
        cross = rsi_cross(bars, i)
        if cross:
            state["arms"][inst] = {
                "direction": cross,
                "used": False,
                "last_cross_close_ms": close_ms,
            }

    candidates = []
    for inst, bars in data.items():
        i = latest_idx[inst]
        close_ms = bars[i]["ts"] + D1H
        last_done = int(state.setdefault("last_processed_close_ms", {}).get(inst, 0) or 0)
        if close_ms <= last_done:
            continue
        side = stoch_side(bars, i)
        arm = state["arms"].get(inst, {})
        if side and arm.get("direction") == side and not arm.get("used", False):
            candidates.append((ranks[inst], inst, side, bars[i]["c"], close_ms))

    # Mark current candles processed after building the candidate list.
    for inst, bars in data.items():
        i = latest_idx[inst]
        close_ms = bars[i]["ts"] + D1H
        state["last_processed_close_ms"][inst] = max(
            int(state["last_processed_close_ms"].get(inst, 0) or 0), close_ms
        )

    candidates.sort()
    return candidates


def open_paper_position(state, candidate):
    _, inst, side, entry_px, entry_ms = candidate
    bankroll = float(state["bankroll_usdt"])
    if bankroll <= 0:
        return
    if side == "LONG":
        tp = entry_px * (1.0 + TP_PCT)
        sl = entry_px * (1.0 - SL_PCT)
    else:
        tp = entry_px * (1.0 - TP_PCT)
        sl = entry_px * (1.0 + SL_PCT)
    state["position"] = {
        "inst": inst,
        "side": side,
        "entry_px": entry_px,
        "entry_ms": entry_ms,
        "tp_px": tp,
        "sl_px": sl,
        "notional_usdt": bankroll,
    }
    state["arms"][inst]["used"] = True
    notify(
        f"OPEN {side} {inst} | bankroll/notional {bankroll:.4f} USDT | entry {entry_px:.8g} | "
        f"TP {tp:.8g} (+1.5%) | SL {sl:.8g} (-0.5%) | max 5h",
        "BloFin PAPER OPEN",
    )


def hourly_status(state, top10, tickers):
    bankroll = float(state.get("bankroll_usdt", START_BANKROLL))
    pnl = float(state.get("realized_pnl_usdt", 0.0))
    t = state.get("trades", {})
    pos = state.get("position")
    if pos:
        mark = tickers.get(pos["inst"], {}).get("last", float(pos["entry_px"]))
        entry = float(pos["entry_px"])
        if pos["side"] == "LONG":
            upct = (mark / entry - 1.0) * 100.0
        else:
            upct = (1.0 - mark / entry) * 100.0
        held_h = max(0.0, (now_ms() - int(pos["entry_ms"])) / D1H)
        pos_text = f"OPEN {pos['side']} {pos['inst']} | {upct:+.2f}% | {held_h:.1f}h/5h"
    else:
        pos_text = "brak otwartej pozycji"

    top_text = ", ".join(top10[:3]) if top10 else "brak"
    elapsed_h = max(0.0, (now_ms() - int(state.get("started_at_ms", now_ms()))) / D1H)
    notify(
        f"PAPER status | {elapsed_h:.1f}h od startu | bankroll {bankroll:.4f} USDT | "
        f"realized {pnl:+.4f} | trades {t.get('total', 0)} "
        f"(W{t.get('wins', 0)}/L{t.get('losses', 0)}/T{t.get('time', 0)}) | "
        f"{pos_text} | TOP3: {top_text}",
        "BloFin PAPER 1H",
    )


def main():
    state = load_state()
    top10, tickers = get_universe()
    if not top10:
        raise RuntimeError("BloFin TOP10 is empty")

    resolve_open_position(state, tickers)
    candidates = evaluate_signals(state, top10)
    if not state.get("position") and candidates:
        open_paper_position(state, candidates[0])

    hourly_status(state, top10, tickers)
    state["last_run_ms"] = now_ms()
    state["last_top10"] = top10
    save_state(state)

    print(json.dumps({
        "bankroll": state["bankroll_usdt"],
        "pnl": state["realized_pnl_usdt"],
        "position": state.get("position"),
        "trades": state.get("trades"),
        "top10": top10,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
