#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from blofin_alert_15m import top_usdt_swaps, analyse_candidate

BASE = "https://openapi.blofin.com"
STATE_PATH = Path("blofin_bot_state.json")
NOTIFY_PATH = Path("trade_notify.md")
TRADE_LOG_DIR = Path("blofin_live_trades")
START_CAPITAL = Decimal("10")
LEVERAGE = Decimal("1")
TP_PCT = Decimal("0.5")
SL_PCT = Decimal("0.5")
MIN_TARGET_PCT = Decimal("0.5")

API_KEY = os.environ.get("BLOFIN_API_KEY", "").strip()
SECRET_KEY = os.environ.get("BLOFIN_SECRET_KEY", "").strip()
PASSPHRASE = os.environ.get("BLOFIN_PASSPHRASE", "").strip()

EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME", "").strip()
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "").strip()
EMAIL_SMTP_HOST = os.environ.get("EMAIL_SMTP_HOST", "").strip()
EMAIL_SMTP_PORT = os.environ.get("EMAIL_SMTP_PORT", "").strip()


def D(x, default="0"):
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def load_state():
    if not STATE_PATH.exists():
        return {
            "capital_usdt": str(START_CAPITAL),
            "active": None,
            "last_trade_signal": None,
            "trades": 0,
        }
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("capital_usdt", str(START_CAPITAL))
    s.setdefault("active", None)
    s.setdefault("last_trade_signal", None)
    s.setdefault("trades", 0)
    return s


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def notify(text):
    NOTIFY_PATH.write_text("@Photofiver\n\n" + text.strip() + "\n", encoding="utf-8")


