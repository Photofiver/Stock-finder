import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

BLOFIN_BASE = "https://openapi.blofin.com"
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "blofin-nhd0jt7wspfnhtitdlaowk1n").strip()

SINGLE_TIMEFRAME = os.getenv("TIMEFRAME", "").strip()
TIMEFRAMES = [SINGLE_TIMEFRAME] if SINGLE_TIMEFRAME else ["4H", "1H", "15m", "5m"]
MIN_MATCHES = 1 if SINGLE_TIMEFRAME else 2
TOP_N = 10
WORKERS = 4
REQUEST_INTERVAL = 0.15
HTTP_RETRIES = 4
STATE_FILE = "scanner_state.json"

_request_lock = threading.Lock()
_last_request_started = 0.0


def wait_for_blofin_slot():
    global _last_request_started
    with _request_lock:
        now = time.monotonic()
        wait = REQUEST_INTERVAL - (now - _last_request_started)
        if wait > 0:
            time.sleep(wait)
        _last_request_started = time.monotonic()


def blofin_get(path, params=None):
    last_error = None
    for attempt in range(HTTP_RETRIES):
        try:
            wait_for_blofin_slot()
            response = requests.get(BLOFIN_BASE + path, params=params, timeout=20)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                pause = 1.0 + attempt
                if retry_after:
                    try:
                        pause = max(float(retry_after), pause)
                    except ValueError:
                        pass
                time.sleep(pause)
                raise requests.HTTPError("HTTP 429", response=response)
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("code")) != "0":
                raise RuntimeError(f"BloFin API error: {payload}")
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise RuntimeError("BloFin returned non-list data")
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < HTTP_RETRIES - 1:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"BloFin request failed after {HTTP_RETRIES} attempts: {last_error}")


def parse_candles(raw):
    candles = []
    for row in raw:
        try:
            confirm = str(row[8]) if len(row) > 8 else "0"
            candles.append(
                (
                    int(row[0]),
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                    confirm,
                )
            )
        except (TypeError, ValueError, IndexError):
            continue
    candles.sort(key=lambda row: row[0])
    return candles


def get_live_instruments():
    live = []
    for row in blofin_get("/api/v1/market/instruments"):
        if (
            row.get("state") == "live"
            and row.get("instType") == "SWAP"
            and row.get("contractType") == "linear"
            and row.get("settleCurrency") == "USDT"
        ):
            inst = str(row.get("instId") or "")
            if inst:
                live.append(inst)
    return sorted(set(live))


def get_universe():
    live = get_live_instruments()
    live_set = set(live)
    ranked = []
    ranking_errors = []

    for ticker in blofin_get("/api/v1/market/tickers"):
        inst = str(ticker.get("instId") or "")
        if inst not in live_set:
            continue
        try:
            last = float(ticker.get("last") or 0)
            open_24h = float(ticker.get("open24h") or 0)
        except (TypeError, ValueError):
            ranking_errors.append(f"{inst}: nieprawidłowe dane 24h")
            continue
        if last <= 0 or open_24h <= 0:
            ranking_errors.append(f"{inst}: brak poprawnej ceny 24h")
            continue
        change_24h = (last / open_24h - 1) * 100
        ranked.append((change_24h, inst))

    ranked.sort(key=lambda item: item[0], reverse=True)
    ranked = ranked[:TOP_N]

    coins = [
        {"inst": inst, "change": change, "blofin_rank": rank}
        for rank, (change, inst) in enumerate(ranked, start=1)
    ]
    return coins, ranking_errors


def get_closed_candles(inst, timeframe):
    raw = blofin_get(
        "/api/v1/market/candles",
        {"instId": inst, "bar": timeframe, "limit": "10"},
    )
    return [row for row in parse_candles(raw) if row[6] == "1"]


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return {"last_slot": None, "captured_at": None}
        return {
            "last_slot": payload.get("last_slot"),
            "captured_at": payload.get("captured_at"),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"last_slot": None, "captured_at": None}


