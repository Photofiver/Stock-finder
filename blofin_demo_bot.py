import base64
import hashlib
import hmac
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.parse import urlencode

import requests

BASE = "https://demo-trading-openapi.blofin.com"
API_KEY = os.getenv("BLOFIN_DEMO_API_KEY", "").strip()
SECRET = os.getenv("BLOFIN_DEMO_SECRET", "").strip()
PASSPHRASE = os.getenv("BLOFIN_DEMO_PASSPHRASE", "").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "blofin-nhd0jt7wspfnhtitdlaowk1n").strip()

TIMEFRAMES = ["4H", "1H", "15m", "5m"]
TOP_N = 10
MIN_MATCHES = 3
MAX_NOTIONAL_USDT = Decimal("10")
LEVERAGE = "1"
MARGIN_MODE = "isolated"
TP_PCT = Decimal("0.01")
SL_PCT = Decimal("0.01")
MAX_HOLD_SECONDS = 60 * 60
CHECK_SECONDS = 60
TEST_MINUTES = int(os.getenv("DEMO_TEST_MINUTES", "60"))
HTTP_TIMEOUT = 20


def notify(message, title="BloFin DEMO bot"):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "4"},
            timeout=HTTP_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        print(f"NTFY ERROR: {exc}")


def require_credentials():
    missing = []
    if not API_KEY:
        missing.append("BLOFIN_DEMO_API_KEY")
    if not SECRET:
        missing.append("BLOFIN_DEMO_SECRET")
    if not PASSPHRASE:
        missing.append("BLOFIN_DEMO_PASSPHRASE")
    if missing:
        raise RuntimeError("Brak GitHub Secrets: " + ", ".join(missing))


def sign(method, request_path, body_str=""):
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    prehash = f"{request_path}{method.upper()}{timestamp}{nonce}{body_str}"
    hex_signature = hmac.new(
        SECRET.encode(), prehash.encode(), hashlib.sha256
    ).hexdigest().encode()
    signature = base64.b64encode(hex_signature).decode()
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": signature,
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }


def private_request(method, path, params=None, body=None):
    method = method.upper()
    if method == "GET":
        qs = urlencode(params or {})
        request_path = path + (f"?{qs}" if qs else "")
        headers = sign(method, request_path)
        response = requests.get(BASE + request_path, headers=headers, timeout=HTTP_TIMEOUT)
    else:
        body_str = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
        headers = sign(method, path, body_str)
        response = requests.request(
            method, BASE + path, headers=headers, data=body_str.encode("utf-8"), timeout=HTTP_TIMEOUT
        )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"BloFin API {path}: {payload.get('code')} {payload.get('msg')}")
    return payload.get("data")


def market_get(path, params=None):
    response = requests.get(BASE + path, params=params, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"BloFin market API {path}: {payload}")
    return payload.get("data", [])


def parse_candles(raw):
    rows = []
    for row in raw:
        try:
            confirm = str(row[8]) if len(row) > 8 else "0"
            rows.append({
                "ts": int(row[0]),
                "open": Decimal(str(row[1])),
                "close": Decimal(str(row[4])),
                "volume": Decimal(str(row[5])),
                "confirm": confirm,
            })
        except (IndexError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x["ts"])
    return [row for row in rows if row["confirm"] == "1"]


def get_instruments():
    instruments = {}
    for row in market_get("/api/v1/market/instruments"):
        if (
            row.get("state") == "live"
            and row.get("instType") == "SWAP"
            and row.get("contractType") == "linear"
            and row.get("settleCurrency") == "USDT"
        ):
            inst = row.get("instId")
            if inst:
                instruments[inst] = row
    return instruments


def get_top10(instruments):
    ranked = []
    for row in market_get("/api/v1/market/tickers"):
        inst = str(row.get("instId") or "")
        if inst not in instruments:
            continue
        try:
            last = Decimal(str(row.get("last")))
            open_24h = Decimal(str(row.get("open24h")))
        except Exception:
            continue
        if last <= 0 or open_24h <= 0:
            continue
        change = (last / open_24h - Decimal("1")) * Decimal("100")
        ranked.append((change, inst, last))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [
        {"rank": idx, "change": change, "inst": inst, "last": last}
        for idx, (change, inst, last) in enumerate(ranked[:TOP_N], 1)
    ]


