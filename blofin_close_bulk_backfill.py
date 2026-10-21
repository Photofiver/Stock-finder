"""READ-ONLY bulk backfill of BloFin LIVE closing orders.

One account-wide paginated GET history, no broker POST or order changes.
For confirmed classification use actual broker orderCategory, not fill proximity.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import blofin_close_reasons_audit as archive

HISTORY_ENDPOINT = "/api/v1/trade/orders-history"
MIN_START_MS = int(datetime(2026, 10, 20, 21, 0, tzinfo=ZoneInfo("UTC")).timestamp()*1000)
MATCH_WINDOW_MS = 120_000
MAX_PAGES = 8
UA = "Mozilla/5.0 BloFinStockFinder/1.0"
VALID_CATEGORIES = {"tp", "sl", "normal", "full_liquidation",
                    "partial_liquidation", "adl", "liquidation"}


def _request_history(params):
    """Only history GET, signed exactly like the working LIVE client."""
    request_path = HISTORY_ENDPOINT + "?" + urlencode(params)
    timestamp = str(int(time.time()*1000))
    nonce = str(uuid.uuid4())
    digest = hmac.new(archive.BLOFIN_SECRET.encode(),
        (request_path+"GET"+timestamp+nonce).encode(), hashlib.sha256)
    headers = {
        "ACCESS-KEY": archive.BLOFIN_KEY,
        "ACCESS-SIGN": base64.b64encode(digest.hexdigest().encode()).decode(),
        "ACCESS-TIMESTAMP": timestamp,
        "ACCESS-NONCE": nonce,
        "ACCESS-PASSPHRASE": archive.BLOFIN_PASSPHRASE,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
    }
    req = Request("https://openapi.blofin.com"+request_path, headers=headers)
    try:
        with urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 403:
            # Official BloFin docs: firewall rule often triggered by excess GETs.
            # NEVER retry while banned or send dozens of per-coin queries.
            raise RuntimeError("BLOFIN_NETWORK_FIREWALL_403_NO_RETRY") from exc
        if exc.code == 429:
            raise RuntimeError("BLOFIN_RATE_LIMIT_429_NO_RETRY") from exc
        raise
    if str(result.get("code"))!="0":
        raise RuntimeError("BLOFIN_HISTORY_API_"+str(result.get("code"))[:40])
    rows = result.get("data") or []
    if not isinstance(rows, list):
        raise RuntimeError("BLOFIN_HISTORY_RESPONSE_NOT_LIST")
    return rows


def fetch_bulk_orders(fetcher, start_ms, end_ms):
    """Paginate at most MAX_PAGES, limit 100; stop on 403, never fan out."""
    result = {}
    cursor = None
    pages = 0
    complete = False
    for i in range(MAX_PAGES):
        params = {"begin":str(start_ms),"end":str(end_ms),"limit":"100"}
        if cursor:
            params["after"] = cursor
        rows = fetcher(params)
        pages += 1
        for row in rows:
            if isinstance(row, dict) and row.get("orderId"):
                result[str(row["orderId"])] = row
        if len(rows) < 100:
            complete = True
            break
        last = str(rows[-1].get("orderId") or "")
        if not last or last == cursor:
            raise RuntimeError("BROKER_ORDER_PAGINATION_STALLED")
        cursor = last
        # Broker rate guard, even on different GitHub Actions runners.
        time.sleep(2)
    return list(result.values()), pages, complete


def match_close(trade, orders):
    """Only a single confirmed opposite-side filled order can verify cause."""
    inst = str(trade.get("inst") or "")
    side = "sell" if trade.get("side")=="LONG" else "buy"
    opened = int(trade.get("opened_ms") or 0)
    closed = int(trade.get("closed_ms") or 0)
    valid = []
    for o in orders:
        if str(o.get("instId") or "") != inst:
            continue
        if str(o.get("side") or "").lower() != side:
            continue
        if str(o.get("state") or "").lower() not in ("filled","partially_canceled"):
            continue
        try:
            if float(o.get("filledSize") or 0)<=0:
                continue
            actual = int(o.get("updateTime") or 0)
        except (ValueError,TypeError):
            continue
        if actual < opened or abs(actual - closed) > MATCH_WINDOW_MS:
            continue
        cat = str(o.get("orderCategory") or "").lower()
        if cat not in VALID_CATEGORIES:
            continue
        valid.append(o)
    if not valid:
        return {"status":"unresolved","reason":"NO_MATCHING_EXCHANGE_CLOSE_ORDER"}
    if len(valid)>1:
        return {"status":"unresolved","reason":"MULTIPLE_POSSIBLE_EXIT_ORDERS",
                "candidate_ids":[str(x.get("orderId")) for x in valid[:8]]}
    o=valid[0]
    return {
        "status":"verified",
        "reason":str(o.get("orderCategory")).upper(),
        "source":"BROKER_ORDER_HISTORY",
        "order_id":str(o.get("orderId") or ""),
        "client_order_id":str(o.get("clientOrderId") or ""),
        "filled_ms":int(o.get("updateTime") or 0),
        "filled_price":str(o.get("averagePrice") or ""),
        "tp_trigger_type":str(o.get("tpTriggerPriceType") or ""),
        "sl_trigger_type":str(o.get("slTriggerPriceType") or ""),
        "reduce_only":str(o.get("reduceOnly") or ""),
        "algo_id":str(o.get("algoId") or ""),
    }


def backfill(state, records, orders):
    updated = dict(records)
    stats = {"eligible":0,"verified_new":0,"still_unresolved":0}
    for t in state.get("trade_history") or []:
        opened = int(t.get("opened_ms") or 0)
        closed = int(t.get("closed_ms") or 0)
        if closed < MIN_START_MS or t.get("reason") != "external close":
            continue
        key = str(opened)+":"+str(t.get("inst"))
        prior = updated.get(key) or {}
        stats["eligible"] += 1
        if prior.get("status") == "verified":
            continue
        result = match_close(t,orders)
        result.update({
            "coin":t.get("inst"),"side":t.get("side"),
            "opened_ms":opened,"closed_ms":closed,
            "closed_uk":datetime.fromtimestamp(closed/1000,
                ZoneInfo("Europe/London")).strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price":t.get("open_price"),
            "exit_price":t.get("close_price"),
            "pnl_usdt":t.get("net_pnl_usdt"),
            "bulk_history_audit":True,
        })
        if result["status"] == "verified":
            stats["verified_new"] += 1
        else:
            stats["still_unresolved"] += 1
        updated[key]=result
    return updated,stats


def main():
    if not archive.GH_TOKEN or not all((archive.BLOFIN_KEY,
        archive.BLOFIN_SECRET,archive.BLOFIN_PASSPHRASE)):
        raise RuntimeError("Missing GitHub or BloFin read credentials")
    state=archive.load_remote("blofin_live_state.json")
    existing,_=archive.get_report()
    # 21:00 UTC = 22:00 UK on Oct 20; include opening orders margin.
    now=int(time.time()*1000)
    start=MIN_START_MS-300_000
    orders,pages,complete=fetch_bulk_orders(_request_history,start,now+120_000)
    updated,stats=backfill(state,existing,orders)
    if updated != existing:
        archive.save_report(updated)
    print("BLOFIN_BULK_CLOSE_BACKFILL",json.dumps({
        "pages":pages,"total_orders":len(orders),"complete":complete,
        **stats,"verified_total":sum(v.get("status")=="verified"
                                    for v in updated.values())}))
    if not complete:
        print("WARNING: Pagination budget exhausted; no unmatched closure "
              "is considered proven without an execution.")


if __name__ == "__main__":
    main()
