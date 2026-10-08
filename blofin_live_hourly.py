import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

BASE = "https://openapi.blofin.com"
API_KEY = os.getenv("BLOFIN_API_KEY", "").strip()
SECRET = os.getenv("BLOFIN_SECRET_KEY", "").strip()
PASSPHRASE = os.getenv("BLOFIN_PASSPHRASE", "").strip()
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "blofin-nhd0jt7wspfnhtitdlaowk1n").strip()
STATE_FILE = os.getenv("LIVE_STATE_FILE", "blofin_live_state.json")
CODE_COMMIT = os.getenv("GITHUB_SHA", "").strip()
TECH_EVENTS_FILE = os.getenv("LIVE_TECH_EVENTS_FILE", "blofin_live_technical_events.json")
TECH_EVENTS_LIMIT = 5000
PATTERN_AUDIT_FILE = os.getenv("LIVE_PATTERN_AUDIT_FILE", "blofin_pattern_learning.json")
PATTERN_AUDIT_LIMIT = 5000
LIVE_ENABLED = os.getenv("BLOFIN_LIVE_ENABLED", "").strip().lower() == "true"
MAX_NOTIONAL_USDT = Decimal(os.getenv("LIVE_MAX_BANKROLL_USDT", "10.64"))
LEVERAGE = "1"
MARGIN_MODE = "isolated"
TOP_N = 7
MAX_OPEN_POSITIONS = 4
TRADE_HISTORY_LIMIT = 5000
POSITION_FRACTION = Decimal("0.25")  # total LIVE bankroll is still hard-capped below
HARD_SL_PCT = Decimal("0.01")
TAKE_PROFIT_PCT = Decimal("0.01")
HOLD_MINUTES = 60
HOLD_MS = HOLD_MINUTES * 60 * 1000
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
VOL_MA_FAST = 5
VOL_MA_SLOW = 10
RECENT_SPIKE_LOOKBACK = 5
SIGNAL_MINUTES = int(os.getenv("LIVE_SIGNAL_MINUTES", "15"))
MAX_DRAWDOWN_PCT = Decimal(os.getenv("LIVE_MAX_DRAWDOWN_PCT", "0.10"))
if SIGNAL_MINUTES == 5:
    SIGNAL_BAR = "5m"
    SOURCE_BAR_MS = 5 * 60 * 1000
    SIGNAL_MS = 5 * 60 * 1000
elif SIGNAL_MINUTES == 10:
    SIGNAL_BAR = "5m"
    SOURCE_BAR_MS = 5 * 60 * 1000
    SIGNAL_MS = 10 * 60 * 1000
elif SIGNAL_MINUTES == 15:
    SIGNAL_BAR = "15m"
    SOURCE_BAR_MS = 15 * 60 * 1000
    SIGNAL_MS = 15 * 60 * 1000
elif SIGNAL_MINUTES == 240:
    # Build 4H candles in UK local time from closed 1H BloFin candles.
    SIGNAL_BAR = "1H"
    SOURCE_BAR_MS = 60 * 60 * 1000
    SIGNAL_MS = 4 * 60 * 60 * 1000
else:
    raise RuntimeError("LIVE_SIGNAL_MINUTES must be 5, 10, 15 or 240")