def get_signal(inst):
    statuses = {}
    long_count = 0
    short_count = 0
    latest_5m_ts = 0

    for tf in TIMEFRAMES:
        candles = parse_candles(
            market_get("/api/v1/market/candles", {"instId": inst, "bar": tf, "limit": "10"})
        )
        if len(candles) < 2:
            statuses[tf] = "?"
            continue
        prev = candles[-2]
        cur = candles[-1]
        if tf == "5m":
            latest_5m_ts = cur["ts"]
        volume_up = cur["volume"] > prev["volume"]
        if not volume_up or cur["close"] == cur["open"]:
            statuses[tf] = "-"
        elif cur["close"] > cur["open"]:
            statuses[tf] = "LONG"
            long_count += 1
        else:
            statuses[tf] = "SHORT"
            short_count += 1

    if long_count >= MIN_MATCHES:
        direction, matches = "LONG", long_count
    elif short_count >= MIN_MATCHES:
        direction, matches = "SHORT", short_count
    else:
        direction, matches = None, max(long_count, short_count)

    return {
        "direction": direction,
        "matches": matches,
        "statuses": statuses,
        "signal_key": f"{inst}|{direction}|{latest_5m_ts}" if direction else None,
    }


def ensure_net_mode():
    data = private_request("GET", "/api/v1/account/position-mode") or {}
    if data.get("positionMode") == "net_mode":
        return
    private_request(
        "POST",
        "/api/v1/account/set-position-mode",
        body={"positionMode": "net_mode", "multiPosition": "false"},
    )


def set_one_x(inst):
    private_request(
        "POST",
        "/api/v1/account/set-leverage",
        body={"instId": inst, "leverage": LEVERAGE, "marginMode": MARGIN_MODE},
    )


def d(value):
    return Decimal(str(value))


def floor_step(value, step):
    value, step = d(value), d(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def price_step(value, tick, rounding):
    value, tick = d(value), d(tick)
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=rounding) * tick