def send_email(subject, body):
    try:
        if not (EMAIL_USERNAME and EMAIL_PASSWORD and EMAIL_TO):
            print("EMAIL_DISABLED missing EMAIL_USERNAME/EMAIL_PASSWORD/EMAIL_TO")
            return False

        host = EMAIL_SMTP_HOST
        if not host:
            if EMAIL_USERNAME.lower().endswith("@gmail.com"):
                host = "smtp.gmail.com"
            else:
                print("EMAIL_DISABLED missing EMAIL_SMTP_HOST")
                return False

        if EMAIL_SMTP_PORT:
            port = int(EMAIL_SMTP_PORT)
        else:
            port = 465 if host == "smtp.gmail.com" else 587

        msg = EmailMessage()
        msg["From"] = EMAIL_USERNAME
        msg["To"] = EMAIL_TO
        msg["Subject"] = subject
        msg.set_content(body)

        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                smtp.login(EMAIL_USERNAME, EMAIL_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(EMAIL_USERNAME, EMAIL_PASSWORD)
                smtp.send_message(msg)

        print(f"EMAIL_SENT {subject}")
        return True
    except Exception as exc:
        print(f"EMAIL_ERROR {type(exc).__name__}: {exc}")
        return False


def iso_time(ms, tz):
    return datetime.fromtimestamp(int(ms) / 1000, tz).isoformat()


def write_trade_log(state, active, hist, realized, fee, funding, net, new_cap, end_ms):
    TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    opened_ms = int(active["opened_at"])
    trade_no = int(active.get("trade_number") or state.get("trades") or 0)
    stamp = datetime.fromtimestamp(opened_ms / 1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inst = str(active["inst"]).replace("/", "-")
    path = TRADE_LOG_DIR / f"{trade_no:06d}_{stamp}_{inst}_{active['direction']}.json"
    log = {
        "trade_number": trade_no,
        "status": "closed",
        "instrument": active["inst"],
        "direction": active["direction"],
        "order_id": active.get("order_id"),
        "client_order_id": active.get("client_order_id"),
        "position_id": active.get("position_id"),
        "opened_at_ms": opened_ms,
        "opened_at_utc": iso_time(opened_ms, timezone.utc),
        "opened_at_uk": iso_time(opened_ms, ZoneInfo("Europe/London")),
        "closed_at_ms": int(end_ms),
        "closed_at_utc": iso_time(end_ms, timezone.utc),
        "closed_at_uk": iso_time(end_ms, ZoneInfo("Europe/London")),
        "capital_before_usdt": active.get("capital_before"),
        "capital_after_usdt": plain(new_cap),
        "realized_pnl_usdt": plain(realized),
        "fee_usdt": plain(fee),
        "funding_usdt": plain(funding),
        "net_pnl_usdt": plain(net),
        "contracts": active.get("contracts"),
        "notional_usdt": active.get("notional_usdt"),
        "reference_entry_price": active.get("reference_price"),
        "tp_price": active.get("tp"),
        "sl_price": active.get("sl"),
        "tp_pct": plain(TP_PCT),
        "sl_pct": plain(SL_PCT),
        "signal_snapshot": active.get("signal_snapshot", {}),
        "blofin_position_history": hist,
    }
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def public_get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "quickprice-live-trader/1.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def auth_headers(method, request_path, body_str=""):
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    prehash = f"{request_path}{method.upper()}{ts}{nonce}{body_str}"
    hex_sig = hmac.new(
        SECRET_KEY.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().encode("utf-8")
    sign = base64.b64encode(hex_sig).decode("utf-8")
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "User-Agent": "quickprice-live-trader/1.0",
    }


def private_request(method, path, params=None, body=None):
    request_path = path
    if params:
        request_path += "?" + urllib.parse.urlencode(params)
    body_str = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    req = urllib.request.Request(
        BASE + request_path,
        data=body_str.encode("utf-8") if body is not None else None,
        headers=auth_headers(method, request_path, body_str),
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"BloFin HTTP {exc.code}: {detail[:500]}")
    if str(data.get("code")) != "0":
        raise RuntimeError(f"BloFin API {data.get('code')}: {data.get('msg')}")
    inner = data.get("data")
    if isinstance(inner, dict) and "code" in inner and str(inner.get("code")) not in ("0", "None", ""):
        raise RuntimeError(f"BloFin order {inner.get('code')}: {inner.get('msg')}")
    return data


def best_signal():
    candidates = []
    for row in top_usdt_swaps():
        try:
            x = analyse_candidate(row["inst"])
            if x and x["passed"]:
                x["change24"] = row["change24"]
                candidates.append(x)
        except Exception:
            continue
    if not candidates:
        return None
    def rank(x):
        return (
            float(x["hit_rate"] or 0),
            float(x["target_pct"] or 0),
            float(x["change24"]),
        )
    candidates.sort(key=rank, reverse=True)
    return candidates[0]


def instrument(inst):
    data = public_get("/api/v1/market/instruments", {"instId": inst}).get("data", [])
    if not data:
        raise RuntimeError(f"Brak danych instrumentu {inst}")
    return data[0]


def last_price(inst):
    rows = public_get("/api/v1/market/tickers", {"instId": inst}).get("data", [])
    if not rows:
        raise RuntimeError(f"Brak ceny {inst}")
    return D(rows[0].get("last"))


def step_floor(value, step):
    value, step = D(value), D(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def step_ceil(value, step):
    value, step = D(value), D(step)
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def plain(d):
    s = format(D(d), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def open_positions():
    return private_request("GET", "/api/v1/account/positions").get("data", [])


def position_mode():
    d = private_request("GET", "/api/v1/account/position-mode").get("data", {})
    return d.get("positionMode", "net_mode"), str(d.get("multiPosition", "false")).lower() == "true"


def margin_mode():
    d = private_request("GET", "/api/v1/account/margin-mode").get("data", {})
    mode = d.get("marginMode")
    if mode not in ("cross", "isolated"):
        raise RuntimeError("Nie można odczytać margin mode z BloFin")
    return mode


def available_usdt():
    d = private_request("GET", "/api/v1/account/balance").get("data", {})
    for row in d.get("details", []):
        if row.get("currency") == "USDT":
            return D(row.get("available") or row.get("availableEquity") or "0")
    return Decimal("0")


def funding_between(inst, begin_ms, end_ms):
    try:
        rows = private_request(
            "GET",
            "/api/v1/account/funding-fees",
            {"begin": str(begin_ms), "end": str(end_ms), "instId": inst, "limit": "100"},
        ).get("data", [])
        return sum((D(x.get("fundingFee")) for x in rows), Decimal("0"))
    except Exception:
        return Decimal("0")


def find_closed_history(active):
    params = {
        "instId": active["inst"],
        "begin": str(max(0, int(active["opened_at"]) - 60000)),
        "end": str(int(time.time() * 1000)),
        "limit": "100",
    }
    if active.get("position_id"):
        params["positionId"] = active["position_id"]
    rows = private_request("GET", "/api/v1/account/positions-history", params).get("data", [])
    if active.get("position_id"):
        rows = [r for r in rows if str(r.get("positionId", "")) == str(active["position_id"])]
    rows = [r for r in rows if int(r.get("createTime") or 0) >= int(active["opened_at"]) - 60000]
    rows.sort(key=lambda r: int(r.get("updateTime") or 0), reverse=True)
    for r in rows:
        if r.get("state") in (None, "", "closed") or D(r.get("closePositions")) > 0:
            return r
    return rows[0] if rows else None


def reconcile(state):
    active = state.get("active")
    if not active:
        return False

    positions = open_positions()
    matched = []
    for p in positions:
        if p.get("instId") != active["inst"] or D(p.get("positions")) == 0:
            continue
        if active.get("position_id") and str(p.get("positionId")) != str(active["position_id"]):
            continue
        matched.append(p)

    if matched:
        p = matched[0]
        if not active.get("position_id") and p.get("positionId"):
            active["position_id"] = str(p.get("positionId"))
            state["active"] = active
            save_state(state)
        print(f"ACTIVE {active['direction']} {active['inst']} capital={state['capital_usdt']} USDT")
        return True

    hist = find_closed_history(active)
    if not hist:
        # Market order may still be settling. Do not open another trade.
        print(f"WAIT_SETTLEMENT {active['inst']}")
        return True

    realized = D(hist.get("realizedPnl"))
    fee = abs(D(hist.get("fee")))
    end_ms = int(hist.get("updateTime") or int(time.time() * 1000))
    funding = funding_between(active["inst"], int(active["opened_at"]), end_ms)
    net = realized - fee + funding
    old_cap = D(state.get("capital_usdt", START_CAPITAL))
    new_cap = max(Decimal("0"), old_cap + net)
    state["capital_usdt"] = plain(new_cap)
    state["active"] = None
    state["last_result"] = {
        "inst": active["inst"],
        "direction": active["direction"],
        "realized_pnl": plain(realized),
        "fee": plain(fee),
        "funding": plain(funding),
        "net": plain(net),
        "capital_after": plain(new_cap),
        "closed_at": end_ms,
    }
    log_path = write_trade_log(state, active, hist, realized, fee, funding, net, new_cap, end_ms)
    save_state(state)
    close_body = (
        f"POZYCJA ZAMKNIĘTA\n\n"
        f"Transakcja: #{active.get('trade_number', state.get('trades', 0))}\n"
        f"Instrument: {active['inst']}\n"
        f"Kierunek: {active['direction']}\n"
        f"Otwarcie UK: {iso_time(active['opened_at'], ZoneInfo('Europe/London'))}\n"
        f"Zamknięcie UK: {iso_time(end_ms, ZoneInfo('Europe/London'))}\n"
        f"Kapitał przed: {active.get('capital_before')} USDT\n"
        f"Realized PnL: {plain(realized)} USDT\n"
        f"Prowizja: {plain(fee)} USDT\n"
        f"Funding: {plain(funding)} USDT\n"
        f"Wynik netto: {plain(net)} USDT\n"
        f"Kapitał po rolowaniu: {plain(new_cap)} USDT\n"
        f"TP: {active.get('tp')}\n"
        f"SL: {active.get('sl')}\n"
        f"Order ID: {active.get('order_id')}\n"
        f"Log: {log_path}"
    )
    notify(
        "## POZYCJA ZAMKNIĘTA\n\n"
        f"**{active['direction']} {active['inst']}**\n\n"
        f"Wynik netto: **{plain(net)} USDT**\n"
        f"Kapitał bota po rolowaniu: **{plain(new_cap)} USDT**"
    )
    send_email(
        f"BloFin CLOSED #{active.get('trade_number', state.get('trades', 0))} {active['direction']} {active['inst']} | {plain(net)} USDT",
        close_body,
    )
    print(f"CLOSED {active['inst']} net={plain(net)} capital={plain(new_cap)} log={log_path}")
    return False


def make_order_plan(sig, capital):
    inst = sig["inst"]
    meta = instrument(inst)
    price = last_price(inst)
    contract_value = D(meta.get("contractValue"))
    lot = D(meta.get("lotSize") or "1")
    minimum = D(meta.get("minSize") or lot)
    tick = D(meta.get("tickSize") or meta.get("priceTick") or "0.00000001")
    if price <= 0 or contract_value <= 0:
        raise RuntimeError("Nieprawidłowa cena lub contractValue")

    target = D(sig["target_price"])
    if sig["direction"] == "LONG":
        target_pct_now = (target / price - 1) * 100
        tp = step_ceil(price * (Decimal("1") + TP_PCT / 100), tick)
        sl = step_ceil(price * (Decimal("1") - SL_PCT / 100), tick)
        order_side = "buy"
    else:
        target_pct_now = (price / target - 1) * 100
        tp = step_floor(price * (Decimal("1") - TP_PCT / 100), tick)
        sl = step_floor(price * (Decimal("1") + SL_PCT / 100), tick)
        order_side = "sell"

    if target_pct_now < MIN_TARGET_PCT:
        raise RuntimeError(f"Cel oddalił się/zbliżył: teraz tylko {target_pct_now:.3f}%")

    contracts = step_floor(capital * LEVERAGE / (price * contract_value), lot)
    if contracts < minimum:
        raise RuntimeError(
            f"10/rolowany kapitał jest za mały dla minimum {plain(minimum)} kontraktów {inst}"
        )

    notional = contracts * contract_value * price
    return {
        "price": price,
        "tp": tp,
        "sl": sl,
        "contracts": contracts,
        "notional": notional,
        "order_side": order_side,
        "meta": meta,
        "target_pct_now": target_pct_now,
    }


def dry_run():
    sig = best_signal()
    state = load_state()
    cap = D(state.get("capital_usdt", START_CAPITAL))
    if not sig:
        print("DRY_RUN_OK no qualified WEJŚCIE now")
        return
    plan = make_order_plan(sig, cap)
    print(json.dumps({
        "dry_run": True,
        "capital_usdt": plain(cap),
        "signal": sig,
        "contracts": plain(plan["contracts"]),
        "notional_usdt": plain(plan["notional"]),
        "tp": plain(plan["tp"]),
        "sl": plain(plan["sl"]),
    }, ensure_ascii=False, indent=2))


def live_run():
    if NOTIFY_PATH.exists():
        NOTIFY_PATH.unlink()
    state = load_state()
    save_state(state)

    if not (API_KEY and SECRET_KEY and PASSPHRASE):
        print("LIVE_DISABLED missing BLOFIN_API_KEY/BLOFIN_SECRET_KEY/BLOFIN_PASSPHRASE")
        return

    # First settle the previous bot trade. Never overlap bot positions.
    if reconcile(state):
        return

    capital = D(state.get("capital_usdt", START_CAPITAL))
    if capital <= 0:
        notify("## BOT ZATRZYMANY\n\nKapitał bota spadł do **0 USDT**.")
        return

    # Safety: never mix this 10-USDT system with any position already open on the account.
    existing = [p for p in open_positions() if D(p.get("positions")) != 0]
    if existing:
        print("SKIP account already has an open futures position")
        return

    sig = best_signal()
    if not sig:
        print("NO_ENTRY no qualified signal")
        return

    signal_id = f"{sig['inst']}:{sig['bar_time']}:{sig['direction']}"
    if state.get("last_trade_signal") == signal_id:
        print("DUPLICATE_SIGNAL skipped")
        return

    mode, multi = position_mode()
    if multi:
        # Opening a new multi-position is supported by BloFin, but this bot keeps one bot position at a time.
        pass
    pos_side = "net" if mode == "net_mode" else sig["direction"].lower()
    mm = margin_mode()
    plan = make_order_plan(sig, capital)

    available = available_usdt()
    if available < capital:
        raise RuntimeError(
            f"Za mało dostępnych środków na BloFin: {plain(available)} USDT, bot potrzebuje {plain(capital)} USDT"
        )

    private_request("POST", "/api/v1/account/set-leverage", body={
        "instId": sig["inst"],
        "leverage": plain(LEVERAGE),
        "marginMode": mm,
        "positionSide": pos_side,
    })

    client_id = ("QPS" + str(int(time.time() * 1000)))[:32]
    order_body = {
        "instId": sig["inst"],
        "marginMode": mm,
        "positionSide": pos_side,
        "side": plan["order_side"],
        "orderType": "market",
        "size": plain(plan["contracts"]),
        "clientOrderId": client_id,
        "tpTriggerPrice": plain(plan["tp"]),
        "tpOrderPrice": "-1",
        "tpTriggerPriceType": "last",
        "slTriggerPrice": plain(plan["sl"]),
        "slOrderPrice": "-1",
        "slTriggerPriceType": "last",
    }
    result = private_request("POST", "/api/v1/trade/order", body=order_body)
    data = result.get("data") or {}
    order_id = str(data.get("orderId") or "")
    if not order_id:
        raise RuntimeError(f"BloFin nie zwrócił orderId: {result}")

    time.sleep(1.5)
    position_id = ""
    try:
        detail = private_request(
            "GET", "/api/v1/trade/order-detail",
            {"instId": sig["inst"], "orderId": order_id},
        ).get("data") or {}
        position_id = str(detail.get("positionId") or "")
    except Exception:
        pass

    trade_number = int(state.get("trades", 0)) + 1
    state["active"] = {
        "trade_number": trade_number,
        "inst": sig["inst"],
        "direction": sig["direction"],
        "order_id": order_id,
        "client_order_id": client_id,
        "position_id": position_id,
        "opened_at": int(time.time() * 1000),
        "capital_before": plain(capital),
        "contracts": plain(plan["contracts"]),
        "notional_usdt": plain(plan["notional"]),
        "reference_price": plain(plan["price"]),
        "tp": plain(plan["tp"]),
        "sl": plain(plan["sl"]),
        "hit_rate": sig["hit_rate"],
        "target_pct": sig["target_pct"],
        "bar_time": sig["bar_time"],
        "signal_snapshot": sig,
    }
    state["last_trade_signal"] = signal_id
    state["trades"] = trade_number
    save_state(state)

    opened_ms = int(state["active"]["opened_at"])
    open_body = (
        f"NOWA POZYCJA BLOFIN\n\n"
        f"Transakcja: #{trade_number}\n"
        f"Instrument: {sig['inst']}\n"
        f"Kierunek: {sig['direction']}\n"
        f"Otwarcie UK: {iso_time(opened_ms, ZoneInfo('Europe/London'))}\n"
        f"Kapitał użyty: {plain(capital)} USDT\n"
        f"Lewar: {plain(LEVERAGE)}x\n"
        f"Wielkość: {plain(plan['contracts'])} kontraktów\n"
        f"Nominał: {plain(plan['notional'])} USDT\n"
        f"Cena referencyjna: {plain(plan['price'])}\n"
        f"TP: {plain(plan['tp'])} (+{plain(TP_PCT)}%)\n"
        f"SL: {plain(plan['sl'])} (-{plain(SL_PCT)}%)\n"
        f"Historyczna skuteczność sygnału: {sig['hit_rate']:.1f}%\n"
        f"Order ID: {order_id}\n"
        f"Stochastic K/D: {sig.get('stoch_k_8', sig.get('k')):.2f} / {sig.get('stoch_d_3', sig.get('d')):.2f}\n"
        f"ATR14: {sig.get('atr14')}\n"
        f"Zmiana 8 świec: {sig.get('change8_pct', sig.get('change8')):.3f}%\n"
        f"Zmiana 24h: {sig.get('change24'):.3f}%\n"
        f"Support: {sig.get('support')}\n"
        f"Resistance: {sig.get('resistance')}"
    )
    notify(
        "## AUTO WEJŚCIE BLOFIN\n\n"
        f"**{sig['direction']} {sig['inst']}**\n\n"
        f"Kapitał użyty: **{plain(capital)} USDT** (1×)\n"
        f"Wielkość: **{plain(plan['contracts'])} kontraktów** ≈ **{plain(plan['notional'])} USDT**\n"
        f"TP: **{plain(plan['tp'])}** (+{plain(TP_PCT)}%)\n"
        f"SL: **{plain(plan['sl'])}** (-{plain(SL_PCT)}%)\n"
        f"Historyczna skuteczność sygnału: **{sig['hit_rate']:.1f}%**\n\n"
        "Po zamknięciu pozycji zysk albo strata netto zostanie dodana do/odjęta od kapitału następnej transakcji."
    )
    send_email(
        f"BloFin OPEN #{trade_number} {sig['direction']} {sig['inst']}",
        open_body,
    )
    print(f"OPENED {sig['direction']} {sig['inst']} order={order_id} capital={plain(capital)}")


def summary_12h():
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 12 * 60 * 60 * 1000
    logs = []

    if TRADE_LOG_DIR.exists():
        for path in sorted(TRADE_LOG_DIR.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            closed_ms = int(row.get("closed_at_ms") or 0)
            if start_ms <= closed_ms <= now_ms:
                row["_path"] = str(path)
                logs.append(row)

    logs.sort(key=lambda x: int(x.get("closed_at_ms") or 0))
    wins = sum(1 for x in logs if D(x.get("net_pnl_usdt")) > 0)
    losses = sum(1 for x in logs if D(x.get("net_pnl_usdt")) < 0)
    flat = len(logs) - wins - losses
    net_total = sum((D(x.get("net_pnl_usdt")) for x in logs), Decimal("0"))
    fees_total = sum((D(x.get("fee_usdt")) for x in logs), Decimal("0"))
    funding_total = sum((D(x.get("funding_usdt")) for x in logs), Decimal("0"))

    state = load_state()
    cap_now = D(state.get("capital_usdt", START_CAPITAL))
    active = state.get("active")
    now_uk = datetime.now(ZoneInfo("Europe/London"))
    start_uk = datetime.fromtimestamp(start_ms / 1000, ZoneInfo("Europe/London"))

    lines = [
        "BLOFIN — PODSUMOWANIE 12 GODZIN",
        "",
        f"Okres: {start_uk:%Y-%m-%d %H:%M} -> {now_uk:%Y-%m-%d %H:%M} UK",
        f"Zamknięte transakcje: {len(logs)}",
        f"Wygrane: {wins}",
        f"Stratne: {losses}",
        f"Na zero: {flat}",
        f"Wynik netto łącznie: {plain(net_total)} USDT",
        f"Prowizje łącznie: {plain(fees_total)} USDT",
        f"Funding łącznie: {plain(funding_total)} USDT",
        f"Aktualny kapitał bota: {plain(cap_now)} USDT",
    ]

    if active:
        lines.extend([
            "",
            "AKTYWNA POZYCJA:",
            f"#{active.get('trade_number')} {active.get('direction')} {active.get('inst')}",
            f"Otwarto UK: {iso_time(active.get('opened_at'), ZoneInfo('Europe/London'))}",
            f"Kapitał użyty: {active.get('capital_before')} USDT",
            f"TP: {active.get('tp')}",
            f"SL: {active.get('sl')}",
        ])
    else:
        lines.extend(["", "Aktywna pozycja: brak"])

    if logs:
        lines.extend(["", "ZAMKNIĘTE TRANSAKCJE:"])
        for row in logs:
            lines.append(
                f"#{row.get('trade_number')} {row.get('direction')} {row.get('instrument')} | "
                f"netto {row.get('net_pnl_usdt')} USDT | "
                f"{row.get('closed_at_uk')}"
            )

    body = "\n".join(lines)
    send_email(
        f"BloFin 12h summary | {len(logs)} trades | {plain(net_total)} USDT",
        body,
    )
    print(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary-12h", action="store_true")
    args = parser.parse_args()
    try:
        if args.dry_run:
            dry_run()
        elif args.summary_12h:
            summary_12h()
        else:
            live_run()
    except Exception as exc:
        notify("## BŁĄD AUTO-TRADE\n\n" + str(exc))
        print("ERROR", repr(exc))
        raise


if __name__ == "__main__":
    main()
