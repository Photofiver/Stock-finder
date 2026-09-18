import json
import blofin_live_hourly as bot
bars=bot.fetch_1h("ONE-USDT")
for b in bars[-4:]:
    print(json.dumps({
      "close_ms": int(b["ts"]+bot.D1H_MS),
      "open": b["o"], "close": b["c"], "volume": b["v"],
      "color": bot.candle_color(b)
    }, sort_keys=True))
i=len(bars)-1
print("SIGNAL", bot.volume_flip_signal(bars,i))
print("VOL_GT_PREV", bars[i]["v"] > bars[i-1]["v"])
