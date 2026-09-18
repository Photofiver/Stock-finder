import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.parse import urlencode

import requests

BASE = "https://openapi.blofin.com"
API_KEY = os.getenv("BLOFIN_API_KEY", "").strip()
SECRET = os.getenv("BLOFIN_SECRET_KEY", "").strip()
PASSPHRASE = os.getenv("BLOFIN_PASSPHRASE", "").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "blofin-nhd0jt7wspfnhtitdlaowk1n").strip()
STATE_FILE = os.getenv("LIVE_STATE_FILE", "blofin_live_state.json")
LIVE_ENABLED = os.getenv("BLOFIN_LIVE_ENABLED", "").strip().lower() == "true"
MAX_NOTIONAL_USDT = Decimal(os.getenv("LIVE_MAX_BANKROLL_USDT", "10.6136"))
LEVERAGE = "1"
MARGIN_MODE = "isolated"
TOP_N = 10
TP_PCT = Decimal("0.006")
SL_PCT = Decimal("0.005")
HOLD_HOURS = 5
RSI_PERIOD = 14
D1H_MS = 60 * 60 * 1000
HTTP_TIMEOUT = 25
MAX_RETRIES = 4
SIGNAL_MAX_AGE_MS = int(os.getenv("LIVE_SIGNAL_MAX_AGE_MS", "180000"))
ENTRY_FILL_ATTEMPTS = 20
ENTRY_FILL_DELAY_SEC = 0.5
CLOSE_HISTORY_ATTEMPTS = 8
CLOSE_HISTORY_DELAY_SEC = 3
USER_AGENT = "Mozilla/5.0 BloFinStockFinder/1.0"


def now_ms():
    return int(time.time() * 1000)


def d(value):
    return Decimal(str(value))


def clean_decimal(value):
    text = format(d(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


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


def notify(message, title="BloFin LIVE"):
    print(f"{title}: {message}")
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


def require_live_enabled():
    if not LIVE_ENABLED:
        raise RuntimeError("LIVE is locked: BLOFIN_LIVE_ENABLED is not true")
    missing = []
    if not API_KEY:
        missing.append("BLOFIN_API_KEY")
    if not SECRET:
        missing.append("BLOFIN_SECRET_KEY")
    if not PASSPHRASE:
        missing.append("BLOFIN_PASSPHRASE")
    if missing:
        raise RuntimeError("Missing GitHub Secrets: " + ", ".join(missing))
    if MAX_NOTIONAL_USDT <= 0:
        raise RuntimeError("LIVE_MAX_BANKROLL_USDT must be > 0")


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
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
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
            method,
            BASE + path,
            headers=headers,
            data=body_str.encode("utf-8"),
            timeout=HTTP_TIMEOUT,
        )
    response.raise_for_status()
    payload = response.json()
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"BloFin API {path}: {payload.get('code')} {payload.get('msg')}")
    return payload.get("data")


