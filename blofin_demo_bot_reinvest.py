import json
import os
import time
from decimal import Decimal

import blofin_demo_bot as base

BANKROLL_FILE = os.getenv("DEMO_BANKROLL_FILE", "demo_bankroll.json")
START_BANKROLL_USDT = Decimal(os.getenv("DEMO_START_BANKROLL_USDT", "10"))
MIN_BANKROLL_USDT = Decimal("0.01")


def load_bankroll():
    try:
        with open(BANKROLL_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        value = Decimal(str(payload.get("bankroll_usdt", "")))
        if value > 0:
            return value
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return START_BANKROLL_USDT


def save_bankroll(bankroll):
    bankroll = max(Decimal("0"), Decimal(str(bankroll)))
    tmp = BANKROLL_FILE + ".tmp"
    payload = {
        "bankroll_usdt": format(bankroll.quantize(Decimal("0.00000001")), "f"),
        "updated_ms": int(time.time() * 1000),
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, BANKROLL_FILE)


def get_closed_result(position, reason_hint=None):
    hist = None
    for _ in range(6):
        time.sleep(2)
        hist = base.latest_position_history(position["inst"], position["opened_ms"])
        if hist:
            break

    if not hist:
        base.notify(
            f"CLOSED {position['direction']} {position['inst']} | powód: {reason_hint or 'TP/SL'} | "
            "brak historii PnL w API — bankroll bez zmiany",
            "BloFin DEMO RESULT",
        )
        return None

    pnl = base.d(hist.get("realizedPnl") or "0")
    fee = base.d(hist.get("fee") or "0")
    net = pnl + fee
    result = "WIN" if net > 0 else "LOSS" if net < 0 else "FLAT"
    reason = reason_hint or result
    base.notify(
        f"{result} {position['direction']} {position['inst']}\n"
        f"powód: {reason} | realizedPnl {pnl:.4f} USDT | fee/rebate {fee:.4f} USDT | "
        f"net {net:.4f} USDT",
        "BloFin DEMO RESULT",
    )
    return net


def apply_result(bankroll, net_change):
    if net_change is None:
        return bankroll
    bankroll = max(Decimal("0"), bankroll + net_change)
    save_bankroll(bankroll)
    base.notify(
        f"Bankroll DEMO po zamknięciu: {bankroll:.4f} USDT. "
        "Następna pozycja użyje aktualnego bankrollu (1x).",
        "BloFin DEMO BANKROLL",
    )
    return bankroll


def run():
    base.require_credentials()
    base.ensure_net_mode()
    instruments = base.get_instruments()
    position = None
    last_signal_key = None
    bankroll = load_bankroll()
    save_bankroll(bankroll)

    started = time.monotonic()
    end_time = started + base.TEST_MINUTES * 60
    base.notify(
        f"START test DEMO z reinwestowaniem: bankroll {bankroll:.4f} USDT, "
        f"sesja {base.TEST_MINUTES} min. Po każdym zamknięciu kolejna pozycja używa "
        "bankroll + zysk - strata.",
        "BloFin DEMO START",
    )

    while time.monotonic() < end_time:
        tick_started = time.monotonic()
        try:
            if position:
                open_row = base.find_open_position(position["inst"])
                if not open_row:
                    net = get_closed_result(position)
                    bankroll = apply_result(bankroll, net)
                    position = None
                elif time.monotonic() - position["opened_monotonic"] >= base.MAX_HOLD_SECONDS:
                    base.close_position(position["inst"])
                    net = get_closed_result(position, "TIME EXIT")
                    bankroll = apply_result(bankroll, net)
                    position = None

            if not position:
                if bankroll < MIN_BANKROLL_USDT:
                    base.notify(
                        f"Bankroll DEMO {bankroll:.4f} USDT — za mały do dalszych wejść.",
                        "BloFin DEMO STOP",
                    )
                    break

                # Existing sizing logic treats MAX_NOTIONAL_USDT as the position notional cap.
                # Set it to the strategy bankroll so profits and losses are compounded automatically.
                base.MAX_NOTIONAL_USDT = bankroll
                top10 = base.get_top10(instruments)
                candidate = base.choose_candidate(top10, instruments, last_signal_key)
                if candidate:
                    position = base.place_trade(candidate, instruments[candidate["inst"]])
                    last_signal_key = candidate["signal_key"]
                    base.notify(
                        f"Reinvest ON | bankroll {bankroll:.4f} USDT | "
                        f"pozycja notional≈{Decimal(position['notional']):.4f} USDT",
                        "BloFin DEMO REINVEST",
                    )
                else:
                    print(f"no eligible signal | bankroll={bankroll:.4f} USDT")
        except Exception as exc:
            print(f"TICK ERROR: {type(exc).__name__}: {exc}")
            base.notify(f"BŁĄD DEMO: {type(exc).__name__}: {exc}", "BloFin DEMO ERROR")

        wait = base.CHECK_SECONDS - (time.monotonic() - tick_started)
        if wait > 0 and time.monotonic() + wait < end_time:
            time.sleep(wait)

    if position:
        try:
            if base.find_open_position(position["inst"]):
                base.close_position(position["inst"])
                net = get_closed_result(position, "TEST END")
            else:
                net = get_closed_result(position)
            bankroll = apply_result(bankroll, net)
        except Exception as exc:
            base.notify(
                f"BŁĄD przy zamykaniu końcowym {position['inst']}: {exc}",
                "BloFin DEMO ERROR",
            )

    save_bankroll(bankroll)
    base.notify(
        f"KONIEC sesji testowej DEMO. Bankroll zapisany: {bankroll:.4f} USDT.",
        "BloFin DEMO STOP",
    )


if __name__ == "__main__":
    run()
