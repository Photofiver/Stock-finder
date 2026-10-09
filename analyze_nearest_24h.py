"""One-off, read-only historical nearest-signal test for October 8-9, 2026.

Uses actual historical TOP7 diagnostics from GitHub commit snapshots and public
BloFin 15m/1m candles. No trading endpoints or private exchange keys are used.
"""
import base64
import concurrent.futures
import datetime as dt
import json
import os
import time
from pathlib import Path

import requests

REPO = "Photofiver/Stock-finder"
GH = "https://api.github.com/repos/" + REPO
BLOFIN = "https://openapi.blofin.com/api/v1/market/candles"
WINDOW_START = "2026-10-08T15:41:53Z"
WINDOW_END = "2026-10-09T15:41:53Z"
FEE_PCT = 0.12
OUT = Path("blofin_live_scans/nearest_24h_2026-10-09.json")
MS15 = 15 * 60 * 1000


def ms(value):
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def query(url, params=None, headers=None):
    err = None
    for attempt in range(4):
        try:
            result = requests.get(url, params=params, headers=headers, timeout=30)
            result.raise_for_status()
            return result.json()
        except Exception as exc:
            err = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"{url}: {err}")


def github(path, params=None):
    token = os.environ.get("GITHUB_TOKEN", "")
    return query(GH + path, params=params, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
    })


def state_for_commit(commit):
    sha = commit["sha"]
    result = github("/contents/blofin_live_state.json", {"ref": sha})
    raw = base64.b64decode(result["content"])
    data = json.loads(raw)
    return {
        "sha": sha,
        "signal_close_ms": int(data.get("last_scan_close_ms") or 0),
        "instruments": data.get("last_diagnostic", {}).get("instruments") or [],
        "last_result": data.get("last_diagnostic", {}).get("result"),
    }


def all_states():
    commits = github("/commits", {
        "path": "blofin_live_state.json",
        "since": "2026-10-08T15:40:00Z",
        "until": "2026-10-09T15:42:00Z",
        "per_page": 100,
        "page": 1,
    })
    if not isinstance(commits, list):
        raise RuntimeError(f"Unexpected commits API response: {commits}")
    print(f"Collected {len(commits)} commit references", flush=True)
    snapshots = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        futures = {pool.submit(state_for_commit, x): x["sha"] for x in commits}
        for f in concurrent.futures.as_completed(futures):
            try:
                snapshots.append(f.result())
            except Exception as exc:
                errors.append({"sha": futures[f], "error": str(exc)})
    if errors:
        print(f"Snapshot errors: {len(errors)}: {errors[:2]}", flush=True)
    by_ms = {}
    for row in snapshots:
        stamp = row["signal_close_ms"]
        if stamp and len(row["instruments"]) >= 1:
            if stamp not in by_ms:
                by_ms[stamp] = row
            elif len(row["instruments"]) > len(by_ms[stamp]["instruments"]):
                by_ms[stamp] = row
    return [by_ms[k] for k in sorted(by_ms)], len(commits), errors


def market(inst, interval, *, after=None, limit=240):
    params = {"instId": inst, "bar": interval, "limit": str(limit)}
    if after is not None:
        params["after"] = str(after)
    payload = query(BLOFIN, params, headers={
        "Accept": "application/json",
        "User-Agent": "BloFin-selection-24h-audit/1.0",
    })
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"BloFin {inst} {interval}: {payload}")
    rows = {}
    for x in payload.get("data", []):
        if len(x) >= 9 and str(x[8]) == "1":
            rows[int(x[0])] = {
                "open": float(x[1]), "high": float(x[2]),
                "low": float(x[3]), "close": float(x[4]),
            }
    return rows


def select(row, method):
    choices = []
    for record in row["instruments"]:
        candle = record.get("candle") or {}
        ref = float(candle.get("close") or 0)
        if not ref:
            continue
        for side in ("LONG", "SHORT"):
            obj = record.get(side.lower()) or {}
            try:
                numerator, denominator = [int(k) for k in obj["score"].split("/")]
                missing = len(obj["missing"])
            except Exception:
                continue
            choices.append({
                "inst": record["inst"], "side": side,
                "score": f"{numerator}/{denominator}",
                "missing": missing,
                "ratio": numerator / denominator,
                "points": numerator,
                "top7_rank": record.get("rank") or 999,
                "signal_close": ref,
                "signal_close_ms": row["signal_close_ms"],
            })
    if method == "minimum_missing":
        choices.sort(key=lambda x: (x["missing"], -x["ratio"],
                                    x["top7_rank"], x["inst"], x["side"]))
    else:
        choices.sort(key=lambda x: (-x["points"], x["top7_rank"],
                                    x["inst"], x["side"]))
    return choices[0] if choices else None