def market_get(path, params=None):
    last_error = None
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(BASE + path, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            if response.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            payload = response.json()
            if str(payload.get("code")) != "0":
                raise RuntimeError(payload)
            return payload.get("data", [])
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Market request failed {path}: {last_error}")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            state.setdefault("trade_history", [])
            state.setdefault("fees_usdt", 0.0)
            state.setdefault("gross_realized_pnl_usdt", 0.0)
            trades = state.setdefault("trades", {"total": 0, "wins": 0, "losses": 0, "flat": 0})
            trades.setdefault("unverified", 0)
            return state
    except Exception:
        pass
    return {
        "version": 1,
        "started_at_ms": now_ms(),
        "position": None,
        "arms": {},
        "last_processed_close_ms": {},
        "trades": {"total": 0, "wins": 0, "losses": 0, "flat": 0, "unverified": 0},
        "trade_history": [],
        "gross_realized_pnl_usdt": 0.0,
        "fees_usdt": 0.0,
        "realized_pnl_usdt": 0.0,
    }


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


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
    rsi = rsi_wilder(bars, RSI_PERIOD)
    for i, bar in enumerate(bars):
        bar["rsi"] = rsi[i]
    return bars


def candle_color(bar):
    if bar["c"] > bar["o"]:
        return "GREEN"
    if bar["c"] < bar["o"]:
        return "RED"
    return "DOJI"


def volume_ok(bars, i, side):
    if i < 1:
        return False

    cur = bars[i]
    cur_color = candle_color(cur)

    if side == "LONG":
        if cur_color != "GREEN":
            return False
        target_color = "RED"
    elif side == "SHORT":
        if cur_color != "RED":
            return False
        target_color = "GREEN"
    else:
        return False

    # Walk backwards until the most recent opposite-color volume bar.
    # Intervening bars of the same color (and DOJI bars) are skipped.
    previous_opposite = None
    for j in range(i - 1, -1, -1):
        if candle_color(bars[j]) == target_color:
            previous_opposite = bars[j]
            break

    if previous_opposite is None:
        return False

    return cur["v"] > previous_opposite["v"]


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


def get_universe():
    instruments = {}
    for row in market_get("/api/v1/market/instruments"):
        if (
            row.get("state") == "live"
            and row.get("instType") == "SWAP"
            and row.get("contractType") == "linear"
            and row.get("settleCurrency") == "USDT"
        ):
            inst = str(row.get("instId") or "")
            if inst:
                instruments[inst] = row
    ranked = []
    tickers = {}
    for row in market_get("/api/v1/market/tickers"):
        inst = str(row.get("instId") or "")
        if inst not in instruments:
            continue
        try:
            last = d(row.get("last"))
            open_24h = d(row.get("open24h"))
        except Exception:
            continue
        if last <= 0 or open_24h <= 0:
            continue
        change = (last / open_24h - Decimal("1")) * Decimal("100")
        ranked.append((change, inst))
        tickers[inst] = {"last": last, "change": change}
    ranked.sort(reverse=True)
    top10 = [inst for _, inst in ranked[:TOP_N]]
    return top10, tickers, instruments


def fetch_1h(inst):
    raw = market_get("/api/v1/market/candles", {"instId": inst, "bar": "1H", "limit": "120"})
    return decorate(parse_candles(raw))


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
            last_cross_close = bars[i]["ts"] + D1H_MS
    arms[inst] = {
        "direction": direction,
        "last_cross_close_ms": last_cross_close,
    }


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

    for inst, bars in data.items():
        i = latest_idx[inst]
        close_ms = bars[i]["ts"] + D1H_MS
        last_done = int(state.setdefault("last_processed_close_ms", {}).get(inst, 0) or 0)
        if close_ms <= last_done:
            continue
        cross = rsi_cross(bars, i)
        if cross:
            state["arms"][inst] = {
                "direction": cross,
                        "last_cross_close_ms": close_ms,
            }

    candidates = []
    scan_now_ms = now_ms()
    for inst, bars in data.items():
        i = latest_idx[inst]
        close_ms = bars[i]["ts"] + D1H_MS
        last_done = int(state.setdefault("last_processed_close_ms", {}).get(inst, 0) or 0)
        if close_ms <= last_done:
            continue
        arm = state["arms"].get(inst, {})
        side = arm.get("direction")
        if (
            side in ("LONG", "SHORT")
            and volume_ok(bars, i, side)
        ):
            signal_age_ms = scan_now_ms - close_ms
            if 0 <= signal_age_ms <= SIGNAL_MAX_AGE_MS:
                candidates.append((ranks[inst], inst, side, close_ms))
            else:
                print(
                    f"STALE SIGNAL {inst} {side}: age={signal_age_ms / 1000:.1f}s "
                    f"(max {SIGNAL_MAX_AGE_MS / 1000:.0f}s)"
                )

    for inst, bars in data.items():
        i = latest_idx[inst]
        close_ms = bars[i]["ts"] + D1H_MS
        state["last_processed_close_ms"][inst] = max(
            int(state["last_processed_close_ms"].get(inst, 0) or 0), close_ms
        )
    candidates.sort()
    return candidates


def require_account_modes():
    pos_mode = private_request("GET", "/api/v1/account/position-mode") or {}
    if pos_mode.get("positionMode") != "net_mode":
        raise RuntimeError("LIVE blocked: BloFin position mode must be net_mode (One-way)")
    margin = private_request("GET", "/api/v1/account/margin-mode") or {}
    if margin.get("marginMode") != MARGIN_MODE:
        raise RuntimeError("LIVE blocked: BloFin margin mode must be isolated")


def get_available_usdt():
    data = private_request("GET", "/api/v1/account/balance") or {}
    for row in data.get("details", []) if isinstance(data, dict) else []:
        if str(row.get("currency")) == "USDT":
            return d(row.get("available") or row.get("availableEquity") or "0")
    return Decimal("0")


def get_open_positions():
    rows = private_request("GET", "/api/v1/account/positions") or []
    out = []
    for row in rows if isinstance(rows, list) else []:
        try:
            if abs(d(row.get("positions") or "0")) > 0:
                out.append(row)
        except Exception:
            pass
    return out


def set_one_x(inst):
    private_request(
        "POST",
        "/api/v1/account/set-leverage",
        body={"instId": inst, "leverage": LEVERAGE, "marginMode": MARGIN_MODE},
    )


def size_for_notional(last, meta, cap):
    contract_value = d(meta.get("contractValue") or "0")
    min_size = d(meta.get("minSize") or "0")
    lot_size = d(meta.get("lotSize") or min_size or "1")
    if contract_value <= 0 or last <= 0 or cap <= 0:
        return None
    raw = cap / (last * contract_value)
    size = floor_step(raw, lot_size)
    if size < min_size or size <= 0:
        return None
    notional = size * contract_value * last
    if notional > cap:
        return None
    return size, notional


def wait_for_order_fill(inst, order_id, client_order_id=""):
    last_row = None
    for attempt in range(ENTRY_FILL_ATTEMPTS):
        try:
            params = {"instId": inst}
            if order_id:
                params["orderId"] = order_id
            else:
                params["clientOrderId"] = client_order_id
            row = private_request("GET", "/api/v1/trade/order-detail", params=params) or {}
            if isinstance(row, list):
                row = row[0] if row else {}
            if isinstance(row, dict):
                last_row = row
                avg = d(row.get("averagePrice") or "0")
                filled = d(row.get("filledSize") or "0")
                state = str(row.get("state") or "").lower()
                if avg > 0 and filled > 0 and state == "filled":
                    return row
        except Exception as exc:
            print(f"ENTRY FILL attempt {attempt + 1}/{ENTRY_FILL_ATTEMPTS}: {exc}")
        if attempt < ENTRY_FILL_ATTEMPTS - 1:
            time.sleep(ENTRY_FILL_DELAY_SEC)

    if isinstance(last_row, dict):
        try:
            if d(last_row.get("averagePrice") or "0") > 0 and d(last_row.get("filledSize") or "0") > 0:
                return last_row
        except Exception:
            pass

    try:
        for row in get_open_positions():
            if row.get("instId") != inst:
                continue
            avg = d(row.get("averagePrice") or "0")
            positions = abs(d(row.get("positions") or "0"))
            if avg > 0 and positions > 0:
                return {
                    "averagePrice": str(avg),
                    "filledSize": str(positions),
                    "fee": "0",
                    "state": "filled",
                    "source": "position",
                }
    except Exception as exc:
        print(f"ENTRY FILL position fallback: {exc}")
    return None


def place_tpsl_for_position(inst, side, tp, sl):
    close_side = "sell" if side == "LONG" else "buy"
    client_id = ("livetpsl" + uuid.uuid4().hex)[:32]
    data = private_request(
        "POST",
        "/api/v1/trade/order-tpsl",
        body={
            "instId": inst,
            "marginMode": MARGIN_MODE,
            "positionSide": "net",
            "side": close_side,
            "tpTriggerPrice": clean_decimal(tp),
            "tpOrderPrice": "-1",
            "tpTriggerPriceType": "last",
            "slTriggerPrice": clean_decimal(sl),
            "slOrderPrice": "-1",
            "slTriggerPriceType": "last",
            "size": "-1",
            "reduceOnly": "true",
            "clientOrderId": client_id,
        },
    )
    row = data[0] if isinstance(data, list) and data else (data or {})
    if str(row.get("code", "0")) != "0":
        raise RuntimeError(f"TP/SL rejected: {row}")
    return str(row.get("tpslId") or ""), client_id


def latest_position_history(inst, opened_ms):
    rows = private_request(
        "GET",
        "/api/v1/account/positions-history",
        params={
            "instId": inst,
            "begin": str(max(0, opened_ms - 120000)),
            "end": str(now_ms() + 60000),
            "limit": "100",
        },
    ) or []
    candidates = []
    for row in rows if isinstance(rows, list) else []:
        try:
            create_ms = int(row.get("createTime") or 0)
            update_ms = int(row.get("updateTime") or 0)
            if create_ms >= opened_ms - 120000 and update_ms >= opened_ms:
                candidates.append(row)
        except Exception:
            pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: int(x.get("updateTime") or 0), reverse=True)
    return candidates[0]