SIGNAL_LABEL = "4H" if SIGNAL_MINUTES == 240 else f"{SIGNAL_MINUTES}m"
D1H_MS = SIGNAL_MS  # compatibility alias for older helper names/state code
HTTP_TIMEOUT = 25
MAX_RETRIES = 4
SIGNAL_MAX_AGE_MS = int(os.getenv("LIVE_SIGNAL_MAX_AGE_MS", "180000"))
ENTRY_FILL_ATTEMPTS = 20
ENTRY_FILL_DELAY_SEC = 0.5
CLOSE_HISTORY_ATTEMPTS = 8
CLOSE_HISTORY_DELAY_SEC = 3
USER_AGENT = "Mozilla/5.0 BloFinStockFinder/1.0"
UK_TZ = ZoneInfo("Europe/London")


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
        detail = payload.get("data")
        raise RuntimeError(
            f"BloFin API {path}: {payload.get('code')} {payload.get('msg')} | "
            f"details={json.dumps(detail, ensure_ascii=False)}"
        )
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
            positions = state.setdefault("positions", {})
            legacy = state.get("position")
            if isinstance(legacy, dict) and legacy.get("inst"):
                positions.setdefault(str(legacy["inst"]), legacy)
            state["position"] = None
            return state
    except Exception:
        pass
    return {
        "version": 1,
        "started_at_ms": now_ms(),
        "position": None,
        "positions": {},
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


def append_technical_event(
    event_type,
    message,
    inst=None,
    side=None,
    rank=None,
    signal_close_ms=None,
    extra=None,
):
    """Persist non-strategy execution problems separately from trade history."""
    try:
        try:
            with open(TECH_EVENTS_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        events = payload.setdefault("events", [])
        payload["version"] = 1
        event = {
            "timestamp_ms": now_ms(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "event_type": str(event_type),
            "message": str(message),
            "signal_label": SIGNAL_LABEL,
        }
        if inst is not None:
            event["inst"] = str(inst)
        if side is not None:
            event["side"] = str(side)
        if rank is not None:
            event["rank"] = int(rank)
        if signal_close_ms is not None:
            event["signal_close_ms"] = int(signal_close_ms)
        if isinstance(extra, dict) and extra:
            event["extra"] = extra

        events.append(event)
        if len(events) > TECH_EVENTS_LIMIT:
            del events[:-TECH_EVENTS_LIMIT]

        tmp = TECH_EVENTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, TECH_EVENTS_FILE)
    except Exception as exc:
        print(f"TECH EVENT LOG ERROR: {type(exc).__name__}: {exc}")


def classify_entry_error(exc):
    text = str(exc).lower()
    if "minimum contract" in text or "lot exceeds" in text:
        return "SIZE_BLOCK"
    if "no available" in text or "insufficient" in text or "balance" in text:
        return "NO_FUNDS"
    if "stale signal" in text:
        return "STALE_SIGNAL"
    if "bankroll" in text or "exposure" in text:
        return "BANKROLL_BLOCK"
    if (
        "blofin api" in text
        or "market request failed" in text
        or "http" in text
        or "timeout" in text
        or "429" in text
        or "connection" in text
    ):
        return "API_ERROR"
    return "ENTRY_ERROR"


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


def ema_series(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def macd_series(bars):
    closes = [float(bar["c"]) for bar in bars]
    fast = ema_series(closes, MACD_FAST)
    slow = ema_series(closes, MACD_SLOW)
    dif = [None] * len(bars)
    for i in range(len(bars)):
        if fast[i] is not None and slow[i] is not None:
            dif[i] = fast[i] - slow[i]

    dea = [None] * len(bars)
    valid = [(i, value) for i, value in enumerate(dif) if value is not None]
    if len(valid) >= MACD_SIGNAL:
        seed = sum(value for _, value in valid[:MACD_SIGNAL]) / MACD_SIGNAL
        seed_index = valid[MACD_SIGNAL - 1][0]
        dea[seed_index] = seed
        prev = seed
        alpha = 2.0 / (MACD_SIGNAL + 1.0)
        for idx, value in valid[MACD_SIGNAL:]:
            prev = value * alpha + prev * (1.0 - alpha)
            dea[idx] = prev

    hist = [
        (dif[i] - dea[i]) if dif[i] is not None and dea[i] is not None else None
        for i in range(len(bars))
    ]
    return dif, dea, hist


def decorate(bars):
    rsi = rsi_wilder(bars, RSI_PERIOD)
    dif, dea, hist = macd_series(bars)
    for i, bar in enumerate(bars):
        bar["rsi"] = rsi[i]
        bar["macd_dif"] = dif[i]
        bar["macd_dea"] = dea[i]
        bar["macd_hist"] = hist[i]
    return bars


def volume_ma(bars, i, period):
    if i + 1 < period:
        return None
    return sum(float(bars[j]["v"]) for j in range(i - period + 1, i + 1)) / period


def short_entry_metrics(bars, i):
    if i < 1:
        return None
    prev = bars[i - 1]
    cur = bars[i]
    needed = (
        prev.get("macd_dif"),
        prev.get("macd_dea"),
        prev.get("macd_hist"),
        cur.get("macd_dif"),
        cur.get("macd_dea"),
        cur.get("macd_hist"),
    )
    if any(value is None for value in needed):
        return None

    macd_cross_down = (
        float(prev["macd_dif"]) >= float(prev["macd_dea"])
        and float(cur["macd_dif"]) < float(cur["macd_dea"])
        and float(prev["macd_hist"]) >= 0
        and float(cur["macd_hist"]) < 0
    )
    red_candle = float(cur["c"]) < float(cur["o"])

    last_green_volume = None
    for j in range(i - 1, -1, -1):
        if candle_color(bars[j]) == "GREEN":
            last_green_volume = float(bars[j]["v"])
            break

    volume_higher_than_last_green = (
        last_green_volume is not None
        and float(cur["v"]) > last_green_volume
    )

    red_body_pct_close = (
        ((float(cur["o"]) - float(cur["c"])) / float(cur["c"])) * 100.0
        if float(cur["c"]) > 0
        else float("inf")
    )
    candle_range = float(cur["h"]) - float(cur["l"])
    lower_wick = min(float(cur["o"]), float(cur["c"])) - float(cur["l"])
    lower_wick_pct_range = (
        (lower_wick / candle_range) * 100.0
        if candle_range > 0
        else 100.0
    )
    close_4_bars_ago = float(bars[i - 4]["c"]) if i >= 4 else None
    return_4_bars_pct = (
        ((float(cur["c"]) / close_4_bars_ago) - 1.0) * 100.0
        if close_4_bars_ago is not None and close_4_bars_ago > 0
        else float("inf")
    )
    close_20_bars_ago = float(bars[i - 20]["c"]) if i >= 20 else None
    return_20_bars_pct = (
        ((float(cur["c"]) / close_20_bars_ago) - 1.0) * 100.0
        if close_20_bars_ago is not None and close_20_bars_ago > 0
        else float("inf")
    )

    return {
        "macd_cross_down": macd_cross_down,
        "red_candle": red_candle,
        "volume_higher_than_last_green": volume_higher_than_last_green,
        "red_body_pct_close": red_body_pct_close,
        "short_body_max_1pct": red_body_pct_close <= 1.0,
        "lower_wick_pct_range": lower_wick_pct_range,
        "lower_wick_max_50pct": lower_wick_pct_range <= 50.0,
        "return_4_bars_pct": return_4_bars_pct,
        "return_4_bars_min_minus_2pct": return_4_bars_pct >= -2.0,
        "return_4_bars_max_1pct": return_4_bars_pct <= 1.0,
        "return_20_bars_pct": return_20_bars_pct,
        "return_20_bars_max_10pct": return_20_bars_pct <= 10.0,
        "volume": float(cur["v"]),
        "last_green_volume": last_green_volume,
        "macd_dif": float(cur["macd_dif"]),
        "macd_dea": float(cur["macd_dea"]),
        "macd_hist": float(cur["macd_hist"]),
    }


def short_entry_signal(bars, i):
    metrics = short_entry_metrics(bars, i)
    return bool(
        metrics
        and metrics["red_candle"]
        and metrics["volume_higher_than_last_green"]
        and metrics["red_body_pct_close"] <= 1.0
        and metrics["lower_wick_max_50pct"]
        and metrics["return_4_bars_min_minus_2pct"]
        and metrics["return_4_bars_max_1pct"]
        and metrics["return_20_bars_max_10pct"]
    )


def stochastic_kd_series(bars, k_period=14, d_period=3):
    """Raw Stochastic %K with 1-bar K smoothing and SMA %D."""
    k_values = [None] * len(bars)
    d_values = [None] * len(bars)

    for idx in range(k_period - 1, len(bars)):
        window = bars[idx - k_period + 1: idx + 1]
        highest = max(float(bar["h"]) for bar in window)
        lowest = min(float(bar["l"]) for bar in window)
        close = float(bars[idx]["c"])
        k_values[idx] = (
            50.0
            if highest == lowest
            else ((close - lowest) / (highest - lowest)) * 100.0
        )

        if idx >= (k_period - 1) + (d_period - 1):
            recent_k = k_values[idx - d_period + 1: idx + 1]
            if all(value is not None for value in recent_k):
                d_values[idx] = sum(recent_k) / d_period

    return k_values, d_values


def recent_stoch_cross_down(bars, i, lookback_bars=3):
    """True when Stochastic 14,1,3 crossed down on the signal bar or <=3 bars ago."""
    k_values, d_values = stochastic_kd_series(bars, 14, 3)
    start = max(1, i - lookback_bars)

    for idx in range(start, i + 1):
        prev_k, prev_d = k_values[idx - 1], d_values[idx - 1]
        cur_k, cur_d = k_values[idx], d_values[idx]
        if None in (prev_k, prev_d, cur_k, cur_d):
            continue
        if prev_k >= prev_d and cur_k < cur_d:
            return True

    return False


def long_entry_metrics(bars, i):
    if i < 1:
        return None
    prev = bars[i - 1]
    cur = bars[i]
    needed = (
        prev.get("macd_dif"),
        prev.get("macd_dea"),
        prev.get("macd_hist"),
        cur.get("macd_dif"),
        cur.get("macd_dea"),
        cur.get("macd_hist"),
    )
    if any(value is None for value in needed):
        return None

    macd_cross_up = (
        float(prev["macd_dif"]) <= float(prev["macd_dea"])
        and float(cur["macd_dif"]) > float(cur["macd_dea"])
        and float(prev["macd_hist"]) <= 0
        and float(cur["macd_hist"]) > 0
    )
    green_candle = float(cur["c"]) > float(cur["o"])

    last_red_volume = None
    for j in range(i - 1, -1, -1):
        if candle_color(bars[j]) == "RED":
            last_red_volume = float(bars[j]["v"])
            break

    volume_higher_than_last_red = (
        last_red_volume is not None
        and float(cur["v"]) > last_red_volume
    )

    candle_range = float(cur["h"]) - float(cur["l"])
    green_body_ratio = (
        (float(cur["c"]) - float(cur["o"])) / candle_range
        if candle_range > 0 and green_candle
        else 0.0
    )
    close_10_bars_ago = float(bars[i - 10]["c"]) if i >= 10 else None
    rise_10_bars_pct = (
        ((float(cur["c"]) / close_10_bars_ago) - 1.0) * 100.0
        if close_10_bars_ago is not None and close_10_bars_ago > 0
        else float("-inf")
    )

    stoch_k_values, stoch_d_values = stochastic_kd_series(bars, 14, 3)
    stoch_k = stoch_k_values[i] if i < len(stoch_k_values) else None
    stoch_d = stoch_d_values[i] if i < len(stoch_d_values) else None
    stoch_cross_down_recent_3 = recent_stoch_cross_down(bars, i, 3)
    stoch_long_ok = not stoch_cross_down_recent_3
    rsi14 = cur.get("rsi")
    rsi_below_70 = rsi14 is not None and float(rsi14) < 70.0

    return {
        "macd_cross_up": macd_cross_up,
        "green_candle": green_candle,
        "volume_higher_than_last_red": volume_higher_than_last_red,
        "green_body_ratio": green_body_ratio,
        "green_body_min_60pct": green_body_ratio >= 0.60,
        "rise_10_bars_pct": rise_10_bars_pct,
        "rise_10_bars_min_2pct": rise_10_bars_pct >= 2.0,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "stoch_cross_down_recent_3": stoch_cross_down_recent_3,
        "stoch_long_ok": stoch_long_ok,
        "rsi14": None if rsi14 is None else float(rsi14),
        "rsi_below_70": rsi_below_70,
        "volume": float(cur["v"]),
        "last_red_volume": last_red_volume,
        "macd_dif": float(cur["macd_dif"]),
        "macd_dea": float(cur["macd_dea"]),
        "macd_hist": float(cur["macd_hist"]),
    }


def long_entry_signal(bars, i):
    metrics = long_entry_metrics(bars, i)
    return bool(
        metrics
        and metrics["green_candle"]
        and metrics["volume_higher_than_last_red"]
        and metrics["green_body_min_60pct"]
        and metrics["rise_10_bars_min_2pct"]
        and metrics["stoch_long_ok"]
        and metrics["rsi_below_70"]
    )


def candle_color(bar):
    if bar["c"] > bar["o"]:
        return "GREEN"
    if bar["c"] < bar["o"]:
        return "RED"
    return "DOJI"



def _pattern_slope_pct(values):
    """Linear slope as percent of the average value per bar."""
    if len(values) < 2:
        return 0.0
    n = len(values)
    x_mean = (n - 1) / 2.0
    y_mean = sum(float(v) for v in values) / n
    if y_mean == 0:
        return 0.0
    denom = sum((x - x_mean) ** 2 for x in range(n))
    if denom == 0:
        return 0.0
    slope = sum(
        (x - x_mean) * (float(values[x]) - y_mean)
        for x in range(n)
    ) / denom
    return (slope / abs(y_mean)) * 100.0


def _pattern_pivots(bars, start, end, field, kind, wing=2):
    out = []
    start = max(start, wing)
    end = min(end, len(bars) - wing - 1)
    for idx in range(start, end + 1):
        value = float(bars[idx][field])
        neighbors = [
            float(bars[j][field])
            for j in range(idx - wing, idx + wing + 1)
            if j != idx
        ]
        if kind == "HIGH" and all(value >= x for x in neighbors):
            out.append((idx, value))
        elif kind == "LOW" and all(value <= x for x in neighbors):
            out.append((idx, value))
    return out


def detect_chart_patterns(bars, i, lookback=40):
    """Passive 15m pattern detector. It NEVER changes an entry/exit decision."""
    if i < 19:
        return []

    start = max(0, i - lookback + 1)
    window = bars[start:i + 1]
    patterns = []

    def add(name, bias, confidence, evidence):
        if not any(item["name"] == name for item in patterns):
            patterns.append({
                "name": name,
                "bias": bias,
                "confidence": round(float(confidence), 3),
                "evidence": evidence,
            })

    def gap_pct(a, b):
        base = max((abs(float(a)) + abs(float(b))) / 2.0, 1e-12)
        return abs(float(a) - float(b)) / base * 100.0

    piv_hi = _pattern_pivots(bars, start, i, "h", "HIGH")
    piv_lo = _pattern_pivots(bars, start, i, "l", "LOW")

    # Double top / bottom: two completed pivots close in price with a meaningful swing between.
    if len(piv_hi) >= 2:
        (a_i, a), (b_i, b) = piv_hi[-2], piv_hi[-1]
        if b_i - a_i >= 3 and i - b_i <= 12 and gap_pct(a, b) <= 1.0:
            valley = min(float(bars[j]["l"]) for j in range(a_i, b_i + 1))
            top = (a + b) / 2.0
            depth = ((top - valley) / top * 100.0) if top > 0 else 0.0
            if depth >= 0.8:
                add("DOUBLE_TOP", "SHORT", min(0.9, 0.62 + depth / 20.0), {
                    "peak_gap_pct": round(gap_pct(a, b), 3),
                    "neck_depth_pct": round(depth, 3),
                })

    if len(piv_lo) >= 2:
        (a_i, a), (b_i, b) = piv_lo[-2], piv_lo[-1]
        if b_i - a_i >= 3 and i - b_i <= 12 and gap_pct(a, b) <= 1.0:
            peak = max(float(bars[j]["h"]) for j in range(a_i, b_i + 1))
            bottom = (a + b) / 2.0
            depth = ((peak - bottom) / bottom * 100.0) if bottom > 0 else 0.0
            if depth >= 0.8:
                add("DOUBLE_BOTTOM", "LONG", min(0.9, 0.62 + depth / 20.0), {
                    "bottom_gap_pct": round(gap_pct(a, b), 3),
                    "neck_height_pct": round(depth, 3),
                })

    # Triple top / bottom: three similar completed pivots with meaningful swings between.
    if len(piv_hi) >= 3:
        (a_i, a), (b_i, b), (c_i, c) = piv_hi[-3:]
        max_gap = max(gap_pct(a, b), gap_pct(b, c), gap_pct(a, c))
        if (
            b_i - a_i >= 3
            and c_i - b_i >= 3
            and i - c_i <= 12
            and max_gap <= 1.2
        ):
            valley1 = min(float(bars[j]["l"]) for j in range(a_i, b_i + 1))
            valley2 = min(float(bars[j]["l"]) for j in range(b_i, c_i + 1))
            top = (a + b + c) / 3.0
            depth1 = ((top - valley1) / top * 100.0) if top > 0 else 0.0
            depth2 = ((top - valley2) / top * 100.0) if top > 0 else 0.0
            if min(depth1, depth2) >= 0.8:
                add("TRIPLE_TOP", "SHORT", min(0.92, 0.68 + min(depth1, depth2) / 20.0), {
                    "max_peak_gap_pct": round(max_gap, 3),
                    "valley1_depth_pct": round(depth1, 3),
                    "valley2_depth_pct": round(depth2, 3),
                })

    if len(piv_lo) >= 3:
        (a_i, a), (b_i, b), (c_i, c) = piv_lo[-3:]
        max_gap = max(gap_pct(a, b), gap_pct(b, c), gap_pct(a, c))
        if (
            b_i - a_i >= 3
            and c_i - b_i >= 3
            and i - c_i <= 12
            and max_gap <= 1.2
        ):
            peak1 = max(float(bars[j]["h"]) for j in range(a_i, b_i + 1))
            peak2 = max(float(bars[j]["h"]) for j in range(b_i, c_i + 1))
            bottom = (a + b + c) / 3.0
            height1 = ((peak1 - bottom) / bottom * 100.0) if bottom > 0 else 0.0
            height2 = ((peak2 - bottom) / bottom * 100.0) if bottom > 0 else 0.0
            if min(height1, height2) >= 0.8:
                add("TRIPLE_BOTTOM", "LONG", min(0.92, 0.68 + min(height1, height2) / 20.0), {
                    "max_bottom_gap_pct": round(max_gap, 3),
                    "peak1_height_pct": round(height1, 3),
                    "peak2_height_pct": round(height2, 3),
                })

    # Head & shoulders / inverse H&S from the latest three completed pivots.
    if len(piv_hi) >= 3:
        (l_i, left), (h_i, head), (r_i, right) = piv_hi[-3:]
        shoulders_gap = gap_pct(left, right)
        shoulder_ref = (left + right) / 2.0
        head_above = ((head / shoulder_ref) - 1.0) * 100.0 if shoulder_ref > 0 else 0.0
        if l_i < h_i < r_i and i - r_i <= 12 and shoulders_gap <= 1.5 and head_above >= 0.8:
            add("HEAD_AND_SHOULDERS", "SHORT", min(0.9, 0.65 + head_above / 20.0), {
                "shoulders_gap_pct": round(shoulders_gap, 3),
                "head_above_shoulders_pct": round(head_above, 3),
            })

    if len(piv_lo) >= 3:
        (l_i, left), (h_i, head), (r_i, right) = piv_lo[-3:]
        shoulders_gap = gap_pct(left, right)
        shoulder_ref = (left + right) / 2.0
        head_below = (1.0 - head / shoulder_ref) * 100.0 if shoulder_ref > 0 else 0.0
        if l_i < h_i < r_i and i - r_i <= 12 and shoulders_gap <= 1.5 and head_below >= 0.8:
            add("INVERSE_HEAD_AND_SHOULDERS", "LONG", min(0.9, 0.65 + head_below / 20.0), {
                "shoulders_gap_pct": round(shoulders_gap, 3),
                "head_below_shoulders_pct": round(head_below, 3),
            })

    # Triangles: compare linear boundary slopes and require visible range compression.
    tri = window[-12:]
    if len(tri) >= 10:
        highs = [float(x["h"]) for x in tri]
        lows = [float(x["l"]) for x in tri]
        high_slope = _pattern_slope_pct(highs)
        low_slope = _pattern_slope_pct(lows)
        first_width = max(highs[:3]) - min(lows[:3])
        last_width = max(highs[-3:]) - min(lows[-3:])
        narrowing = first_width > 0 and last_width / first_width <= 0.80
        evidence = {
            "high_slope_pct_per_bar": round(high_slope, 4),
            "low_slope_pct_per_bar": round(low_slope, 4),
            "width_ratio": round(last_width / first_width, 3) if first_width > 0 else None,
        }
        if narrowing and abs(high_slope) <= 0.08 and low_slope >= 0.05:
            add("ASCENDING_TRIANGLE", "LONG", 0.70, evidence)
        if narrowing and abs(low_slope) <= 0.08 and high_slope <= -0.05:
            add("DESCENDING_TRIANGLE", "SHORT", 0.70, evidence)
        if narrowing and high_slope <= -0.04 and low_slope >= 0.04:
            add("SYMMETRICAL_TRIANGLE", "NEUTRAL", 0.62, evidence)

    # Pennants: strong impulse followed by a short converging consolidation.
    pennant = window[-16:]
    if len(pennant) >= 14:
        impulse_start = float(pennant[0]["c"])
        impulse_end = float(pennant[-8]["c"])
        cons = pennant[-8:]
        if impulse_start > 0:
            impulse_pct = (impulse_end / impulse_start - 1.0) * 100.0
            highs = [float(x["h"]) for x in cons]
            lows = [float(x["l"]) for x in cons]
            high_slope = _pattern_slope_pct(highs)
            low_slope = _pattern_slope_pct(lows)
            first_width = max(highs[:3]) - min(lows[:3])
            last_width = max(highs[-3:]) - min(lows[-3:])
            narrowing = first_width > 0 and last_width / first_width <= 0.78
            evidence = {
                "impulse_pct": round(impulse_pct, 3),
                "high_slope_pct_per_bar": round(high_slope, 4),
                "low_slope_pct_per_bar": round(low_slope, 4),
                "width_ratio": round(last_width / first_width, 3) if first_width > 0 else None,
            }
            if narrowing and high_slope <= -0.03 and low_slope >= 0.03:
                if impulse_pct >= 3.0:
                    add("BULL_PENNANT", "LONG", 0.69, evidence)
                if impulse_pct <= -3.0:
                    add("BEAR_PENNANT", "SHORT", 0.69, evidence)

    # Wedges: both boundaries trend in the same direction while converging.
    wedge = window[-14:]
    if len(wedge) >= 12:
        highs = [float(x["h"]) for x in wedge]
        lows = [float(x["l"]) for x in wedge]
        high_slope = _pattern_slope_pct(highs)
        low_slope = _pattern_slope_pct(lows)
        first_width = max(highs[:3]) - min(lows[:3])
        last_width = max(highs[-3:]) - min(lows[-3:])
        narrowing = first_width > 0 and last_width / first_width <= 0.82
        evidence = {
            "high_slope_pct_per_bar": round(high_slope, 4),
            "low_slope_pct_per_bar": round(low_slope, 4),
            "width_ratio": round(last_width / first_width, 3) if first_width > 0 else None,
        }
        if narrowing and high_slope > 0.03 and low_slope > high_slope + 0.02:
            add("RISING_WEDGE", "SHORT", 0.66, evidence)
        if narrowing and low_slope < -0.03 and high_slope < low_slope - 0.02:
            add("FALLING_WEDGE", "LONG", 0.66, evidence)

    # Flags: strong impulse followed by a smaller counter-trend consolidation.
    flag = window[-16:]
    if len(flag) >= 14:
        impulse_start = float(flag[0]["c"])
        impulse_end = float(flag[-7]["c"])
        flag_start = float(flag[-7]["c"])
        flag_end = float(flag[-1]["c"])
        if impulse_start > 0 and flag_start > 0:
            impulse_pct = (impulse_end / impulse_start - 1.0) * 100.0
            flag_pct = (flag_end / flag_start - 1.0) * 100.0
            recent_high = max(float(x["h"]) for x in flag[-7:])
            recent_low = min(float(x["l"]) for x in flag[-7:])
            recent_range_pct = (recent_high / recent_low - 1.0) * 100.0 if recent_low > 0 else 999.0
            evidence = {
                "impulse_pct": round(impulse_pct, 3),
                "flag_move_pct": round(flag_pct, 3),
                "flag_range_pct": round(recent_range_pct, 3),
            }
            if impulse_pct >= 3.0 and -2.0 <= flag_pct <= 0.5 and recent_range_pct <= 4.0:
                add("BULL_FLAG", "LONG", 0.67, evidence)
            if impulse_pct <= -3.0 and -0.5 <= flag_pct <= 2.0 and recent_range_pct <= 4.0:
                add("BEAR_FLAG", "SHORT", 0.67, evidence)

    # Cup & handle / inverse cup & handle: rounded recovery plus a shallow handle.
    cupwin = window[-30:]
    if len(cupwin) >= 26:
        highs = [float(x["h"]) for x in cupwin]
        lows = [float(x["l"]) for x in cupwin]
        closes = [float(x["c"]) for x in cupwin]

        left_rim = max(highs[:6])
        right_rim = max(highs[-10:-5])
        trough_slice = lows[6:-10]
        if trough_slice:
            trough = min(trough_slice)
            rim = (left_rim + right_rim) / 2.0
            rim_gap = gap_pct(left_rim, right_rim)
            cup_depth = ((rim - trough) / rim * 100.0) if rim > 0 else 0.0
            handle_low = min(lows[-5:])
            handle_drop = ((right_rim - handle_low) / right_rim * 100.0) if right_rim > 0 else 999.0
            if (
                rim_gap <= 2.0
                and cup_depth >= 2.0
                and handle_drop <= max(1.5, cup_depth * 0.45)
                and closes[-1] >= trough
            ):
                add("CUP_AND_HANDLE", "LONG", min(0.88, 0.66 + cup_depth / 30.0), {
                    "rim_gap_pct": round(rim_gap, 3),
                    "cup_depth_pct": round(cup_depth, 3),
                    "handle_drop_pct": round(handle_drop, 3),
                })

        left_floor = min(lows[:6])
        right_floor = min(lows[-10:-5])
        dome_slice = highs[6:-10]
        if dome_slice:
            dome = max(dome_slice)
            floor = (left_floor + right_floor) / 2.0
            floor_gap = gap_pct(left_floor, right_floor)
            dome_height = ((dome - floor) / floor * 100.0) if floor > 0 else 0.0
            handle_high = max(highs[-5:])
            handle_rise = ((handle_high - right_floor) / right_floor * 100.0) if right_floor > 0 else 999.0
            if (
                floor_gap <= 2.0
                and dome_height >= 2.0
                and handle_rise <= max(1.5, dome_height * 0.45)
                and closes[-1] <= dome
            ):
                add("INVERSE_CUP_AND_HANDLE", "SHORT", min(0.88, 0.66 + dome_height / 30.0), {
                    "floor_gap_pct": round(floor_gap, 3),
                    "dome_height_pct": round(dome_height, 3),
                    "handle_rise_pct": round(handle_rise, 3),
                })

    return patterns


def pattern_entry_filter(bars, i, side):
    """Active entry filter: block trades when a detected chart pattern points the other way."""
    patterns = detect_chart_patterns(bars, i)
    opposing_bias = "LONG" if side == "SHORT" else "SHORT"
    opposing = [
        pattern for pattern in patterns
        if str(pattern.get("bias") or "").upper() == opposing_bias
    ]
    return {
        "allowed": not opposing,
        "patterns": patterns,
        "opposing": opposing,
    }


def review_chart_patterns(patterns, trade_side, result):
    """Score whether using each detected pattern as a directional filter would help."""
    reviews = []
    for pattern in patterns or []:
        bias = str(pattern.get("bias") or "NEUTRAL")
        if bias == trade_side:
            relation = "SUPPORTS"
        elif bias in ("LONG", "SHORT"):
            relation = "OPPOSES"
        else:
            relation = "NEUTRAL"

        if result not in ("WIN", "LOSS") or relation == "NEUTRAL":
            effect = "NO_EFFECT"
        elif (relation == "SUPPORTS" and result == "WIN") or (
            relation == "OPPOSES" and result == "LOSS"
        ):
            effect = "HELPED"
        else:
            effect = "HURT"

        reviews.append({
            "name": pattern.get("name"),
            "bias": bias,
            "confidence": pattern.get("confidence"),
            "relation_to_trade": relation,
            "effect_if_used_as_filter": effect,
            "evidence": pattern.get("evidence") or {},
        })
    return reviews


def append_pattern_audit(pos, result, closed_ms, net_pnl):
    """Write passive pattern evidence without changing the LIVE strategy."""
    try:
        try:
            with open(PATTERN_AUDIT_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        patterns = (
            (pos.get("signal_snapshot") or {}).get("chart_patterns")
            if isinstance(pos, dict)
            else []
        ) or []
        reviews = review_chart_patterns(patterns, str(pos.get("side") or ""), result)

        payload["version"] = 1
        payload["mode"] = "PASSIVE_ONLY_DOES_NOT_BLOCK_TRADES"
        payload["interpretation"] = (
            "HELPED = pattern supported a winner or opposed a loser; "
            "HURT = pattern supported a loser or opposed a winner."
        )
        events = payload.setdefault("events", [])
        events.append({
            "closed_ms": int(closed_ms),
            "inst": str(pos.get("inst") or ""),
            "side": str(pos.get("side") or ""),
            "result": str(result),
            "net_pnl_usdt": float(net_pnl),
            "signal_close_ms": int(pos.get("signal_close_ms") or 0),
            "patterns": patterns,
            "reviews": reviews,
        })
        if len(events) > PATTERN_AUDIT_LIMIT:
            del events[:-PATTERN_AUDIT_LIMIT]

        summary = payload.setdefault("summary", {})
        for item in reviews:
            name = str(item.get("name") or "UNKNOWN")
            row = summary.setdefault(name, {
                "seen": 0,
                "helped": 0,
                "hurt": 0,
                "no_effect": 0,
                "supports_trade": 0,
                "opposes_trade": 0,
                "wins_when_present": 0,
                "losses_when_present": 0,
            })
            row["seen"] += 1
            effect = str(item.get("effect_if_used_as_filter") or "NO_EFFECT")
            if effect == "HELPED":
                row["helped"] += 1
            elif effect == "HURT":
                row["hurt"] += 1
            else:
                row["no_effect"] += 1
            relation = str(item.get("relation_to_trade") or "NEUTRAL")
            if relation == "SUPPORTS":
                row["supports_trade"] += 1
            elif relation == "OPPOSES":
                row["opposes_trade"] += 1
            if result == "WIN":
                row["wins_when_present"] += 1
            elif result == "LOSS":
                row["losses_when_present"] += 1

        tmp = PATTERN_AUDIT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, PATTERN_AUDIT_FILE)
    except Exception as exc:
        print(f"PATTERN AUDIT ERROR: {type(exc).__name__}: {exc}")


def last_green_red_volume(bars, i):
    """Return the latest GREEN and latest RED candle volumes at or before index i."""
    last_green = None
    last_red = None

    for j in range(i, -1, -1):
        color = candle_color(bars[j])
        if color == "GREEN" and last_green is None:
            last_green = bars[j]["v"]
        elif color == "RED" and last_red is None:
            last_red = bars[j]["v"]

        if last_green is not None and last_red is not None:
            break

    return last_green, last_red


def volume_flip_signal(bars, i):
    if i < 0:
        return None

    green_v, red_v = last_green_red_volume(bars, i)
    if green_v is None or red_v is None:
        return None

    # Keep the existing TEST 1 direction mapping; only the comparison method changes.
    # Compare the latest GREEN Volume with the latest RED Volume, regardless of
    # how many candles are between them.
    if green_v > red_v:
        return "SHORT"
    if red_v > green_v:
        return "LONG"
    return None


def volume_ok(bars, i, side):
    return volume_flip_signal(bars, i) == side


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
            and str(row.get("assetClass") or "").lower() == "crypto"
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


def aggregate_10m_bars(bars):
    grouped = {}
    for bar in bars:
        bucket = (int(bar["ts"]) // SIGNAL_MS) * SIGNAL_MS
        grouped.setdefault(bucket, []).append(bar)

    out = []
    for bucket in sorted(grouped):
        group = sorted(grouped[bucket], key=lambda x: x["ts"])
        if len(group) != 2:
            continue
        if int(group[0]["ts"]) != bucket:
            continue
        if int(group[1]["ts"]) != bucket + SOURCE_BAR_MS:
            continue
        out.append({
            "ts": bucket,
            "o": group[0]["o"],
            "h": max(x["h"] for x in group),
            "l": min(x["l"] for x in group),
            "c": group[-1]["c"],
            "v": sum(x["v"] for x in group),
        })
    return out


def aggregate_4h_uk_bars(bars):
    grouped = {}
    for bar in bars:
        dt_local = datetime.fromtimestamp(
            int(bar["ts"]) / 1000, tz=timezone.utc
        ).astimezone(UK_TZ)
        key = (dt_local.year, dt_local.month, dt_local.day, dt_local.hour // 4)
        grouped.setdefault(key, []).append(bar)

    out = []
    ordered_groups = sorted(
        grouped.values(), key=lambda group: min(int(x["ts"]) for x in group)
    )
    for raw_group in ordered_groups:
        group = sorted(raw_group, key=lambda x: int(x["ts"]))
        if any(
            int(group[i]["ts"]) != int(group[i - 1]["ts"]) + SOURCE_BAR_MS
            for i in range(1, len(group))
        ):
            continue

        close_ms = int(group[-1]["ts"]) + SOURCE_BAR_MS
        close_local = datetime.fromtimestamp(
            close_ms / 1000, tz=timezone.utc
        ).astimezone(UK_TZ)

        # Only completed UK-local 00/04/08/12/16/20 -> next-boundary candles.
        if close_local.minute != 0 or close_local.second != 0 or close_local.hour % 4 != 0:
            continue

        out.append({
            "ts": int(group[0]["ts"]),
            "close_ms": close_ms,
            "o": group[0]["o"],
            "h": max(x["h"] for x in group),
            "l": min(x["l"] for x in group),
            "c": group[-1]["c"],
            "v": sum(x["v"] for x in group),
        })
    return out


def bar_close_ms(bar):
    return int(bar.get("close_ms", int(bar["ts"]) + SIGNAL_MS))


def fetch_signal_bars(inst):
    raw = market_get(
        "/api/v1/market/candles",
        {"instId": inst, "bar": SIGNAL_BAR, "limit": "240"},
    )
    bars = parse_candles(raw)
    if SIGNAL_MINUTES == 10:
        bars = aggregate_10m_bars(bars)
    elif SIGNAL_MINUTES == 240:
        bars = aggregate_4h_uk_bars(bars)
    return decorate(bars)


def fetch_1h(inst):
    # Compatibility alias for older callers.
    return fetch_signal_bars(inst)


def build_signal_snapshot(inst, side, signal_close_ms, rank):
    """Capture the market/indicator state that existed at the signal close."""
    try:
        bars = fetch_signal_bars(inst)
        i = next(
            (
                idx
                for idx, bar in enumerate(bars)
                if bar_close_ms(bar) == int(signal_close_ms)
            ),
            None,
        )
        if i is None:
            return {
                "snapshot_error": "signal candle not found",
                "captured_ms": now_ms(),
                "rank": int(rank),
                "side": side,
                "signal_close_ms": int(signal_close_ms),
            }

        cur = bars[i]
        prev = bars[i - 1] if i >= 1 else None
        candle_range = float(cur["h"]) - float(cur["l"])
        body = abs(float(cur["c"]) - float(cur["o"]))
        close = float(cur["c"])

        def close_return_pct(lookback):
            if i < lookback:
                return None
            old_close = float(bars[i - lookback]["c"])
            if old_close <= 0:
                return None
            return (close / old_close - 1.0) * 100.0

        upper_wick = float(cur["h"]) - max(float(cur["o"]), float(cur["c"]))
        lower_wick = min(float(cur["o"]), float(cur["c"])) - float(cur["l"])

        metrics = (
            long_entry_metrics(bars, i)
            if side == "LONG"
            else short_entry_metrics(bars, i)
        ) or {}

        opposite_volume = (
            metrics.get("last_red_volume")
            if side == "LONG"
            else metrics.get("last_green_volume")
        )
        volume_ratio = None
        if opposite_volume not in (None, 0):
            volume_ratio = float(cur["v"]) / float(opposite_volume)

        prev_hist = (
            float(prev["macd_hist"])
            if prev and prev.get("macd_hist") is not None
            else None
        )
        cur_hist = (
            float(cur["macd_hist"])
            if cur.get("macd_hist") is not None
            else None
        )
        hist_delta_pct_close = None
        if prev_hist is not None and cur_hist is not None and close > 0:
            hist_delta_pct_close = ((cur_hist - prev_hist) / close) * 100.0

        snapshot = {
            "captured_ms": now_ms(),
            "rank": int(rank),
            "side": side,
            "signal_label": SIGNAL_LABEL,
            "signal_close_ms": int(signal_close_ms),
            "open": float(cur["o"]),
            "high": float(cur["h"]),
            "low": float(cur["l"]),
            "close": close,
            "volume": float(cur["v"]),
            "color": candle_color(cur),
            "range_pct_close": (
                candle_range / close * 100.0 if close > 0 else None
            ),
            "body_pct_range": (
                body / candle_range * 100.0 if candle_range > 0 else None
            ),
            "body_pct_close": body / close * 100.0 if close > 0 else None,
            "upper_wick_pct_range": (
                upper_wick / candle_range * 100.0 if candle_range > 0 else None
            ),
            "lower_wick_pct_range": (
                lower_wick / candle_range * 100.0 if candle_range > 0 else None
            ),
            "return_1bar_pct": close_return_pct(1),
            "return_2bar_pct": close_return_pct(2),
            "return_4bar_pct": close_return_pct(4),
            "return_8bar_pct": close_return_pct(8),
            "return_10bar_pct": close_return_pct(10),
            "return_20bar_pct": close_return_pct(20),
            "rsi14": (
                float(cur["rsi"]) if cur.get("rsi") is not None else None
            ),
            "macd_dif": (
                float(cur["macd_dif"])
                if cur.get("macd_dif") is not None
                else None
            ),
            "macd_dea": (
                float(cur["macd_dea"])
                if cur.get("macd_dea") is not None
                else None
            ),
            "macd_hist": cur_hist,
            "prev_macd_hist": prev_hist,
            "macd_hist_delta_pct_close": hist_delta_pct_close,
            "opposite_candle_volume": (
                float(opposite_volume) if opposite_volume is not None else None
            ),
            "volume_vs_opposite_ratio": volume_ratio,
            "filter_metrics": metrics,
            "chart_patterns": detect_chart_patterns(bars, i),
        }
        if prev:
            snapshot["prev_candle"] = {
                "open": float(prev["o"]),
                "high": float(prev["h"]),
                "low": float(prev["l"]),
                "close": float(prev["c"]),
                "volume": float(prev["v"]),
                "color": candle_color(prev),
            }
        return snapshot
    except Exception as exc:
        return {
            "snapshot_error": f"{type(exc).__name__}: {exc}",
            "captured_ms": now_ms(),
            "rank": int(rank),
            "side": side,
            "signal_close_ms": int(signal_close_ms),
        }


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
            last_cross_close = bar_close_ms(bars[i])
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
            bars = fetch_signal_bars(inst)
            if len(bars) < 40:
                continue
            data[inst] = bars
            latest_idx[inst] = len(bars) - 1
        except Exception as exc:
            print(f"{SIGNAL_LABEL} ERROR {inst}: {exc}")
            append_technical_event(
                "MARKET_DATA_ERROR",
                f"{type(exc).__name__}: {exc}",
                inst=inst,
                extra={"stage": "fetch_signal_bars"},
            )

    candidates = []
    scan_now_ms = now_ms()
    for inst, bars in data.items():
        i = latest_idx[inst]
        close_ms = bar_close_ms(bars[i])
        last_done = int(state.setdefault("last_processed_close_ms", {}).get(inst, 0) or 0)
        if close_ms <= last_done:
            continue

        side = None
        if short_entry_signal(bars, i):
            side = "SHORT"
        elif long_entry_signal(bars, i):
            side = "LONG"

        if side:
            pattern_filter = pattern_entry_filter(bars, i, side)
            if not pattern_filter["allowed"]:
                opposing_names = [
                    str(pattern.get("name") or "")
                    for pattern in pattern_filter["opposing"]
                ]
                message = (
                    f"{inst} {side} blocked by opposing chart pattern(s): "
                    + ", ".join(opposing_names)
                )
                print("PATTERN BLOCK: " + message)
                append_technical_event(
                    "PATTERN_BLOCK",
                    message,
                    inst=inst,
                    side=side,
                    rank=ranks[inst],
                    signal_close_ms=close_ms,
                    extra={
                        "opposing_patterns": pattern_filter["opposing"],
                        "all_patterns": pattern_filter["patterns"],
                    },
                )
            else:
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
        close_ms = bar_close_ms(bars[i])
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


def current_live_bankroll(state):
    """Starting bankroll compounded only by realized NET PnL since the bankroll reset."""
    starting_bankroll = d(state.get("bankroll_start_usdt", MAX_NOTIONAL_USDT))
    realized_net = d(state.get("realized_pnl_usdt", 0))
    pnl_baseline = d(state.get("bankroll_pnl_baseline_usdt", 0))
    bankroll = starting_bankroll + (realized_net - pnl_baseline)
    return max(Decimal("0"), bankroll)


def risk_stop_active(state):
    """Hard cumulative-loss stop: block all new entries at 10% drawdown."""
    if bool(state.get("risk_stop_triggered")):
        return True
    starting_bankroll = d(state.get("bankroll_start_usdt", MAX_NOTIONAL_USDT))
    if starting_bankroll <= 0:
        return True
    bankroll = current_live_bankroll(state)
    threshold = starting_bankroll * (Decimal("1") - MAX_DRAWDOWN_PCT)
    if bankroll <= threshold:
        state["risk_stop_triggered"] = True
        state["risk_stop_triggered_ms"] = now_ms()
        state["risk_stop_bankroll_usdt"] = clean_decimal(bankroll)
        state["risk_stop_threshold_usdt"] = clean_decimal(threshold)
        notify(
            f"HARD STOP: bankroll {bankroll:.4f} USDT <= {threshold:.4f} USDT "
            f"({MAX_DRAWDOWN_PCT * 100:.1f}% max drawdown). No new trades.",
            "BloFin LIVE HARD STOP",
        )
        return True
    return False


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


def place_sl_for_position(inst, side, sl):
    close_side = "sell" if side == "LONG" else "buy"
    client_id = ("livesl" + uuid.uuid4().hex)[:32]
    data = private_request(
        "POST",
        "/api/v1/trade/order-tpsl",
        body={
            "instId": inst,
            "marginMode": MARGIN_MODE,
            "positionSide": "net",
            "side": close_side,
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
        raise RuntimeError(f"SL rejected: {row}")
    return str(row.get("tpslId") or ""), client_id


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


def place_tp_for_position(inst, side, tp):
    close_side = "sell" if side == "LONG" else "buy"
    client_id = ("livetp" + uuid.uuid4().hex)[:32]
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
            "size": "-1",
            "reduceOnly": "true",
            "clientOrderId": client_id,
        },
    )
    row = data[0] if isinstance(data, list) and data else (data or {})
    if str(row.get("code", "0")) != "0":
        raise RuntimeError(f"TP rejected: {row}")
    return str(row.get("tpslId") or ""), client_id


def cancel_specific_tpsl(pos):
    tpsl_id = str(pos.get("tpsl_id") or "").strip()
    if not tpsl_id:
        return False
    data = private_request(
        "POST",
        "/api/v1/trade/cancel-tpsl",
        body=[{
            "instId": pos["inst"],
            "tpslId": tpsl_id,
            "clientOrderId": str(pos.get("tpsl_client_order_id") or ""),
        }],
    )
    row = data[0] if isinstance(data, list) and data else (data or {})
    if str(row.get("code", "0")) != "0":
        raise RuntimeError(f"TP/SL cancel rejected: {row}")
    return True


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

    patterns = (pos.get("signal_snapshot") or {}).get("chart_patterns") or []
    pattern_review = review_chart_patterns(patterns, str(pos.get("side") or ""), result)

    history = state.setdefault("trade_history", [])
    history.append({
        "inst": pos["inst"],
        "side": pos["side"],
        "result": result,
        "opened_ms": opened_ms,
        "closed_ms": closed_ms,
        "hold_minutes": round(hold_minutes, 2),
        "signal_close_ms": int(pos.get("signal_close_ms") or 0),
        "signal_age_ms": int(pos.get("signal_age_ms") or 0),
        "signal_rank": pos.get("signal_rank"),
        "strategy": str(pos.get("strategy") or ""),
        "code_commit": str(pos.get("code_commit") or ""),
        "open_price": str(hist.get("openAveragePrice") or pos.get("reference_entry") or ""),
        "close_price": str(hist.get("closeAveragePrice") or ""),
        "tp": str(pos.get("tp") or ""),
        "sl": str(pos.get("sl") or ""),
        "filled_size": str(pos.get("filled_size") or pos.get("size") or ""),
        "notional_usdt": str(pos.get("notional_usdt") or ""),
        "gross_pnl_usdt": float(gross_pnl),
        "fee_usdt": float(fee),
        "net_pnl_usdt": float(net_pnl),
        "reason": reason,
        "history_id": str(hist.get("historyId") or ""),
        "signal_snapshot": pos.get("signal_snapshot") or {},
        "pattern_review": pattern_review,
    })
    if len(history) > TRADE_HISTORY_LIMIT:
        del history[:-TRADE_HISTORY_LIMIT]

    append_pattern_audit(pos, result, closed_ms, net_pnl)

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


def cancel_tracked_tpsl(state):
    pos = state.get("position")
    if not pos:
        return False

    if pos.get("hold_policy") == "HOLD_60M":
        # HOLD60 positions keep broker-side TP +1% / SL -1% protection active.
        return False

    # Managed LIVE positions intentionally keep their broker-side TP/SL protection.
    if (
        pos.get("tp_policy") == "TP1"
        or pos.get("sl_policy") == "SL1"
        or str(pos.get("risk_profile") or "").endswith(("SL10", "SL1", "SL05"))
    ):
        return False

    tpsl_id = str(pos.get("tpsl_id") or "").strip()
    if not tpsl_id:
        pos["protection_status"] = "VOLUME_FLIP_ONLY"
        pos["tp"] = ""
        pos["sl"] = ""
        return False

    data = private_request(
        "POST",
        "/api/v1/trade/cancel-tpsl",
        body=[{
            "instId": pos["inst"],
            "tpslId": tpsl_id,
            "clientOrderId": str(pos.get("tpsl_client_order_id") or ""),
        }],
    )
    row = data[0] if isinstance(data, list) and data else (data or {})
    if str(row.get("code", "0")) != "0":
        print(f"TPSL CANCEL {pos['inst']}: {row}")
        return False

    pos["tpsl_id"] = ""
    pos["tpsl_client_order_id"] = ""
    pos["tp"] = ""
    pos["sl"] = ""
    pos["protection_status"] = "VOLUME_FLIP_ONLY"
    notify(
        f"{pos['side']} {pos['inst']} | old TP/SL cancelled | "
        "position now exits only on an opposite larger Volume colour flip.",
        "BloFin LIVE STRATEGY",
    )
    return True


def sync_tracked_position(state):
    open_positions = get_open_positions()
    tracked = state.get("position")
    if tracked:
        matching = [p for p in open_positions if p.get("instId") == tracked.get("inst")]
        if not matching:
            record_closed(state, "external close")
            return get_open_positions()
        cancel_tracked_tpsl(state)
    return open_positions


def evaluate_tracked_exit_signal(state, expected_close_ms=None):
    pos = state.get("position")
    if not isinstance(pos, dict) or not pos.get("inst"):
        return False

    if pos.get("hold_policy") != "HOLD_60M":
        return False

    opened_ms = int(pos.get("opened_ms") or 0)
    if opened_ms <= 0:
        return False

    elapsed_ms = now_ms() - opened_ms
    if elapsed_ms < HOLD_MS:
        return False

    close_tracked_position(
        state,
        f"{HOLD_MINUTES}m hold complete",
    )
    return True


def place_live_trade(state, candidate, tickers, instruments, cap_usdt=None, allocation_label=None):
    if risk_stop_active(state):
        raise RuntimeError("HARD STOP active: 10% drawdown limit reached")

    rank, inst, side, signal_close_ms = candidate
    signal_age_ms = now_ms() - int(signal_close_ms)
    signal_snapshot = build_signal_snapshot(
        inst, side, signal_close_ms, rank
    )
    if signal_age_ms < 0 or signal_age_ms > SIGNAL_MAX_AGE_MS:
        raise RuntimeError(
            f"{inst}: stale signal ({signal_age_ms / 1000:.1f}s old; "
            f"max {SIGNAL_MAX_AGE_MS / 1000:.0f}s)"
        )

    market_reference = d(tickers[inst]["last"])
    available = get_available_usdt()

    # HARD SAFETY CAP: MAX_NOTIONAL_USDT is the bankroll for this bot, not the
    # whole BloFin account. Existing tracked positions consume this bankroll.
    tracked_positions = get_tracked_positions(state)
    tracked_exposure = Decimal("0")
    for tracked_inst, tracked_pos in tracked_positions.items():
        try:
            tracked_notional = abs(d(tracked_pos.get("notional_usdt") or "0"))
        except Exception:
            raise RuntimeError(
                f"{tracked_inst}: tracked position notional is invalid; "
                "new LIVE entries are blocked for safety"
            )
        if tracked_notional <= 0:
            raise RuntimeError(
                f"{tracked_inst}: tracked position notional is missing; "
                "new LIVE entries are blocked for safety"
            )
        tracked_exposure += tracked_notional

    live_bankroll = current_live_bankroll(state)
    remaining_bankroll = live_bankroll - tracked_exposure
    if remaining_bankroll <= 0:
        raise RuntimeError(
            f"LIVE bankroll fully used: exposure {tracked_exposure:.4f} USDT "
            f">= current bankroll {live_bankroll:.4f} USDT"
        )

    if cap_usdt is None:
        cap = min(
            remaining_bankroll,
            available * POSITION_FRACTION,
        )
        allocation_label = allocation_label or "25% of available, within LIVE bankroll cap"
        risk_profile = "QUARTER_ACCOUNT_HOLD60"
        account_fraction = clean_decimal(POSITION_FRACTION)
    else:
        cap = min(d(cap_usdt), available, remaining_bankroll)
        allocation_label = allocation_label or "dynamic equal split within LIVE bankroll cap"
        risk_profile = "DYNAMIC_SPLIT_HOLD60"
        account_fraction = ""
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
        "signal_rank": int(rank),
        "signal_snapshot": signal_snapshot,
        "order_id": order_id,
        "client_order_id": client_id,
        "requested_reference": clean_decimal(market_reference),
        "reference_entry": "",
        "tp": "",
        "sl": "",
        "size": clean_decimal(size),
        "notional_usdt": clean_decimal(estimated_notional),
        "protection_status": "HOLD_60M_PENDING_FILL",
        "strategy": f"MACD_VOL_{side}_{SIGNAL_LABEL}",
        "code_commit": CODE_COMMIT,
        "risk_profile": risk_profile,
        "account_fraction": account_fraction,
        "allocation_label": allocation_label,
    }

    fill = wait_for_order_fill(inst, order_id, client_id)
    if not fill:
        state["position"]["protection_status"] = "ENTRY_PRICE_UNAVAILABLE"
        append_technical_event(
            "FILL_ERROR",
            "Market order accepted but fill price/size could not be confirmed.",
            inst=inst,
            side=side,
            rank=rank,
            signal_close_ms=signal_close_ms,
            extra={"order_id": order_id, "client_order_id": client_id},
        )
        notify(
            f"CRITICAL {side} {inst}: market order was accepted but actual fill price "
            "could not be confirmed. Closing the position for safety.",
            "BloFin LIVE SAFETY",
        )
        try:
            close_tracked_position(state, "SAFETY: no fill price")
        except Exception as exc:
            state["position"]["protection_error"] = str(exc)
            notify(
                f"CRITICAL {side} {inst}: automatic safety close also failed: {exc}",
                "BloFin LIVE SAFETY",
            )
        return

    fill_price = d(fill.get("averagePrice") or "0")
    filled_size = d(fill.get("filledSize") or size)
    contract_value = d(instruments[inst].get("contractValue") or "0")
    actual_notional = (
        filled_size * contract_value * fill_price
        if contract_value > 0
        else estimated_notional
    )
    tick = d(instruments[inst].get("tickSize") or "0.00000001")
    if side == "LONG":
        hard_tp = price_step(
            fill_price * (Decimal("1") + TAKE_PROFIT_PCT), tick, ROUND_CEILING
        )
        hard_sl = price_step(
            fill_price * (Decimal("1") - HARD_SL_PCT), tick, ROUND_FLOOR
        )
    else:
        hard_tp = price_step(
            fill_price * (Decimal("1") - TAKE_PROFIT_PCT), tick, ROUND_FLOOR
        )
        hard_sl = price_step(
            fill_price * (Decimal("1") + HARD_SL_PCT), tick, ROUND_CEILING
        )

    state["position"].update({
        "reference_entry": clean_decimal(fill_price),
        "entry_fee": str(fill.get("fee") or "0"),
        "filled_size": clean_decimal(filled_size),
        "notional_usdt": clean_decimal(actual_notional),
        "tp": clean_decimal(hard_tp),
        "sl": clean_decimal(hard_sl),
        "tp_policy": "TP1",
        "sl_policy": "SL1",
        "hold_policy": "HOLD_60M",
        "hold_minutes": HOLD_MINUTES,
        "exit_after_ms": opened_ms + HOLD_MS,
        "protection_status": "PLACING_TP1_SL1_HOLD60",
    })

    try:
        tpsl_id, tpsl_client_id = place_tpsl_for_position(
            inst, side, hard_tp, hard_sl
        )
    except Exception as exc:
        state["position"]["protection_status"] = "TP1_SL1_FAILED"
        state["position"]["protection_error"] = str(exc)
        append_technical_event(
            "PROTECTION_ERROR",
            f"{type(exc).__name__}: {exc}",
            inst=inst,
            side=side,
            rank=rank,
            signal_close_ms=signal_close_ms,
            extra={"stage": "place_tp_sl_hold60"},
        )
        notify(
            f"CRITICAL {side} {inst}: TP +1% / SL -1% could not be placed. "
            f"Closing position for safety. Error: {exc}",
            "BloFin LIVE SAFETY",
        )
        try:
            close_tracked_position(state, "SAFETY: TP1/SL1 placement failed")
        except Exception as close_exc:
            state["position"]["protection_error"] = (
                f"{exc}; safety close failed: {close_exc}"
            )
            notify(
                f"CRITICAL {side} {inst}: safety close failed too: {close_exc}",
                "BloFin LIVE SAFETY",
            )
        return

    state["position"]["tpsl_id"] = tpsl_id
    state["position"]["tpsl_client_order_id"] = tpsl_client_id
    state["position"]["protection_status"] = "TP1_SL1_HOLD60_ACTIVE"

    notify(
        f"OPEN {side} {inst} | 1x isolated | actual entry {fill_price} | "
        f"notional≈{actual_notional:.4f} USDT ({allocation_label}) | "
        f"TP {hard_tp} (+1%) | SL {hard_sl} (-1%) | "
        f"max hold {HOLD_MINUTES}m, then market close if still open | "
        f"signal age {signal_age_ms / 1000:.0f}s",
        "BloFin LIVE OPEN",
    )


def get_tracked_positions(state):
    positions = state.setdefault("positions", {})
    legacy = state.get("position")
    if isinstance(legacy, dict) and legacy.get("inst"):
        positions.setdefault(str(legacy["inst"]), legacy)
    state["position"] = None
    return positions


def _run_for_tracked_position(state, inst, func, *args):
    positions = get_tracked_positions(state)
    pos = positions.get(inst)
    if not pos:
        return None
    state["position"] = pos
    try:
        result = func(state, *args)
        updated = state.get("position")
        if isinstance(updated, dict) and updated.get("inst"):
            positions[inst] = updated
        else:
            positions.pop(inst, None)
        return result
    finally:
        state["position"] = None


def sync_all_tracked_positions(state):
    positions = get_tracked_positions(state)
    open_positions = get_open_positions()
    open_by_inst = {str(p.get("instId")): p for p in open_positions}

    for inst in list(positions):
        if inst not in open_by_inst:
            _run_for_tracked_position(state, inst, record_closed, "external close")
        else:
            _run_for_tracked_position(state, inst, cancel_tracked_tpsl)

    return get_open_positions()


def ensure_tp1_for_all_tracked_positions(state):
    positions = get_tracked_positions(state)
    if not positions:
        return []

    _, tickers, instruments = get_universe()
    open_by_inst = {
        str(row.get("instId")): row
        for row in get_open_positions()
        if row.get("instId")
    }
    updated = []

    for inst in list(positions):
        pos = positions.get(inst)
        if not isinstance(pos, dict):
            continue
        if (
            pos.get("hold_policy") == "HOLD_60M"
            and pos.get("tp_policy") == "TP1"
            and pos.get("sl_policy") == "SL1"
            and str(pos.get("tp") or "").strip()
            and str(pos.get("sl") or "").strip()
        ):
            continue
        if (
            pos.get("tp_policy") == "TP1"
            and pos.get("sl_policy") == "SL1"
            and str(pos.get("tp") or "").strip()
            and str(pos.get("sl") or "").strip()
        ):
            continue
        row = open_by_inst.get(inst)
        if not row:
            continue
        meta = instruments.get(inst)
        if not meta:
            print(f"TP1 MIGRATION {inst}: instrument metadata missing")
            continue

        entry = d(pos.get("reference_entry") or row.get("averagePrice") or "0")
        if entry <= 0:
            print(f"TP1 MIGRATION {inst}: entry price missing")
            continue
        tick = d(meta.get("tickSize") or "0.00000001")
        side = str(pos.get("side") or "")
        if side == "LONG":
            tp = price_step(
                entry * (Decimal("1") + TAKE_PROFIT_PCT), tick, ROUND_CEILING
            )
            sl = price_step(
                entry * (Decimal("1") - HARD_SL_PCT), tick, ROUND_FLOOR
            )
        elif side == "SHORT":
            tp = price_step(
                entry * (Decimal("1") - TAKE_PROFIT_PCT), tick, ROUND_FLOOR
            )
            sl = price_step(
                entry * (Decimal("1") + HARD_SL_PCT), tick, ROUND_CEILING
            )
        else:
            continue

        last = d((tickers.get(inst) or {}).get("last") or "0")
        tp_already_hit = (
            last > 0
            and (
                (side == "LONG" and last >= tp)
                or (side == "SHORT" and last <= tp)
            )
        )
        if tp_already_hit:
            notify(
                f"{side} {inst} | price already reached/passed TP +1% "
                f"(target {tp}, last {last}); closing now.",
                "BloFin LIVE TP1",
            )
            _run_for_tracked_position(
                state, inst, close_tracked_position, "TP +1% reached"
            )
            updated.append(inst)
            continue

        sl_already_hit = (
            last > 0
            and (
                (side == "LONG" and last <= sl)
                or (side == "SHORT" and last >= sl)
            )
        )
        if sl_already_hit:
            notify(
                f"{side} {inst} | price already reached/passed SL -1% "
                f"(target {sl}, last {last}); closing now.",
                "BloFin LIVE SL1",
            )
            _run_for_tracked_position(
                state, inst, close_tracked_position, "SL -1% reached"
            )
            updated.append(inst)
            continue

        old_id = str(pos.get("tpsl_id") or "").strip()
        if old_id:
            try:
                cancel_specific_tpsl(pos)
            except Exception as cancel_exc:
                print(
                    f"TP1 MIGRATION {inst}: old TP/SL cancel returned {cancel_exc}; "
                    "continuing with replacement because it may already be cancelled"
                )
            try:
                new_id, new_client = place_tpsl_for_position(inst, side, tp, sl)
            except Exception as exc:
                print(f"TP1 MIGRATION ERROR {inst}: {exc}")
                try:
                    restore_id, restore_client = place_sl_for_position(inst, side, sl)
                    pos["tpsl_id"] = restore_id
                    pos["tpsl_client_order_id"] = restore_client
                    pos["sl"] = clean_decimal(sl)
                    pos["protection_status"] = "SL1_ACTIVE"
                except Exception as restore_exc:
                    pos["protection_status"] = "PROTECTION_MIGRATION_FAILED"
                    pos["protection_error"] = f"{exc}; restore SL failed: {restore_exc}"
                    notify(
                        f"CRITICAL {side} {inst}: TP1 migration failed and SL restore failed: "
                        f"{restore_exc}",
                        "BloFin LIVE SAFETY",
                    )
                continue

            pos["tpsl_id"] = new_id
            pos["tpsl_client_order_id"] = new_client
        else:
            try:
                tp_id, tp_client = place_tp_for_position(inst, side, tp)
                pos["tp_tpsl_id"] = tp_id
                pos["tp_tpsl_client_order_id"] = tp_client
            except Exception as exc:
                print(f"TP1 MIGRATION ERROR {inst}: {exc}")
                continue

        pos["tp"] = clean_decimal(tp)
        pos["sl"] = clean_decimal(sl)
        pos["tp_policy"] = "TP1"
        pos["sl_policy"] = "SL1"
        old_profile = str(pos.get("risk_profile") or "")
        if old_profile:
            pos["risk_profile"] = (
                old_profile.replace("SL10", "SL1").replace("SL05", "SL1")
            )
        pos["protection_status"] = "TP1_SL1_ACTIVE"
        pos.pop("protection_error", None)
        updated.append(inst)
        notify(
            f"{side} {inst} | TP +1% active at {tp} | SL -1% active at {sl}",
            "BloFin LIVE TP1",
        )

    return updated


def evaluate_all_tracked_exit_signals(state, expected_close_ms=None):
    closed = []
    for inst in list(get_tracked_positions(state)):
        result = _run_for_tracked_position(
            state, inst, evaluate_tracked_exit_signal, expected_close_ms
        )
        if result:
            closed.append(inst)
    return closed


def place_live_trade_multi(
    state, candidate, tickers, instruments, cap_usdt, allocation_label
):
    positions = get_tracked_positions(state)
    state["position"] = None
    place_live_trade(
        state,
        candidate,
        tickers,
        instruments,
        cap_usdt=cap_usdt,
        allocation_label=allocation_label,
    )
    pos = state.get("position")
    if isinstance(pos, dict) and pos.get("inst"):
        positions[str(pos["inst"])] = pos
    state["position"] = None
    return pos


def execute_candidate_batch(state, candidates, tickers, instruments):
    if risk_stop_active(state):
        print("HARD STOP active: skipping all new entries.")
        for rank, inst, side, signal_close_ms in candidates:
            append_technical_event(
                "RISK_STOP",
                "Valid strategy signal blocked by the bot hard drawdown stop.",
                inst=inst,
                side=side,
                rank=rank,
                signal_close_ms=signal_close_ms,
            )
        return []

    positions = get_tracked_positions(state)
    open_positions = get_open_positions()
    account_open = {str(p.get("instId")) for p in open_positions if p.get("instId")}
    tracked = set(positions)
    untracked = sorted(account_open - tracked)
    if untracked:
        message = (
            "No new LIVE order: account has untracked open position(s): "
            + ", ".join(untracked[:5])
        )
        notify(message, "BloFin LIVE BLOCKED")
        for rank, inst, side, signal_close_ms in candidates:
            append_technical_event(
                "UNTRACKED_POSITION_BLOCK",
                message,
                inst=inst,
                side=side,
                rank=rank,
                signal_close_ms=signal_close_ms,
                extra={"untracked": untracked[:5]},
            )
        return []

    slots = max(0, MAX_OPEN_POSITIONS - len(account_open))
    if slots <= 0:
        for rank, inst, side, signal_close_ms in candidates:
            append_technical_event(
                "NO_SLOT",
                f"Valid signal not entered: all {MAX_OPEN_POSITIONS} position slots were occupied.",
                inst=inst,
                side=side,
                rank=rank,
                signal_close_ms=signal_close_ms,
                extra={"open_positions": len(account_open)},
            )
        return []

    selected = []
    skipped = []
    seen = set(account_open)
    for candidate in candidates:
        rank, inst, side, signal_close_ms = candidate
        inst = str(inst)
        if inst in seen:
            skipped.append((candidate, "ALREADY_OPEN"))
            continue
        if len(selected) >= slots:
            skipped.append((candidate, "NO_SLOT"))
            continue
        selected.append(candidate)
        seen.add(inst)

    for candidate, reason in skipped:
        rank, inst, side, signal_close_ms = candidate
        if reason == "ALREADY_OPEN":
            message = "Valid signal not entered: this instrument already has an open position."
        else:
            message = f"Valid signal not entered: only {slots} free position slot(s) were available."
        append_technical_event(
            reason,
            message,
            inst=inst,
            side=side,
            rank=rank,
            signal_close_ms=signal_close_ms,
            extra={"open_positions": len(account_open), "free_slots": slots},
        )

    if not selected:
        return []

    available = get_available_usdt()
    if available <= 0:
        notify("No available USDT for a new LIVE order.", "BloFin LIVE BLOCKED")
        for rank, inst, side, signal_close_ms in selected:
            append_technical_event(
                "NO_FUNDS",
                "Valid signal not entered: available USDT was zero or below.",
                inst=inst,
                side=side,
                rank=rank,
                signal_close_ms=signal_close_ms,
                extra={"available_usdt": str(available)},
            )
        return []

    tracked_exposure = Decimal("0")
    for tracked_inst, tracked_pos in positions.items():
        try:
            tracked_notional = abs(d(tracked_pos.get("notional_usdt") or "0"))
        except Exception as exc:
            message = f"{tracked_inst} has invalid tracked notional: {exc}"
            notify("No new LIVE order: " + message, "BloFin LIVE BLOCKED")
            for rank, inst, side, signal_close_ms in selected:
                append_technical_event(
                    "TRACKING_ERROR",
                    message,
                    inst=inst,
                    side=side,
                    rank=rank,
                    signal_close_ms=signal_close_ms,
                )
            return []
        if tracked_notional <= 0:
            message = f"{tracked_inst} has missing tracked notional."
            notify("No new LIVE order: " + message, "BloFin LIVE BLOCKED")
            for rank, inst, side, signal_close_ms in selected:
                append_technical_event(
                    "TRACKING_ERROR",
                    message,
                    inst=inst,
                    side=side,
                    rank=rank,
                    signal_close_ms=signal_close_ms,
                )
            return []
        tracked_exposure += tracked_notional

    live_bankroll = current_live_bankroll(state)
    remaining_bankroll = live_bankroll - tracked_exposure
    if remaining_bankroll <= 0:
        message = (
            f"Current bankroll {live_bankroll:.4f} USDT is already used by "
            f"{tracked_exposure:.4f} USDT exposure."
        )
        notify("No new LIVE order: " + message, "BloFin LIVE BLOCKED")
        for rank, inst, side, signal_close_ms in selected:
            append_technical_event(
                "BANKROLL_BLOCK",
                message,
                inst=inst,
                side=side,
                rank=rank,
                signal_close_ms=signal_close_ms,
                extra={
                    "live_bankroll_usdt": str(live_bankroll),
                    "tracked_exposure_usdt": str(tracked_exposure),
                },
            )
        return []

    batch_budget = min(available, remaining_bankroll)
    per_trade_cap = batch_budget / Decimal(len(selected))
    allocation_label = (
        f"1/{len(selected)} of remaining LIVE bankroll; "
        f"current bankroll {live_bankroll:.4f} USDT; max {MAX_OPEN_POSITIONS} positions"
    )
    executed = []
    for candidate in selected:
        rank, inst, side, signal_close_ms = candidate
        try:
            pos = place_live_trade_multi(
                state,
                candidate,
                tickers,
                instruments,
                per_trade_cap,
                allocation_label,
            )
            if pos:
                executed.append({
                    "inst": inst,
                    "side": side,
                    "rank": int(rank),
                    "signal_close_ms": int(signal_close_ms),
                    "notional_usdt": pos.get("notional_usdt"),
                })
        except Exception as exc:
            event_type = classify_entry_error(exc)
            append_technical_event(
                event_type,
                f"{type(exc).__name__}: {exc}",
                inst=inst,
                side=side,
                rank=rank,
                signal_close_ms=signal_close_ms,
                extra={
                    "stage": "entry",
                    "per_trade_cap_usdt": str(per_trade_cap),
                    "available_usdt": str(available),
                },
            )
            print(f"LIVE ENTRY ERROR {inst}: {type(exc).__name__}: {exc}")
            notify(
                f"{inst} {side}: entry failed: {exc}",
                "BloFin LIVE ENTRY ERROR",
            )
    return executed


def status(state, top10):
    available = get_available_usdt()
    positions = get_tracked_positions(state)
    if positions:
        parts = []
        for pos in list(positions.values())[:MAX_OPEN_POSITIONS]:
            age_min = max(
                0.0, (now_ms() - int(pos.get("opened_ms") or now_ms())) / 60000.0
            )
            parts.append(f"{pos.get('side')} {pos.get('inst')} {age_min:.0f}m")
        pos_text = f"{len(positions)}/{MAX_OPEN_POSITIONS} open: " + ", ".join(parts)
    else:
        pos_text = f"0/{MAX_OPEN_POSITIONS} open"
    t = state.get("trades", {})
    live_bankroll = current_live_bankroll(state)
    notify(
        f"LIVE status | bankroll {live_bankroll:.4f} USDT | available {available:.4f} USDT | "
        f"NET realized {d(state.get('realized_pnl_usdt', 0)):+.4f} | "
        f"fees {d(state.get('fees_usdt', 0)):.4f} | trades {t.get('total', 0)} "
        f"(W{t.get('wins', 0)}/L{t.get('losses', 0)}/F{t.get('flat', 0)}/U{t.get('unverified', 0)}) | "
        f"{pos_text} | TOP3: {', '.join(top10[:3]) if top10 else 'none'}",
        f"BloFin LIVE {SIGNAL_LABEL}",
    )


def main():
    require_live_enabled()
    require_account_modes()
    state = load_state()
    top10, tickers, instruments = get_universe()
    if not top10:
        raise RuntimeError("BloFin TOP10 is empty")

    sync_all_tracked_positions(state)
    evaluate_all_tracked_exit_signals(state)

    if risk_stop_active(state):
        candidates = []
        executed = []
    else:
        candidates = evaluate_signals(state, top10)
        executed = execute_candidate_batch(state, candidates, tickers, instruments)

    status(state, top10)
    state["last_run_ms"] = now_ms()
    state["last_top10"] = top10
    save_state(state)
    print(json.dumps({
        "positions": get_tracked_positions(state),
        "executed": executed,
        "trades": state.get("trades"),
        "top10": top10,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