def calc_choice(pick, candles, minute_cache):
    if pick is None:
        return {"status": "NO_CANDIDATE", "net_pct": None}
    stamp = pick["signal_close_ms"]
    ref = pick["signal_close"]
    bars = candles.get(pick["inst"], {})
    # The signal candle closes at stamp; the next bar OPENS at stamp.
    candle = bars.get(stamp)
    if candle is None:
        try:
            candle = market(pick["inst"], "15m", after=stamp + MS15, limit=4).get(stamp)
        except Exception:
            pass
    if candle is None:
        return {"status": "NO_EXCHANGE_CANDLE", "net_pct": None}
    side = pick["side"]
    tp = ref * (1.01 if side == "LONG" else 0.99)
    sl = ref * (0.99 if side == "LONG" else 1.01)
    tp_hit = candle["high"] >= tp if side == "LONG" else candle["low"] <= tp
    sl_hit = candle["low"] <= sl if side == "LONG" else candle["high"] >= sl
    first = None
    minute_count = 0
    if tp_hit and sl_hit:
        key = (pick["inst"], stamp)
        if key not in minute_cache:
            try:
                minute_cache[key] = market(
                    pick["inst"], "1m", after=stamp + MS15, limit=30)
            except Exception:
                minute_cache[key] = {}
        minutes = minute_cache[key]
        period = [minutes[k] for k in sorted(minutes)
                  if stamp <= k < stamp + MS15]
        minute_count = len(period)
        if minute_count == 15:
            for bar in period:
                up = bar["high"] >= tp if side == "LONG" else bar["low"] <= tp
                down = bar["low"] <= sl if side == "LONG" else bar["high"] >= sl
                if up and down:
                    first = "BOTH_IN_SAME_MINUTE"
                    break
                if up:
                    first = "TP"
                    break
                if down:
                    first = "SL"
                    break
        if first is None:
            first = "BOTH_UNRESOLVED"
    else:
        first = "TP" if tp_hit else "SL" if sl_hit else "NEITHER"
    net = (
        1.00 - FEE_PCT if first == "TP" else
        -1.00 - FEE_PCT if first == "SL" else
        (candle["close"] / ref - 1.0) * 100 *
        (1 if side == "LONG" else -1) - FEE_PCT if first == "NEITHER" else None
    )
    return {
        "status": first,
        "net_pct": round(net, 6) if net is not None else None,
        "next_15m_candle": candle,
        "tp_reached": tp_hit,
        "sl_reached": sl_hit,
        "minute_bars_checked": minute_count,
    }


def summarise(rows):
    resolved = [r for r in rows if r.get("net_pct") is not None]
    unresolved = [r for r in rows if r.get("net_pct") is None]
    net = sum(r["net_pct"] for r in resolved)
    compounded = 100 * (
        __import__("math").prod(1 + r["net_pct"] / 100 for r in resolved) - 1
    )
    return {
        "choices": len(rows),
        "resolved": len(resolved),
        "unresolved": len(unresolved),
        "status_counts": {
            status: sum(r["status"] == status for r in rows)
            for status in sorted(set(r["status"] for r in rows))
        },
        "net_pct_points_known_only": round(net, 5),
        "compounded_pct_known_only": round(compounded, 5),
        "minimum_if_unresolved_all_SL": round(net - len(unresolved) * 1.12, 5),
        "maximum_if_unresolved_all_TP": round(net + len(unresolved) * 0.88, 5),
        "winning": sum(r["net_pct"] > 0 for r in resolved),
        "losing": sum(r["net_pct"] < 0 for r in resolved),
    }