def history_result_ready(hist):
    if not isinstance(hist, dict):
        return False
    required = ("realizedPnl", "fee", "openAveragePrice", "closeAveragePrice", "updateTime")
    return all(str(hist.get(key) if hist.get(key) is not None else "").strip() != "" for key in required)


def record_closed(state, reason_hint=None):
    pos = state.get("position")
    if not pos:
        return False

    reason = reason_hint or pos.get("close_reason") or "TP/SL"
    hist = None
    for attempt in range(CLOSE_HISTORY_ATTEMPTS):
        try:
            candidate = latest_position_history(pos["inst"], int(pos["opened_ms"]))
            if history_result_ready(candidate):
                hist = candidate
                break
        except Exception as exc:
            print(f"CLOSE HISTORY attempt {attempt + 1}/{CLOSE_HISTORY_ATTEMPTS}: {exc}")
        if attempt < CLOSE_HISTORY_ATTEMPTS - 1:
            time.sleep(CLOSE_HISTORY_DELAY_SEC)

    if not hist:
        first_pending = not bool(pos.get("result_pending"))
        pos["result_pending"] = True
        pos["close_reason"] = reason
        pos.setdefault("close_detected_ms", now_ms())
        if first_pending:
            notify(
                f"RESULT PENDING {pos['side']} {pos['inst']} | BloFin close history not ready yet. "
                "The bot will retry and will NOT record a fake 0.0000 PnL.",
                "BloFin LIVE RESULT PENDING",
            )
        return False

    gross_pnl = d(hist.get("realizedPnl") or "0")
    fee = d(hist.get("fee") or "0")
    net_pnl = gross_pnl - fee
    closed_ms = int(hist.get("updateTime") or now_ms())
    opened_ms = int(pos.get("opened_ms") or closed_ms)
    hold_minutes = max(0.0, (closed_ms - opened_ms) / 60000.0)

    trades = state.setdefault(
        "trades",
        {"total": 0, "wins": 0, "losses": 0, "flat": 0, "unverified": 0},
    )
    trades.setdefault("unverified", 0)
    trades["total"] += 1
    if net_pnl > 0:
        result = "WIN"
        trades["wins"] += 1
    elif net_pnl < 0:
        result = "LOSS"
        trades["losses"] += 1
    else:
        result = "FLAT"
        trades["flat"] += 1

    state["gross_realized_pnl_usdt"] = float(
        d(state.get("gross_realized_pnl_usdt", 0)) + gross_pnl
    )
    state["fees_usdt"] = float(d(state.get("fees_usdt", 0)) + fee)
    state["realized_pnl_usdt"] = float(d(state.get("realized_pnl_usdt", 0)) + net_pnl)

    history = state.setdefault("trade_history", [])
    history.append({
        "inst": pos["inst"],
        "side": pos["side"],
        "opened_ms": opened_ms,
        "closed_ms": closed_ms,
        "hold_minutes": round(hold_minutes, 2),
        "open_price": str(hist.get("openAveragePrice") or pos.get("reference_entry") or ""),
        "close_price": str(hist.get("closeAveragePrice") or ""),
        "gross_pnl_usdt": float(gross_pnl),
        "fee_usdt": float(fee),
        "net_pnl_usdt": float(net_pnl),
        "reason": reason,
        "history_id": str(hist.get("historyId") or ""),
    })
    if len(history) > 100:
        del history[:-100]

    notify(
        f"{result} {pos['side']} {pos['inst']} | {reason} | "
        f"gross {gross_pnl:+.4f} USDT | fee {fee:.4f} USDT | "
        f"NET {net_pnl:+.4f} USDT | held {hold_minutes:.1f} min | "
        f"exit {hist.get('closeAveragePrice')}",
        "BloFin LIVE RESULT",
    )
    state["position"] = None
    return True


