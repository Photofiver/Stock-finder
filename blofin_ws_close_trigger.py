"""Public BloFin WebSocket clock for a confirmed 5-minute candle.

No account credentials are read or sent. This only wakes up the existing LIVE
scanner; the scanner must independently confirm every instrument's closed candle.
"""
import json
import time

PUBLIC_WS_URL = "wss://openapi.blofin.com/ws/public"
REFERENCE_INSTRUMENT = "BTC-USDT"
SIGNAL_MS = 5 * 60 * 1000
POST_CLOSE_WINDOW_MS = 120_000
WS_GRACE_MS = 5_000
REST_FALLBACK_DELAY_MS = 2_000


def utc_ms():
    return time.time_ns() // 1_000_000


def select_close_ms(now_ms, signal_ms=SIGNAL_MS):
    """Return (candle close timestamp, needs_waiting).

    Starts within two minutes after a boundary scan that boundary; earlier
    prewarmed starts wait for the next one. No signal is evaluated here.
    """
    floor = (int(now_ms) // signal_ms) * signal_ms
    if int(now_ms) - floor < POST_CLOSE_WINDOW_MS:
        return floor, False
    return floor + signal_ms, True


def matches_confirmed_close(payload, expected_close_ms, instrument=REFERENCE_INSTRUMENT):
    """Accept only a final 5m candle of the exact expected period."""
    if not isinstance(payload, dict):
        return False
    arg = payload.get("arg")
    if not isinstance(arg, dict) or arg.get("channel") != "candle5m":
        return False
    if arg.get("instId") != instrument:
        return False
    data = payload.get("data")
    if not isinstance(data, list):
        return False
    for candle in data:
        try:
            if isinstance(candle, list) and len(candle) >= 9:
                opened_ms, confirmed = int(candle[0]), str(candle[8])
            elif isinstance(candle, dict):
                opened_ms, confirmed = int(candle["ts"]), str(candle["confirm"])
            else:
                continue
            if opened_ms + SIGNAL_MS == int(expected_close_ms) and confirmed == "1":
                return True
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return False


def wait_for_close():
    """Wait for BloFin confirmation; on failure use clock + REST revalidation."""
    target_ms, pending = select_close_ms(utc_ms())
    if not pending:
        # A scheduled/manual runner arrived after the boundary. The trading
        # script still checks REST-confirmed bars and signal freshness.
        remain_ms = target_ms + REST_FALLBACK_DELAY_MS - utc_ms()
        if remain_ms > 0:
            time.sleep(remain_ms / 1000)
        print(f"WS_CLOSE_TRIGGER mode=already_closed delay_ms={utc_ms()-target_ms}")
        return target_ms

    socket = None
    reason = "confirmation_not_received"
    try:
        # This is an unauthenticated public feed. Never attach API keys.
        import websocket

        connect_seconds = max(0.5, min(5, (target_ms + WS_GRACE_MS - utc_ms()) / 1000))
        socket = websocket.create_connection(PUBLIC_WS_URL, timeout=connect_seconds)
        socket.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "candle5m", "instId": REFERENCE_INSTRUMENT}],
        }))
        while utc_ms() < target_ms + WS_GRACE_MS:
            remaining_seconds = (target_ms + WS_GRACE_MS - utc_ms()) / 1000
            if remaining_seconds <= 0:
                break
            socket.settimeout(min(2, remaining_seconds))
            try:
                raw = socket.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if matches_confirmed_close(payload, target_ms) and utc_ms() >= target_ms:
                print(f"WS_CLOSE_TRIGGER mode=confirmed delay_ms={utc_ms()-target_ms}")
                return target_ms
    except Exception as exc:
        # Do not print network responses/headers or any environment secrets.
        reason = type(exc).__name__
    finally:
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    # Never start the scan before the boundary. The scanner additionally
    # rechecks confirmed candles via REST and independently enforces freshness.
    remaining_ms = target_ms + REST_FALLBACK_DELAY_MS - utc_ms()
    if remaining_ms > 0:
        time.sleep(remaining_ms / 1000)
    print(f"WS_CLOSE_TRIGGER mode=fallback reason={reason} delay_ms={utc_ms()-target_ms}")
    return target_ms


if __name__ == "__main__":
    wait_for_close()