def main():
    snaps, commit_count, errors = all_states()
    start, end = ms(WINDOW_START), ms(WINDOW_END)
    scans = [x for x in snaps if start <= x["signal_close_ms"]
             and x["signal_close_ms"] + MS15 <= end]
    print(f"Analysing {len(scans)} completed 15m signals", flush=True)
    methods = ("minimum_missing", "highest_raw_score")
    picks = {m: [select(row, m) for row in scans] for m in methods}
    instruments = sorted({
        p["inst"] for group in picks.values() for p in group if p
    })
    candle_cache = {}
    candle_errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        future = {pool.submit(market, inst, "15m"): inst for inst in instruments}
        for f in concurrent.futures.as_completed(future):
            inst = future[f]
            try:
                candle_cache[inst] = f.result()
            except Exception as exc:
                candle_errors.append({"inst": inst, "error": str(exc)})
                candle_cache[inst] = {}
    print(f"Fetched 15m candles for {len(instruments)} instruments; "
          f"errors {len(candle_errors)}", flush=True)
    minute_cache = {}
    outcomes = {}
    for method in methods:
        group = []
        for pick in picks[method]:
            result = calc_choice(pick, candle_cache, minute_cache)
            if pick:
                row = {
                    **pick,
                    "signal_time_utc": dt.datetime.fromtimestamp(
                        pick["signal_close_ms"] / 1000, dt.timezone.utc
                    ).isoformat(),
                    **result,
                }
            else:
                row = result
            group.append(row)
        outcomes[method] = {"summary": summarise(group), "decisions": group}

    current = json.loads(Path("blofin_live_state.json").read_text(encoding="utf-8"))
    actual = [
        t for t in current.get("trade_history", [])
        if start <= int(t.get("opened_ms") or 0) < end
        and t.get("result") in ("WIN", "LOSS", "FLAT")
    ]
    actual_summary = {
        "trades": len(actual),
        "wins": sum(float(t.get("net_pnl_usdt") or 0) > 0 for t in actual),
        "losses": sum(float(t.get("net_pnl_usdt") or 0) < 0 for t in actual),
        "net_usdt_after_fees": round(
            sum(float(t.get("net_pnl_usdt") or 0) for t in actual), 8
        ),
    }
    zk = next((
        t for t in actual if t.get("inst") == "ZK-USDT"
        and t.get("side") == "LONG"
        and int(t.get("signal_close_ms") or 0) == 1791556200000
    ), None)
    actual_zk_pct = (
        float(zk["net_pnl_usdt"]) / float(zk["notional_usdt"]) * 100
        if zk is not None and float(zk.get("notional_usdt") or 0) > 0 else None
    )
    baseline_zk = next((
        row for row in outcomes["minimum_missing"]["decisions"]
        if row.get("inst") == "ZK-USDT"
        and int(row.get("signal_close_ms") or 0) == 1791556200000
    ), None)
    adjusted = None
    if baseline_zk and baseline_zk.get("net_pct") is not None and actual_zk_pct is not None:
        adjusted = round(
            outcomes["minimum_missing"]["summary"]["net_pct_points_known_only"]
            - baseline_zk["net_pct"] + actual_zk_pct, 6
        )
    report = {
        "analysis_version": 1,
        "start_utc": WINDOW_START,
        "end_utc": WINDOW_END,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_source": (
            "Actual TOP7 15m diagnostics from historical GitHub commits; "
            "future public BloFin confirmed 15m/1m OHLC."
        ),
        "assumptions": (
            "One position each 15m, price at signal candle close, "
            "TP +1%, SL -1%, close after next 15m if neither; "
            "0.12% combined fees, zero execution slippage; "
            "does not model 60m LIVE exits, spread, capital constraint, "
            "live risk guards, or actual entry latency."
        ),
        "commits_seen": commit_count,
        "snapshot_count": len(snaps),
        "snapshot_errors": errors,
        "market_errors": candle_errors,
        "distinct_instruments": len(instruments),
        "historical_actual_live_trades": actual_summary,
        "real_zk_net_pct": actual_zk_pct,
        "nearest_result_substituting_real_zk_pnl_pct_points": adjusted,
        "strategies": outcomes,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "scans": len(scans),
        "methods": {k: v["summary"] for k, v in outcomes.items()},
        "actual": actual_summary,
        "with_real_zk": adjusted,
        "market_errors": len(candle_errors),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