def close_tracked_position(state, reason):
    pos = state.get("position")
    if not pos:
        return
    private_request(
        "POST",
        "/api/v1/trade/close-position",
        body={
            "instId": pos["inst"],
            "marginMode": MARGIN_MODE,
            "positionSide": "net",
            "clientOrderId": ("liveclose" + uuid.uuid4().hex)[:32],
        },
    )
    record_closed(state, reason)


def sync_tracked_position(state):
    open_positions = get_open_positions()
    tracked = state.get("position")
    if tracked:
        matching = [p for p in open_positions if p.get("instId") == tracked.get("inst")]
        if not matching:
            record_closed(state, "TP/SL or external close")
            return get_open_positions()
        age_ms = now_ms() - int(tracked.get("opened_ms") or now_ms())
        if age_ms >= HOLD_HOURS * D1H_MS:
            close_tracked_position(state, "MAX 5H")
            return get_open_positions()
    return open_positions


def place_live_trade(state, candidate, tickers, instruments):
    _, inst, side, signal_close_ms = candidate
    signal_age_ms = now_ms() - int(signal_close_ms)
    if signal_age_ms < 0 or signal_age_ms > SIGNAL_MAX_AGE_MS:
        raise RuntimeError(
            f"{inst}: stale signal ({signal_age_ms / 1000:.1f}s old; "
            f"max {SIGNAL_MAX_AGE_MS / 1000:.0f}s)"
        )

    market_reference = d(tickers[inst]["last"])
    available = get_available_usdt()
    # Keep a 2% cash buffer for fees/rounding while never exceeding the requested cap.
    cap = min(MAX_NOTIONAL_USDT, available * Decimal("0.98"))
    sized = size_for_notional(market_reference, instruments[inst], cap)
    if sized is None:
        raise RuntimeError(f"{inst}: minimum contract/lot exceeds live cap {cap:.4f} USDT")
    size, estimated_notional = sized
    set_one_x(inst)

    order_side = "buy" if side == "LONG" else "sell"
    client_id = ("live" + uuid.uuid4().hex)[:32]
    body = {
        "instId": inst,
        "marginMode": MARGIN_MODE,
        "positionSide": "net",
        "side": order_side,
        "orderType": "market",
        "size": clean_decimal(size),
        "reduceOnly": "false",
        "clientOrderId": client_id,
    }
    data = private_request("POST", "/api/v1/trade/order", body=body)
    row = data[0] if isinstance(data, list) and data else (data or {})
    if str(row.get("code", "0")) != "0":
        raise RuntimeError(f"Order rejected: {row}")

    order_id = str(row.get("orderId") or "")
    opened_ms = now_ms()
    state["position"] = {
        "inst": inst,
        "side": side,
        "opened_ms": opened_ms,
        "signal_close_ms": signal_close_ms,
        "signal_age_ms": signal_age_ms,
        "order_id": order_id,
        "client_order_id": client_id,
        "requested_reference": clean_decimal(market_reference),
        "reference_entry": "",
        "tp": "",
        "sl": "",
        "size": clean_decimal(size),
        "notional_usdt": clean_decimal(estimated_notional),
        "protection_status": "WAITING_FOR_FILL",
    }

    fill = wait_for_order_fill(inst, order_id, client_id)
    if not fill:
        state["position"]["protection_status"] = "ENTRY_PRICE_UNAVAILABLE"
        notify(
            f"CRITICAL {side} {inst}: market order was accepted but actual fill price "
            "could not be confirmed. Closing the position for safety.",
            "BloFin LIVE SAFETY",
        )
        try:
            close_tracked_position(state, "PROTECTION FAILURE: no fill price")
        except Exception as exc:
            state["position"]["protection_error"] = str(exc)
            notify(
                f"CRITICAL {side} {inst}: automatic safety close also failed: {exc}",
                "BloFin LIVE SAFETY",
            )
        return

    fill_price = d(fill.get("averagePrice") or "0")
    filled_size = d(fill.get("filledSize") or size)
    tick = d(instruments[inst].get("tickSize") or "0.00000001")
    if side == "LONG":
        tp = price_step(fill_price * (Decimal("1") + TP_PCT), tick, ROUND_CEILING)
        sl = price_step(fill_price * (Decimal("1") - SL_PCT), tick, ROUND_FLOOR)
    else:
        tp = price_step(fill_price * (Decimal("1") - TP_PCT), tick, ROUND_FLOOR)
        sl = price_step(fill_price * (Decimal("1") + SL_PCT), tick, ROUND_CEILING)

    contract_value = d(instruments[inst].get("contractValue") or "0")
    actual_notional = filled_size * contract_value * fill_price if contract_value > 0 else estimated_notional
    state["position"].update({
        "reference_entry": clean_decimal(fill_price),
        "entry_fee": str(fill.get("fee") or "0"),
        "filled_size": clean_decimal(filled_size),
        "notional_usdt": clean_decimal(actual_notional),
        "tp": clean_decimal(tp),
        "sl": clean_decimal(sl),
        "protection_status": "PLACING_TPSL",
    })

    try:
        tpsl_id, tpsl_client_id = place_tpsl_for_position(inst, side, tp, sl)
    except Exception as exc:
        state["position"]["protection_status"] = "TPSL_FAILED"
        state["position"]["protection_error"] = str(exc)
        notify(
            f"CRITICAL {side} {inst}: TP/SL could not be placed after entry at "
            f"{fill_price}. Closing the position for safety. Error: {exc}",
            "BloFin LIVE SAFETY",
        )
        try:
            close_tracked_position(state, "PROTECTION FAILURE: TP/SL rejected")
        except Exception as close_exc:
            state["position"]["protection_error"] = f"{exc}; safety close failed: {close_exc}"
            notify(
                f"CRITICAL {side} {inst}: safety close failed too: {close_exc}",
                "BloFin LIVE SAFETY",
            )
        return

    state["position"]["protection_status"] = "ACTIVE"
    state["position"]["tpsl_id"] = tpsl_id
    state["position"]["tpsl_client_order_id"] = tpsl_client_id
    notify(
        f"OPEN {side} {inst} | 1x isolated | actual entry {fill_price} | "
        f"notional≈{actual_notional:.4f} USDT | TP {tp} (+0.6%) | "
        f"SL {sl} (-0.5%) | signal age {signal_age_ms / 1000:.0f}s | max 5h",
        "BloFin LIVE OPEN",
    )

