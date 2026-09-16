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

TIMEFRAME = "1H"
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


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2 / (period + 1)
    previous = sum(values[:period]) / period
    out[period - 1] = previous
    for i in range(period, len(values)):
        previous = alpha * values[i] + (1 - alpha) * previous
        out[i] = previous
    return out


def macd_histogram(values, fast=12, slow=26, signal=9):
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)

    macd_line = [None] * len(values)
    compact = []
    positions = []
    for i, (fast_value, slow_value) in enumerate(zip(fast_ema, slow_ema)):
        if fast_value is not None and slow_value is not None:
            value = fast_value - slow_value
            macd_line[i] = value
            compact.append(value)
            positions.append(i)

    compact_signal = ema(compact, signal)
    histogram = [None] * len(values)
    for j, i in enumerate(positions):
        if compact_signal[j] is not None:
            histogram[i] = macd_line[i] - compact_signal[j]
    return histogram


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


def get_closed_candles(inst):
    raw = blofin_get(
        "/api/v1/market/candles",
        {"instId": inst, "bar": TIMEFRAME, "limit": "120"},
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
    inst = coin["inst"]
    candles = get_closed_candles(inst)
    if len(candles) < 40:
        raise RuntimeError(f"za mało zamkniętych świec ({len(candles)})")

    closes = [row[4] for row in candles]
    volumes = [row[5] for row in candles]
    hist = macd_histogram(closes)

    i = len(candles) - 1
    p = i - 1
    if hist[p] is None or hist[i] is None:
        raise RuntimeError("brak danych MACD")

    short_flip = hist[p] > 0 and hist[i] < 0
    long_flip = hist[p] < 0 and hist[i] > 0
    volume_up = volumes[i] > volumes[p]

    short_rules = [short_flip, volume_up]
    long_rules = [long_flip, volume_up]

    short_score = sum(short_rules)
    long_score = sum(long_rules)

    if short_score >= long_score:
        side = "SHORT"
        score = short_score
        selected_flip = short_flip
    else:
        side = "LONG"
        score = long_score
        selected_flip = long_flip

    if short_flip:
        flip_label = "MACD ZIELONY→CZERWONY"
    elif long_flip:
        flip_label = "MACD CZERWONY→ZIELONY"
    else:
        flip_label = "MACD bez świeżej zmiany"

    result = dict(coin)
    result.update(
        {
            "side": side,
            "score": score,
            "exact": score == 2,
            "macd_flip": selected_flip,
            "flip_label": flip_label,
            "volume_up": volume_up,
        }
    )
    return result


def send_ntfy(message):
    if not NTFY_TOPIC:
        raise RuntimeError("NTFY_TOPIC is empty")

    last_error = None
    for attempt in range(5):
        try:
            response = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": "BloFin 1H Scanner", "Priority": "4"},
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


def build_message(coins, results, errors, ranking_errors):
    results.sort(
        key=lambda row: (row["score"], row["macd_flip"], -row["blofin_rank"]),
        reverse=True,
    )

    qualified = [row for row in results if row["score"] == 2]
    header = (
        f"TOP {len(coins)} BloFin 24h | analiza 1H | "
        f"SETUP MACD+VOL 2/2: {len(qualified)}"
    )

    lines = []
    if qualified:
        for n, row in enumerate(qualified, start=1):
            lines.append(
                f"{n}. {row['inst']} {row['side']} — 2/2 | "
                f"{row['flip_label']} | VOL ↑ | BloFin 24h #{row['blofin_rank']} "
                f"{row['change']:+.2f}%"
            )
    else:
        lines.append("Brak setupu MACD+VOL 2/2.")

    if errors:
        lines.append(f"Pominięto {len(errors)} instrumentów z TOP 10 podczas analizy.")
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
    errors = []
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        future_map = {executor.submit(analyze, coin): coin["inst"] for coin in coins}
        for future in as_completed(future_map):
            inst = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(f"{inst}: {type(exc).__name__}: {exc}")

    for error in ranking_errors:
        print(f"RANKING 24H: {error}", file=sys.stderr)
    for error in errors:
        print(error, file=sys.stderr)

    message = build_message(coins, results, errors, ranking_errors)
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