def current_slot():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def save_state(slot):
    payload = {
        "last_slot": slot,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def analyze(coin):
    result = dict(coin)
    result["timeframes"] = {}
    result["errors"] = []

    for timeframe in TIMEFRAMES:
        try:
            candles = get_closed_candles(coin["inst"], timeframe)
            if len(candles) < 2:
                raise RuntimeError(f"za mało zamkniętych świec ({len(candles)})")
            prev_volume = candles[-2][5]
            volume = candles[-1][5]
            volume_up = volume > prev_volume
            result["timeframes"][timeframe] = {
                "volume_up": volume_up,
                "prev_volume": prev_volume,
                "volume": volume,
            }
        except Exception as exc:
            result["timeframes"][timeframe] = None
            result["errors"].append(f"{timeframe}: {type(exc).__name__}: {exc}")

    result["matches"] = sum(
        1
        for timeframe in TIMEFRAMES
        if result["timeframes"].get(timeframe)
        and result["timeframes"][timeframe]["volume_up"]
    )
    return result


def send_ntfy(message):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is empty")

    if SINGLE_TIMEFRAME:
        title = f"BloFin {SINGLE_TIMEFRAME} Scanner"
    else:
        title = "BloFin Multi-TF Volume"

    last_error = None
    for attempt in range(5):
        try:
            response = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": "4"},
                timeout=20,
            )
            response.raise_for_status()
            try:
                message_id = response.json().get("id")
            except (ValueError, AttributeError):
                message_id = None
            suffix = f" id={message_id}" if message_id else ""
            print(f"NTFY OK{suffix}")
            return
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 4:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"ntfy failed after 5 attempts: {last_error}")


def timeframe_status(row, timeframe):
    data = row["timeframes"].get(timeframe)
    if data is None:
        return f"{timeframe} ?"
    return f"{timeframe} {'✓' if data['volume_up'] else '✗'}"


def build_message(coins, results, ranking_errors):
    results.sort(key=lambda row: (row["matches"], -row["blofin_rank"]), reverse=True)
    qualified = [row for row in results if row["matches"] >= MIN_MATCHES]

    if SINGLE_TIMEFRAME:
        header = (
            f"TOP {len(coins)} BloFin 24h | analiza {SINGLE_TIMEFRAME} | "
            f"VOLUME 1/1: {len(qualified)}"
        )
    else:
        header = (
            f"TOP {len(coins)} BloFin 24h | VOLUME multi-TF 4H→1H→15m→5m | "
            f"min 2/4: {len(qualified)}"
        )

    lines = []
    if qualified:
        for n, row in enumerate(qualified, start=1):
            if SINGLE_TIMEFRAME:
                data = row["timeframes"][SINGLE_TIMEFRAME]
                lines.append(
                    f"{n}. {row['inst']} — VOL ↑ {data['prev_volume']:.4f}→{data['volume']:.4f} | "
                    f"BloFin 24h #{row['blofin_rank']} {row['change']:+.2f}%"
                )
            else:
                statuses = " | ".join(timeframe_status(row, tf) for tf in TIMEFRAMES)
                lines.append(
                    f"{n}. {row['inst']} — {row['matches']}/4 | {statuses} | "
                    f"BloFin 24h #{row['blofin_rank']} {row['change']:+.2f}%"
                )
    else:
        if SINGLE_TIMEFRAME:
            lines.append("Brak coinów z wyższym volume niż na poprzedniej zamkniętej świecy.")
        else:
            lines.append("Brak coinów z rosnącym volume na co najmniej 2 z 4 interwałów.")

    analysis_errors = sum(len(row["errors"]) for row in results)
    if analysis_errors:
        lines.append(f"Błędy danych dla {analysis_errors} kombinacji coin/interwał.")
    if ranking_errors:
        lines.append(f"Pominięto {len(ranking_errors)} instrumentów przy rankingu BloFin 24h.")

    return header + "\n" + "\n".join(lines)


def main():
    force_scan = os.getenv("FORCE_SCAN", "0") == "1"
    no_state = os.getenv("NO_STATE", "0") == "1"

    state = load_state()
    slot = current_slot()
    if state.get("last_slot") == slot and not force_scan:
        print(f"SKIP: scan for {slot} already completed")
        return 0

    coins, ranking_errors = get_universe()
    if not coins:
        raise RuntimeError("Nie znaleziono aktywnych kontraktów USDT-M")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {executor.submit(analyze, coin): coin["inst"] for coin in coins}
        for future in as_completed(future_map):
            inst = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                print(f"{inst}: {type(exc).__name__}: {exc}", file=sys.stderr)

    for error in ranking_errors:
        print(f"RANKING 24H: {error}", file=sys.stderr)
    for row in results:
        for error in row["errors"]:
            print(f"{row['inst']} {error}", file=sys.stderr)

    message = build_message(coins, results, ranking_errors)
    send_ntfy(message)
    print(message)

    if not no_state:
        save_state(slot)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        error_message = f"BŁĄD SKANERA: {type(exc).__name__}: {exc}"
        print(error_message, file=sys.stderr)
        try:
            send_ntfy(error_message)
        except Exception as notify_exc:
            print(f"NTFY ERROR: {notify_exc}", file=sys.stderr)
        raise