def status(state, top10):
    available = get_available_usdt()
    pos = state.get("position")
    if pos:
        age_h = max(0.0, (now_ms() - int(pos["opened_ms"])) / D1H_MS)
        pos_text = f"OPEN {pos['side']} {pos['inst']} | {age_h:.1f}h/5h"
    else:
        pos_text = "no tracked open position"
    t = state.get("trades", {})
    notify(
        f"LIVE status | cap {MAX_NOTIONAL_USDT:.4f} USDT | available {available:.4f} USDT | "
        f"NET realized {d(state.get('realized_pnl_usdt', 0)):+.4f} | "
        f"fees {d(state.get('fees_usdt', 0)):.4f} | trades {t.get('total', 0)} "
        f"(W{t.get('wins', 0)}/L{t.get('losses', 0)}/F{t.get('flat', 0)}/U{t.get('unverified', 0)}) | "
        f"{pos_text} | "
        f"TOP3: {', '.join(top10[:3]) if top10 else 'none'}",
        "BloFin LIVE 1H",
    )


def main():
    require_live_enabled()
    require_account_modes()
    state = load_state()
    top10, tickers, instruments = get_universe()
    if not top10:
        raise RuntimeError("BloFin TOP10 is empty")

    open_positions = sync_tracked_position(state)
    candidates = evaluate_signals(state, top10)

    if not state.get("position"):
        if open_positions:
            names = ", ".join(str(p.get("instId")) for p in open_positions[:5])
            notify(
                f"No new LIVE order: account already has an untracked open position ({names}).",
                "BloFin LIVE BLOCKED",
            )
        elif candidates:
            place_live_trade(state, candidates[0], tickers, instruments)

    status(state, top10)
    state["last_run_ms"] = now_ms()
    state["last_top10"] = top10
    save_state(state)
    print(json.dumps({
        "cap": str(MAX_NOTIONAL_USDT),
        "position": state.get("position"),
        "trades": state.get("trades"),
        "top10": top10,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