def clean_decimal(value):
    text = format(d(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def size_for_10_usdt(last, meta):
    contract_value = d(meta.get("contractValue") or "0")
    min_size = d(meta.get("minSize") or "0")
    lot_size = d(meta.get("lotSize") or min_size or "1")
    if contract_value <= 0 or last <= 0:
        return None
    raw = MAX_NOTIONAL_USDT / (last * contract_value)
    size = floor_step(raw, lot_size)
    if size < min_size or size <= 0:
        return None
    notional = size * contract_value * last
    if notional > MAX_NOTIONAL_USDT:
        return None
    return size


def get_positions(inst=None):
    params = {"instId": inst} if inst else None
    data = private_request("GET", "/api/v1/account/positions", params=params)
    return data if isinstance(data, list) else []


def find_open_position(inst):
    for row in get_positions(inst):
        try:
            if abs(d(row.get("positions") or "0")) > 0:
                return row
        except Exception:
            continue
    return None


def latest_position_history(inst, opened_ms):
    data = private_request(
        "GET", "/api/v1/account/positions-history", params={"instId": inst, "limit": "20"}
    )
    rows = data if isinstance(data, list) else []
    candidates = []
    for row in rows:
        try:
            if int(row.get("createTime") or 0) >= opened_ms - 30000:
                candidates.append(row)
        except Exception:
            pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: int(x.get("updateTime") or 0), reverse=True)
    return candidates[0]


def close_position(inst):
    private_request(
        "POST",
        "/api/v1/trade/close-position",
        body={"instId": inst, "marginMode": MARGIN_MODE, "positionSide": "net"},
    )


def place_trade(candidate, meta):
    inst = candidate["inst"]
    direction = candidate["direction"]
    last = candidate["last"]
    size = size_for_10_usdt(last, meta)
    if size is None:
        raise RuntimeError(f"{inst}: minimalny kontrakt przekracza limit 10 USDT lub brak danych kontraktu")

    set_one_x(inst)
    tick = d(meta.get("tickSize") or "0.00000001")
    if direction == "LONG":
        side = "buy"
        tp = price_step(last * (Decimal("1") + TP_PCT), tick, ROUND_CEILING)
        sl = price_step(last * (Decimal("1") - SL_PCT), tick, ROUND_FLOOR)
    else:
        side = "sell"
        tp = price_step(last * (Decimal("1") - TP_PCT), tick, ROUND_FLOOR)
        sl = price_step(last * (Decimal("1") + SL_PCT), tick, ROUND_CEILING)

    client_id = ("demo" + uuid.uuid4().hex)[:32]
    body = {
        "instId": inst,
        "marginMode": MARGIN_MODE,
        "positionSide": "net",
        "side": side,
        "orderType": "market",
        "size": clean_decimal(size),
        "reduceOnly": "false",
        "clientOrderId": client_id,
        "tpTriggerPrice": clean_decimal(tp),
        "tpOrderPrice": "-1",
        "tpTriggerPriceType": "last",
        "slTriggerPrice": clean_decimal(sl),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "last",
    }
    data = private_request("POST", "/api/v1/trade/order", body=body)
    row = data[0] if isinstance(data, list) and data else (data or {})
    if str(row.get("code", "0")) != "0":
        raise RuntimeError(f"Order rejected: {row}")
    order_id = str(row.get("orderId") or "")
    opened_ms = int(time.time() * 1000)
    notional = size * d(meta.get("contractValue")) * last
    notify(
        f"OPEN {direction} {inst}\n"
        f"sygnał {candidate['matches']}/4 | 4H {candidate['statuses']['4H']} | 1H {candidate['statuses']['1H']} | "
        f"15m {candidate['statuses']['15m']} | 5m {candidate['statuses']['5m']}\n"
        f"1x | notional≈{notional:.2f} USDT | TP {tp} | SL {sl} | max 60 min",
        "BloFin DEMO OPEN",
    )
    return {
        "inst": inst,
        "direction": direction,
        "opened_ms": opened_ms,
        "opened_monotonic": time.monotonic(),
        "order_id": order_id,
        "signal_key": candidate["signal_key"],
        "notional": str(notional),
    }


def report_closed(position, reason_hint=None):
    time.sleep(2)
    hist = latest_position_history(position["inst"], position["opened_ms"])
    if hist:
        pnl = d(hist.get("realizedPnl") or "0")
        fee = d(hist.get("fee") or "0")
        result = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
        reason = reason_hint or result
        notify(
            f"{result} {position['direction']} {position['inst']}\n"
            f"powód: {reason} | PnL {pnl:.4f} USDT | fee {fee:.4f} USDT",
            "BloFin DEMO RESULT",
        )
    else:
        notify(
            f"CLOSED {position['direction']} {position['inst']} | powód: {reason_hint or 'TP/SL'} | brak historii PnL jeszcze w API",
            "BloFin DEMO RESULT",
        )


def choose_candidate(top10, instruments, last_signal_key):
    candidates = []
    for coin in top10:
        try:
            sig = get_signal(coin["inst"])
            if not sig["direction"]:
                continue
            candidate = {**coin, **sig}
            if candidate["signal_key"] == last_signal_key:
                continue
            if size_for_10_usdt(coin["last"], instruments[coin["inst"]]) is None:
                continue
            candidates.append(candidate)
        except Exception as exc:
            print(f"SIGNAL ERROR {coin['inst']}: {exc}")
    candidates.sort(key=lambda x: (-x["matches"], x["rank"]))
    return candidates[0] if candidates else None


def run():
    require_credentials()
    ensure_net_mode()
    instruments = get_instruments()
    position = None
    last_signal_key = None
    started = time.monotonic()
    end_time = started + TEST_MINUTES * 60
    notify(
        f"START test DEMO: {TEST_MINUTES} min, sprawdzanie co 60 s, TOP10, 4H/1H/15m/5m, wejście 3/4 lub 4/4, 1x, max 10 USDT, TP/SL ±1%.",
        "BloFin DEMO START",
    )

    while time.monotonic() < end_time:
        tick_started = time.monotonic()
        try:
            if position:
                open_row = find_open_position(position["inst"])
                if not open_row:
                    report_closed(position)
                    position = None
                elif time.monotonic() - position["opened_monotonic"] >= MAX_HOLD_SECONDS:
                    close_position(position["inst"])
                    report_closed(position, "TIME EXIT 60m")
                    position = None

            if not position:
                top10 = get_top10(instruments)
                candidate = choose_candidate(top10, instruments, last_signal_key)
                if candidate:
                    position = place_trade(candidate, instruments[candidate["inst"]])
                    last_signal_key = candidate["signal_key"]
                else:
                    print(f"{datetime.now(timezone.utc).isoformat()} no 3/4 or 4/4 signal")
        except Exception as exc:
            print(f"TICK ERROR: {type(exc).__name__}: {exc}")
            notify(f"BŁĄD DEMO: {type(exc).__name__}: {exc}", "BloFin DEMO ERROR")

        wait = CHECK_SECONDS - (time.monotonic() - tick_started)
        if wait > 0 and time.monotonic() + wait < end_time:
            time.sleep(wait)

    if position:
        try:
            if find_open_position(position["inst"]):
                close_position(position["inst"])
                report_closed(position, "TEST END")
            else:
                report_closed(position)
        except Exception as exc:
            notify(f"BŁĄD przy zamykaniu końcowym {position['inst']}: {exc}", "BloFin DEMO ERROR")
    notify("KONIEC sesji testowej DEMO.", "BloFin DEMO STOP")


if __name__ == "__main__":
    run()
