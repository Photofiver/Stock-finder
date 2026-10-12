"""BloFin 5m trade snapshots (generated PNG charts, not TradingView screenshots).

A chart is captured at entry and on confirmation of an exit, showing
candlesticks, SMA10/SMA20, Volume, RSI14, MACD(12,26,9), and event price.
Trading decisions do not depend on this diagnostics module.
"""
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BAR_MS = 5 * 60 * 1000
CHART_ROOT = Path("blofin_trade_charts")
LONDON = ZoneInfo("Europe/London")


def _sma(values, period):
    return [
        sum(values[i - period + 1:i + 1]) / period if i >= period - 1 else math.nan
        for i in range(len(values))
    ]


def _ema(values, period):
    alpha = 2.0 / (period + 1)
    result = []
    for value in values:
        result.append(value if not result else alpha * value + (1 - alpha) * result[-1])
    return result


def _rsi(closes, period=14):
    result = [math.nan] * len(closes)
    if len(closes) <= period:
        return result
    gains = [max(0.0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i-1]) / period
            avg_l = (avg_l * (period - 1) + losses[i-1]) / period
        result[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return result


def _normalize(raw, event_ms):
    """No candles beginning after the event. Keeps the event's partial 5m bar."""
    limit_ts = (int(event_ms) // BAR_MS) * BAR_MS
    rows = []
    for item in raw if isinstance(raw, list) else []:
        try:
            ts = int(item[0])
            o, h, l, c, volume = map(float, item[1:6])
            if ts <= limit_ts and min(o, h, l, c) > 0:
                rows.append((ts, o, h, l, c, volume))
        except (ValueError, TypeError, IndexError):
            continue
    return sorted({row[0]: row for row in rows}.values())


def render_trade_chart(candles, inst, side, event, event_ms, event_price,
                       opened_ms, entry_price=None, tp=None, sl=None,
                       pnl=None, reason="", chart_root=CHART_ROOT):
    """Render from supplied BloFin candles: testable without API or order access."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    candles = _normalize(candles, event_ms)
    if len(candles) < 21:
        raise ValueError(f"{inst}: too few candles for chart ({len(candles)})")
    closes = [x[4] for x in candles]
    opens = [x[1] for x in candles]
    vol = [x[5] for x in candles]
    fast = _sma(closes, 10)
    slow = _sma(closes, 20)
    rsi = _rsi(closes, 14)
    macd = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    macd_signal = _ema(macd, 9)
    hist = [a - b for a, b in zip(macd, macd_signal)]
    green, red = "#13966e", "#d14b60"
    colors = [green if c >= o else red for o, c in zip(opens, closes)]
    x = list(range(len(candles)))
    fig, axes = plt.subplots(
        4, 1, figsize=(16, 11), sharex=True,
        gridspec_kw={"height_ratios": [4.7, 1.2, 1.45, 1.55], "hspace": 0.16},
    )
    ax_price, ax_vol, ax_rsi, ax_macd = axes
    for i, (_, op, hi, lo, cl, volume) in enumerate(candles):
        ax_price.vlines(i, lo, hi, color=colors[i], lw=1)
        ax_price.add_patch(Rectangle(
            (i - 0.33, min(op, cl)), 0.66, max(abs(cl - op), 1e-12),
            color=colors[i], alpha=0.78, lw=0,
        ))
    ax_price.plot(x, fast, label="MA10", color="#2078b4", lw=1.55)
    ax_price.plot(x, slow, label="MA20", color="#f0a234", lw=1.55)
    if entry_price and float(entry_price) > 0 and event == "EXIT":
        ax_price.axhline(float(entry_price), color="#6e6fa5", lw=1, ls=":",
                         label=f"Entry {float(entry_price):.7g}")
    if tp and float(tp) > 0:
        ax_price.axhline(float(tp), color=green, ls="--", alpha=0.5, lw=0.85,
                         label="TP")
    if sl and float(sl) > 0:
        ax_price.axhline(float(sl), color=red, ls="--", alpha=0.5, lw=0.85,
                         label="SL")
    event_candle_start = (int(event_ms) // BAR_MS) * BAR_MS
    marker_idx = next(
        (i for i, row in enumerate(candles) if row[0] == event_candle_start),
        len(candles) - 1,
    )
    marker_color = green if event == "ENTRY" else "#493f8e"
    ax_price.axvline(marker_idx, ls=":", color=marker_color, lw=1.7)
    if float(event_price) > 0:
        ax_price.scatter([marker_idx], [float(event_price)], marker="D", s=130,
                         color=marker_color, edgecolors="white", zorder=8, lw=1)
        ax_price.annotate(
            f"{event} {float(event_price):.8g}", (marker_idx, float(event_price)),
            xytext=(-13, 15), textcoords="offset points", ha="right",
            fontsize=11, weight="bold", color=marker_color,
            bbox={"boxstyle": "round,pad=0.3", "fc": "white", "alpha": 0.9, "ec": marker_color},
        )
    when = datetime.fromtimestamp(event_ms / 1000, tz=LONDON)
    summary = f"{inst}  |  {side}  |  {event}  |  {when:%d.%m.%Y %H:%M:%S} UK"
    if pnl is not None:
        summary += f"  |  netto {float(pnl):+.4f} USDT"
    fig.suptitle(summary, fontsize=17, weight="bold", y=0.987)
    if reason:
        ax_price.set_title(str(reason)[:130], fontsize=10, loc="left", pad=12)
    ax_price.legend(loc="upper left", fontsize=9, ncol=4)
    ax_price.set_ylabel("Cena USDT", fontsize=10)
    ax_price.margins(x=0.018, y=0.13)

    ax_vol.bar(x, vol, color=colors, width=0.85)
    ax_vol.set_ylabel("Volume")
    ax_rsi.plot(x, rsi, color="#844dbe", lw=1.4, label="RSI(14)")
    ax_rsi.axhline(70, color=red, ls=":", lw=1)
    ax_rsi.axhline(30, color=green, ls=":", lw=1)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI 14")
    ax_rsi.legend(loc="upper left", fontsize=9)
    ax_macd.plot(x, macd, label="MACD", color="#156cb8", lw=1.35)
    ax_macd.plot(x, macd_signal, label="Signal", color="#dd8d32", lw=1.35)
    ax_macd.bar(x, hist, color=[green if v >= 0 else red for v in hist],
                width=0.85, alpha=0.55)
    ax_macd.axhline(0, color="#8495a5", lw=0.8)
    ax_macd.set_ylabel("MACD")
    ax_macd.legend(loc="upper left", fontsize=9)
    tick_i = list(range(0, len(x), max(1, len(x) // 8)))
    if tick_i[-1] != len(x) - 1:
        tick_i.append(len(x) - 1)
    labels = [
        datetime.fromtimestamp(candles[i][0] / 1000, tz=LONDON).strftime("%d.%m %H:%M")
        for i in tick_i
    ]
    ax_macd.set_xticks(tick_i, labels, fontsize=9)
    ax_macd.set_xlabel("Czas UK  |  wykres 5m BloFin, nie interfejs TradingView")
    for ax in axes:
        ax.grid(alpha=0.15)
    fig.subplots_adjust(left=0.065, right=0.985, bottom=0.07, top=0.935)
    root = Path(chart_root)
    day = when.strftime("%Y-%m-%d")
    dirname = root / day
    dirname.mkdir(parents=True, exist_ok=True)
    trade_id = str(int(opened_ms))
    filename = f"{trade_id}_{inst.replace('-', '_')}_{event.lower()}.png"
    filepath = dirname / filename
    try:
        fig.savefig(filepath, dpi=125, facecolor="white")
    finally:
        plt.close(fig)
    return filepath.as_posix()


def capture_chart(bot, pos, event, event_ms, price, **kwargs):
    """Non-critical diagnostics. Caller handles exceptions, never skips TP/SL."""
    inst = str(pos["inst"])
    # BloFin 'after' fetches older candles; anchor to the event candle
    # instead of accidentally capturing candles from a later trade.
    raw = bot.market_get(
        "/api/v1/market/candles",
        {"instId": inst, "bar": "5m",
         "after": str(int(event_ms) + BAR_MS),
         "limit": "160"},
    )
    return render_trade_chart(
        raw, inst, str(pos["side"]), str(event).upper(), int(event_ms),
        float(price), int(pos["opened_ms"]),
        entry_price=kwargs.get("entry_price"),
        tp=pos.get("tp"), sl=pos.get("sl"),
        pnl=kwargs.get("pnl"), reason=kwargs.get("reason", ""),
    )
