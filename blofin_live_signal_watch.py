import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import blofin_live_hourly as bot

SIGNAL_STATE_FILE = os.getenv("SIGNAL_STATE_FILE", "blofin_signal_state.json")
CANDLE_CONFIRM_ATTEMPTS = 30
CANDLE_CONFIRM_DELAY_SEC = 1
BROKER_ID = os.getenv("BLOFIN_BROKER_ID", "dd3511977f23cc87").strip()
UK_TZ = ZoneInfo("Europe/London")

_original_private_request = bot.private_request


def private_request_with_broker(method, path, params=None, body=None):
    if method.upper() != "GET" and path in {
        "/api/v1/trade/order",
        "/api/v1/trade/close-position",
        "/api/v1/trade/order-tpsl",
        "/api/v1/trade/order-algo",
    }:
        body = dict(body or {})
        body.setdefault("brokerId", BROKER_ID)
    return _original_private_request(method, path, params=params, body=body)


bot.private_request = private_request_with_broker


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def default_signal_state():
    return {
        "version": 1,
        "started_at_ms": bot.now_ms(),
        "position": None,
        "arms": {},
        "last_processed_close_ms": {},
        "trades": {"total": 0, "wins": 0, "losses": 0, "flat": 0},
        "realized_pnl_usdt": 0.0,
    }


