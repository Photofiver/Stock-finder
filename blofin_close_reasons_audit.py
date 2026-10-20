"""READ-ONLY BloFin closing-order audit, independent of the LIVE trader.

Only GET /api/v1/trade/orders-history is used. Does not place, cancel,
amend orders, change TP/SL, or write blofin_live_state.json.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import base64
import hashlib
import hmac
import time
import uuid
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

REPO = os.environ.get("GITHUB_REPOSITORY", "Photofiver/Stock-finder")
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
BLOFIN_KEY = os.environ.get("BLOFIN_API_KEY", "").strip()
BLOFIN_SECRET = os.environ.get("BLOFIN_SECRET_KEY", "").strip()
BLOFIN_PASSPHRASE = os.environ.get("BLOFIN_PASSPHRASE", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def _request_json(url, method="GET", data=None, headers=None):
    req = Request(url, data=data, method=method, headers=headers or {})
    with urlopen(req, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def _gh_headers(raw=False):
    return {
        "Authorization": "Bearer " + GH_TOKEN,
        "Accept": ("application/vnd.github.raw+json" if raw
                   else "application/vnd.github+json"),
        "User-Agent": "BloFinCloseReasonAudit/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_url(path):
    return ("https://api.github.com/repos/" + REPO +
            "/contents/" + quote(path, safe="/"))


def load_remote(path):
    # GitHub raw media type handles large (>1 MB) trading state files.
    req = Request(_gh_url(path) + "?ref=main", headers=_gh_headers(True))
    with urlopen(req, timeout=45) as response:
        body = response.read()
    doc = json.loads(body.decode("utf-8"))
    if isinstance(doc, dict) and "content" in doc and "encoding" in doc:
        doc = json.loads(base64.b64decode(doc["content"]).decode("utf-8"))
    return doc


def get_report():
    try:
        result = _request_json(_gh_url("blofin_close_reasons.json") + "?ref=main",
                               headers=_gh_headers())
    except HTTPError as exc:
        if exc.code == 404:
            return {}, None
        raise
    if not isinstance(result, dict) or "sha" not in result:
        raise RuntimeError("Expected GitHub contents response")
    data = base64.b64decode(result["content"]).decode("utf-8")
    return json.loads(data), result["sha"]


def save_report(data):
    url = _gh_url("blofin_close_reasons.json")
    # Optimistic retry: LIVE scans can commit while this read-only audit runs.
    for attempt in range(3):
        latest, sha = get_report()
        merged = dict(latest)
        merged.update(data)
        payload = {
            "message": "Save verified BloFin closing-order evidence",
            "branch": "main",
            "content": base64.b64encode(
                (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode()
            ).decode(),
        }
        if sha:
            payload["sha"] = sha
        try:
            _request_json(url, method="PUT",
                data=json.dumps(payload).encode(),
                headers={**_gh_headers(), "Content-Type": "application/json"})
            return True
        except HTTPError as exc:
            if exc.code not in (409, 422) or attempt == 2:
                raise
            time.sleep(2)
    return False


def private_get(method, path, params=None):
    if method != "GET" or path != "/api/v1/trade/orders-history":
        raise RuntimeError("READ_ONLY_BROKER_AUDIT_BLOCKED")
    query = urlencode(params or {})
    request_path = path + ("?" + query if query else "")
    for attempt in range(3):
        timestamp = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        prehash = request_path + "GET" + timestamp + nonce
        hex_digest = hmac.new(BLOFIN_SECRET.encode(), prehash.encode(),
                              hashlib.sha256).hexdigest().encode()
        headers = {
            "ACCESS-KEY": BLOFIN_KEY,
            "ACCESS-SIGN": base64.b64encode(hex_digest).decode(),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": BLOFIN_PASSPHRASE,
            "Accept": "application/json",
        }
        try:
            payload = _request_json(
                "https://openapi.blofin.com" + request_path, headers=headers)
            if str(payload.get("code")) != "0":
                raise RuntimeError("BloFin response " + str(payload.get("code")))
            return payload.get("data") or []
        except HTTPError as exc:
            if exc.code != 429 or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return []


def notify(message):
    if not NTFY_TOPIC:
        return
    try:
        req = Request("https://ntfy.sh/" + NTFY_TOPIC,
            method="POST", data=message.encode("utf-8"),
            headers={"Title": "BloFin VERIFIED CLOSE CAUSE"})
        with urlopen(req, timeout=8):
            pass
    except Exception as exc:
        print("NOTIFY_FAILURE:", type(exc).__name__)


STATE = Path("blofin_live_state.json")
AUDIT = Path("blofin_close_reasons.json")
UK = ZoneInfo("Europe/London")
WINDOW_MS = 120_000
MAX_TRADES_PER_RUN = 4
MAX_ATTEMPTS = 4


def integer(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def classify(trade, orders):
    """Never infer TP/SL from exit price; require an exchange order record."""
    inst = str(trade.get("inst") or "")
    side = "buy" if trade.get("side") == "SHORT" else "sell"
    open_ms, closed_ms = integer(trade.get("opened_ms")), integer(trade.get("closed_ms"))
    candidates = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("instId") or inst) != inst:
            continue
        if str(order.get("side") or "").lower() != side:
            continue
        if str(order.get("state") or "").lower() not in ("filled", "partially_canceled"):
            continue
        try:
            if float(order.get("filledSize") or 0) <= 0:
                continue
        except (ValueError, TypeError):
            continue
        update_ms = integer(order.get("updateTime"))
        if update_ms < open_ms or abs(update_ms - closed_ms) > WINDOW_MS:
            continue
        category = str(order.get("orderCategory") or "").lower()
        if category not in ("tp", "sl", "normal", "full_liquidation",
                            "partial_liquidation", "liquidation", "adl"):
            continue
        candidates.append(order)

    if not candidates:
        return {"status": "unresolved", "reason": "NO_MATCHING_FILLED_CLOSE_ORDER"}
    if len(candidates) != 1:
        return {"status": "unresolved", "reason": "MULTIPLE_MATCHING_CLOSE_ORDERS",
                "candidate_order_ids": [str(o.get("orderId") or "")
                                        for o in candidates[:8]]}
    order = candidates[0]
    category = str(order.get("orderCategory") or "").lower()
    client_id = str(order.get("clientOrderId") or "")
    source = ("BOT_MARKET_CLOSE" if client_id.startswith("liveclose")
              else "NOT_PROVEN")
    return {
        "status": "verified", "reason": category.upper(),
        "source": source,
        "order_id": str(order.get("orderId") or ""),
        "client_order_id": client_id,
        "filled_ms": integer(order.get("updateTime")),
        "filled_price": str(order.get("averagePrice") or ""),
        "mark_trigger": (
            str(order.get("tpTriggerPriceType") or "") if category == "tp"
            else str(order.get("slTriggerPriceType") or "") if category == "sl"
            else ""
        ),
    }


def inspect(trade, request):
    try:
        rows = request("GET", "/api/v1/trade/orders-history", params={
            "instId": trade["inst"],
            "begin": str(max(0, integer(trade["opened_ms"]) - WINDOW_MS)),
            "end": str(integer(trade["closed_ms"]) + WINDOW_MS),
            "limit": "100",
        }) or []
        if not isinstance(rows, list):
            return {"status": "unresolved", "reason": "INVALID_BROKER_RESPONSE"}
        return classify(trade, rows)
    except Exception as exc:
        return {"status": "unresolved",
                "reason": "BROKER_API_" + type(exc).__name__}


def audit(state, existing, request, notifier=None):
    records = dict(existing)
    processed = 0
    reset_ms = integer(state.get("bankroll_rebased_at_ms"))
    # Oldest first for fair retrospective backfill.
    for trade in state.get("trade_history") or []:
        if processed >= MAX_TRADES_PER_RUN:
            break
        if trade.get("reason") != "external close":
            continue
        opened_ms = integer(trade.get("opened_ms"))
        if opened_ms < reset_ms:
            continue
        key = f"{opened_ms}:{trade.get('inst')}"
        previous = records.get(key) or {}
        if previous.get("status") == "verified" or integer(previous.get("attempts")) >= MAX_ATTEMPTS:
            continue
        result = inspect(trade, request)
        result["attempts"] = integer(previous.get("attempts")) + 1
        result["coin"] = trade.get("inst")
        result["side"] = trade.get("side")
        result["opened_ms"] = opened_ms
        result["closed_ms"] = integer(trade.get("closed_ms"))
        result["closed_uk"] = datetime.fromtimestamp(
            result["closed_ms"] / 1000, UK).strftime("%Y-%m-%d %H:%M:%S")
        result["entry_price"] = trade.get("open_price")
        result["exit_price"] = trade.get("close_price")
        result["pnl_usdt"] = trade.get("net_pnl_usdt")
        records[key] = result
        processed += 1
        if notifier and (result["status"] == "verified"
                         or result["attempts"] == MAX_ATTEMPTS):
            notifier(
                f"{result['coin']} {result['side']} | {result['closed_uk']} UK | "
                f"BloFin closure: {result['reason']} | "
                f"order {result.get('order_id', 'unknown')} | "
                f"net {result['pnl_usdt']} USDT"
            )
    return records, processed


def main():
    if not GH_TOKEN or not all((BLOFIN_KEY, BLOFIN_SECRET, BLOFIN_PASSPHRASE)):
        raise RuntimeError("GitHub or BloFin API credentials unavailable")
    state = load_remote("blofin_live_state.json")
    old, _ = get_report()
    current, processed = audit(state, old, private_get, notify)
    if current != old:
        save_report(current)
    print("BLOFIN_CLOSE_REASON_AUDIT checked=", processed,
          "total=", len(current),
          "verified=", sum(r.get("status") == "verified" for r in current.values()))


if __name__ == "__main__":
    main()