def send_ntfy(title, message, priority=2, actions=None, click=None):
    headers = {
        "Title": title,
        "Priority": str(priority),
    }
    if actions:
        headers["Actions"] = actions
    if click:
        headers["Click"] = click
    try:
        requests.post(
            f"https://ntfy.sh/{bot.NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=bot.HTTP_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        print(f"NTFY ERROR: {exc}")


def expected_signal_close_ms():
    now = bot.now_ms()
    return (now // bot.SIGNAL_MS) * bot.SIGNAL_MS


def wait_for_confirmed_signal_close(top7, expected_close_ms):
    pending = set(top7)
    for attempt in range(CANDLE_CONFIRM_ATTEMPTS):
        ready = []
        for inst in list(pending):
            try:
                bars = bot.fetch_signal_bars(inst)
                if bars:
                    latest_close_ms = bot.bar_close_ms(bars[-1])
                    if latest_close_ms >= expected_close_ms:
                        ready.append(inst)
            except Exception as exc:
                print(f"CANDLE CONFIRM {inst}: {type(exc).__name__}: {exc}")
        for inst in ready:
            pending.discard(inst)
        if not pending:
            print(
                f"{bot.SIGNAL_LABEL} candle {expected_close_ms} confirmed for all TOP{bot.TOP_N} "
                f"after {attempt + 1} check(s)."
            )
            return
        if attempt < CANDLE_CONFIRM_ATTEMPTS - 1:
            time.sleep(CANDLE_CONFIRM_DELAY_SEC)

    raise RuntimeError(
        f"{bot.SIGNAL_LABEL} candle not confirmed in time for: " + ", ".join(sorted(pending))
    )


def build_live_diagnostic(state, top7):
    def fmt(value, digits=6):
        if value is None:
            return "n/a"
        try:
            return f"{float(value):.{digits}g}"
        except Exception:
            return str(value)

    def pct_vs(current, reference):
        if reference in (None, 0):
            return None
        return ((float(current) / float(reference)) - 1.0) * 100.0

    lines = [
        f"{bot.SIGNAL_LABEL} TOP{bot.TOP_N} — BRAK WEJSCIA",
        "SHORT: MACD cross w dol + RED + RED volume > ostatni GREEN + korpus <=1% ceny close",
        "LONG: MACD cross w gore + GREEN + GREEN volume > ostatni RED + korpus >=60% zakresu + close >=2% vs 10 swiec wczesniej + brak Stoch 14,1,3 cross DOWN w ostatnich 3 swiecach",
        "",
    ]
    records = []

    for rank, inst in enumerate(top7, start=1):
        try:
            bars = bot.fetch_signal_bars(inst)
            if len(bars) < 40:
                lines.append(f"{rank}. {inst} — brak danych")
                records.append({"rank": rank, "inst": inst, "error": "insufficient_data"})
                continue

            i = len(bars) - 1
            prev = bars[i - 1]
            cur = bars[i]
            s = bot.short_entry_metrics(bars, i)
            l = bot.long_entry_metrics(bars, i)
            if not s or not l:
                lines.append(f"{rank}. {inst} — brak danych MACD/Volume")
                records.append({"rank": rank, "inst": inst, "error": "missing_macd_or_volume"})
                continue

            s_checks = [
                ("MACD_DOWN", s["macd_cross_down"]),
                ("RED", s["red_candle"]),
                ("RED_VOL_GT_GREEN", s["volume_higher_than_last_green"]),
                ("BODY_LE_1PCT", s["short_body_max_1pct"]),
            ]
            l_checks = [
                ("MACD_UP", l["macd_cross_up"]),
                ("GREEN", l["green_candle"]),
                ("GREEN_VOL_GT_RED", l["volume_higher_than_last_red"]),
                ("BODY_GE_60PCT", l["green_body_min_60pct"]),
                ("RISE10_GE_2PCT", l["rise_10_bars_min_2pct"]),
                ("STOCH_NO_DOWN_LAST3", l["stoch_long_ok"]),
            ]

            s_ok = sum(1 for _, ok in s_checks if ok)
            l_ok = sum(1 for _, ok in l_checks if ok)
            s_missing = [name for name, ok in s_checks if not ok]
            l_missing = [name for name, ok in l_checks if not ok]

            current_volume = float(cur["v"])
            vol_vs_green_pct = pct_vs(current_volume, s["last_green_volume"])
            vol_vs_red_pct = pct_vs(current_volume, l["last_red_volume"])
            rsi = cur.get("rsi")
            candle = bot.candle_color(cur)
            close_ms = bot.bar_close_ms(cur)

            record = {
                "rank": rank,
                "inst": inst,
                "close_ms": close_ms,
                "candle": {
                    "color": candle,
                    "open": float(cur["o"]),
                    "high": float(cur["h"]),
                    "low": float(cur["l"]),
                    "close": float(cur["c"]),
                    "volume": current_volume,
                },
                "rsi14": None if rsi is None else float(rsi),
                "macd": {
                    "prev_dif": float(prev["macd_dif"]),
                    "prev_dea": float(prev["macd_dea"]),
                    "prev_hist": float(prev["macd_hist"]),
                    "dif": float(cur["macd_dif"]),
                    "dea": float(cur["macd_dea"]),
                    "hist": float(cur["macd_hist"]),
                    "cross_down": bool(s["macd_cross_down"]),
                    "cross_up": bool(l["macd_cross_up"]),
                },
                "short": {
                    "score": f"{s_ok}/4",
                    "missing": s_missing,
                    "red_candle": bool(s["red_candle"]),
                    "body_pct_close": float(s["red_body_pct_close"]),
                    "body_limit_pct": 1.0,
                    "volume": current_volume,
                    "last_green_volume": s["last_green_volume"],
                    "volume_vs_last_green_pct": vol_vs_green_pct,
                },
                "long": {
                    "score": f"{l_ok}/6",
                    "missing": l_missing,
                    "green_candle": bool(l["green_candle"]),
                    "body_pct_range": float(l["green_body_ratio"]) * 100.0,
                    "body_min_pct_range": 60.0,
                    "volume": current_volume,
                    "last_red_volume": l["last_red_volume"],
                    "volume_vs_last_red_pct": vol_vs_red_pct,
                    "rise_10_bars_pct": float(l["rise_10_bars_pct"]),
                    "rise_10_bars_min_pct": 2.0,
                    "stoch_k": l["stoch_k"],
                    "stoch_d": l["stoch_d"],
                    "stoch_cross_down_recent_3": bool(l["stoch_cross_down_recent_3"]),
                    "stoch_long_ok": bool(l["stoch_long_ok"]),
                },
            }
            records.append(record)

            lines.append(
                f"{rank}. {inst} | {candle} | O={fmt(cur['o'])} H={fmt(cur['h'])} "
                f"L={fmt(cur['l'])} C={fmt(cur['c'])} | RSI14={fmt(rsi, 4)}"
            )
            lines.append(
                "   MACD: "
                f"prev DIF={fmt(prev['macd_dif'])} DEA={fmt(prev['macd_dea'])} HIST={fmt(prev['macd_hist'])} -> "
                f"now DIF={fmt(cur['macd_dif'])} DEA={fmt(cur['macd_dea'])} HIST={fmt(cur['macd_hist'])} | "
                f"cross DOWN={'TAK' if s['macd_cross_down'] else 'NIE'}, "
                f"UP={'TAK' if l['macd_cross_up'] else 'NIE'}"
            )
            lines.append(
                f"   SHORT {s_ok}/4: candle RED={'TAK' if s['red_candle'] else 'NIE'} | "
                f"VOL={fmt(current_volume)} vs last GREEN={fmt(s['last_green_volume'])} "
                f"({fmt(vol_vs_green_pct, 4)}%) | body={s['red_body_pct_close']:.3f}% <=1% "
                f"| brak: {', '.join(s_missing) if s_missing else 'NIC'}"
            )
            lines.append(
                f"   LONG  {l_ok}/6: candle GREEN={'TAK' if l['green_candle'] else 'NIE'} | "
                f"VOL={fmt(current_volume)} vs last RED={fmt(l['last_red_volume'])} "
                f"({fmt(vol_vs_red_pct, 4)}%) | body={l['green_body_ratio'] * 100:.1f}% >=60% "
                f"| 10BAR={l['rise_10_bars_pct']:+.3f}% >=2% "
                f"| STOCH K={fmt(l['stoch_k'], 4)} D={fmt(l['stoch_d'], 4)} "
                f"| DOWN last3={'TAK' if l['stoch_cross_down_recent_3'] else 'NIE'} "
                f"| brak: {', '.join(l_missing) if l_missing else 'NIC'}"
            )
        except Exception as exc:
            lines.append(f"{rank}. {inst} — blad danych: {type(exc).__name__}: {exc}")
            records.append(
                {
                    "rank": rank,
                    "inst": inst,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    state["last_diagnostic"] = {
        "generated_at_ms": bot.now_ms(),
        "signal_label": bot.SIGNAL_LABEL,
        "result": "NO_ENTRY",
        "top7": list(top7),
        "instruments": records,
    }
    return "\n".join(lines)


def main():
    bot.require_live_enabled()
    bot.require_account_modes()

    state = bot.load_state()
    top7, tickers, instruments = bot.get_universe()
    if not top7:
        raise RuntimeError("BloFin TOP7 is empty")

    expected_close_ms = expected_signal_close_ms()
    last_scan_close_ms = int(state.get("last_scan_close_ms") or 0)
    if last_scan_close_ms == expected_close_ms:
        print(
            json.dumps(
                {
                    "duplicate_scan_skipped": True,
                    "signal_close_ms": expected_close_ms,
                    "top7": top7,
                },
                ensure_ascii=False,
            )
        )
        return

    # Start just after the signal boundary and confirm the closed signal candle for TOP7
    # plus the tracked instrument, because exits are based on its Volume flip.
    tracked_insts = list(bot.get_tracked_positions(state))
    confirm_insts = list(dict.fromkeys(top7 + tracked_insts))
    wait_for_confirmed_signal_close(confirm_insts, expected_close_ms)

    bot.sync_all_tracked_positions(state)
    bot.ensure_tp1_for_all_tracked_positions(state)
    candidates = bot.evaluate_signals(state, top7)

    for rank, inst, side, signal_close_ms in candidates:
        if int(signal_close_ms) != expected_close_ms:
            raise RuntimeError(
                f"Candidate candle mismatch: got {signal_close_ms}, "
                f"expected {expected_close_ms}"
            )
        print(
            f"DIRECT LIVE candidate {side} {inst} | TOP{rank} | "
            f"signal age {(bot.now_ms() - signal_close_ms) / 1000:.1f}s"
        )

    executed = bot.execute_candidate_batch(state, candidates, tickers, instruments)

    if not candidates:
        diagnostic = build_live_diagnostic(state, top7)
        print(diagnostic)
        send_ntfy("BloFin LIVE check", diagnostic, priority=2)
    else:
        send_ntfy(
            "BloFin LIVE check",
            f"Skan {bot.SIGNAL_LABEL} wykonany. Kandydaci: {len(candidates)}, wykonane: {len(executed)}.",
            priority=2,
        )

    state["last_scan_close_ms"] = expected_close_ms
    state["last_run_ms"] = bot.now_ms()
    state["last_top7"] = top7
    bot.save_state(state)

    print(
        json.dumps(
            {
                "executed": executed,
                "signal_close_ms": expected_close_ms,
                "top7": top7,
                "positions": bot.get_tracked_positions(state),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
