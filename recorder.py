#!/usr/bin/env python3
"""Cloud recorder for NSE index option chains (runs on GitHub Actions).

    python recorder.py session --tag a --until 12:45     record every minute until 12:45 IST
    python recorder.py session --tag b --until 15:35
    python recorder.py evening                            day's 1-min index candles, F&O bhavcopy, participant OI
    python recorder.py once                               one snapshot now (end-to-end test)

Each minute, for every symbol in SYMBOLS, it saves the option chain of the nearest
expiries (strikes within +/- WIDTH of ATM): LTP, bid, ask, OI, OI change, IV, volume,
plus the spot. Files: data/<date>/<SYM>-<tag>-<hour>.jsonl.gz (one JSON line per minute),
encrypted by save.sh into enc/ before anything is committed.
`commit_every` minutes it calls ./save.sh so a crash loses little.

Stops by itself when NSE shows no trading today (holiday / weekend). Never places orders.
"""

import argparse
import gzip
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta

import fno_alerts as fa

IST = fa.IST
SYMBOLS = {"NIFTY": 2, "BANKNIFTY": 1}       # symbol -> how many nearest expiries to record
WIDTH = 20                                   # strikes each side of ATM
FIELDS = ("last_price", "top_bid_price", "top_ask_price", "oi", "previous_oi", "implied_volatility", "volume")


def now():
    return datetime.now(IST)


def log(msg):
    print(now().strftime("%H:%M:%S"), msg, flush=True)


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        fa.Telegram(tok, chat).send(text)


def save(message):
    if os.path.exists("save.sh"):
        subprocess.run(["bash", "save.sh", message], check=False)


def snapshot(client, sym, exps, t):
    u = fa.UNDERLYINGS[sym]
    out = []
    for e in exps:
        spot, rows = client.chain(u, e)
        if not rows:
            continue
        atm = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
        keep = []
        for r in rows[max(0, atm - WIDTH): atm + WIDTH + 1]:
            line = [r["strike"]]
            for leg in (r["ce"], r["pe"]):
                line += [fa.num(leg.get(k)) for k in FIELDS]
            keep.append(line)
        out.append({"t": t.strftime("%H:%M:%S"), "expiry": e, "spot": spot, "rows": keep,
                    "cols": "strike,ce[ltp,bid,ask,oi,prev_oi,iv,vol],pe[same]"})
    return out


def session(tag, until, commit_every=30):
    client = fa.NseYahoo(gap=1.5)
    day = now().date()
    folder = os.path.join("data", day.isoformat())
    os.makedirs(folder, exist_ok=True)
    end = now().replace(hour=int(until[:2]), minute=int(until[3:]), second=0, microsecond=0)
    if now().weekday() >= 5:
        log("weekend, nothing to record")
        return
    # wait for the open
    while now() < now().replace(hour=9, minute=14, second=0):
        time.sleep(20)
    exps = {}
    for sym, n in SYMBOLS.items():
        try:
            exps[sym] = [e for e in client.expiries(fa.UNDERLYINGS[sym]) if e >= day.isoformat()][:n]
        except Exception as e:  # noqa: BLE001
            log("expiries %s: %s" % (sym, e))
    counts = {s: 0 for s in SYMBOLS}
    last_commit, checked_open = time.monotonic(), False
    announced = False
    while now() < end:
        t0 = time.monotonic()
        t = now()
        if t.hour * 60 + t.minute > 15 * 60 + 31:
            break
        for sym in SYMBOLS:
            if not exps.get(sym):
                continue
            try:
                snaps = snapshot(client, sym, exps[sym], t)
            except Exception as e:  # noqa: BLE001
                log("%s: %s" % (sym, e))
                continue
            with gzip.open(os.path.join(folder, "%s-%s-%02d.jsonl.gz" % (sym, tag, t.hour)), "at") as f:
                for s in snaps:
                    f.write(json.dumps(s) + "\n")
            counts[sym] += 1
            if tag == "a" and not announced:
                announced = True
                telegram("📼 Recorder started for %s (%s)." % (day, ", ".join(SYMBOLS)))
        # holiday check: by 09:35 the chain must have moved today
        if not checked_open and t.hour * 60 + t.minute >= 9 * 60 + 35:
            checked_open = True
            if not any(counts.values()):
                log("no data by 09:35, treating as holiday")
                telegram("📼 No NSE data by 09:35 on %s. Market holiday? Recorder stopped." % day)
                return
        if time.monotonic() - last_commit > commit_every * 60:
            save("data %s %s %s" % (day, tag, t.strftime("%H:%M")))
            last_commit = time.monotonic()
        time.sleep(max(5, 60 - (time.monotonic() - t0)))
    save("data %s %s end" % (day, tag))
    log("done %s" % counts)
    if tag == "b" and any(counts.values()):
        telegram("📼 Recorder finished %s: %s snapshots." % (
            day, ", ".join("%s %d" % kv for kv in counts.items())))


def evening():
    day = now().date()
    folder = os.path.join("data", day.isoformat())
    os.makedirs(folder, exist_ok=True)
    ua = {"User-Agent": "Mozilla/5.0"}
    for name, sym in (("nifty", "^NSEI"), ("banknifty", "^NSEBANK"), ("indiavix", "^INDIAVIX")):
        url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1m" % urllib.request.quote(sym)
        try:
            data = urllib.request.urlopen(urllib.request.Request(url, headers=ua), timeout=30).read()
            open(os.path.join(folder, "%s-1m.json" % name), "wb").write(data)
        except Exception as e:  # noqa: BLE001
            log("yahoo %s: %s" % (sym, e))
        time.sleep(2)
    fo = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip" % day.strftime("%Y%m%d")
    try:
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(urllib.request.Request(fo, headers=ua), timeout=60).read()))
        text = z.open(z.namelist()[0]).read().decode("utf-8", "replace").splitlines()
        keep = [text[0]] + [l for l in text[1:] if ",NIFTY," in l or ",BANKNIFTY," in l]
        with gzip.open(os.path.join(folder, "fo-bhavcopy.csv.gz"), "wt") as f:
            f.write("\n".join(keep))
    except Exception as e:  # noqa: BLE001
        log("bhavcopy: %s" % e)
    part = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_%s.csv" % day.strftime("%d%m%Y")
    try:
        open(os.path.join(folder, "participant-oi.csv"), "wb").write(
            urllib.request.urlopen(urllib.request.Request(part, headers=ua), timeout=30).read())
    except Exception as e:  # noqa: BLE001
        log("participant: %s" % e)
    save("evening %s" % day)


def once():
    """One snapshot of every symbol, saved like a real run: tests secrets, encryption and push."""
    client = fa.NseYahoo(gap=1.5)
    t = now()
    folder = os.path.join("data", t.date().isoformat())
    os.makedirs(folder, exist_ok=True)
    for sym, n in SYMBOLS.items():
        exps = [e for e in client.expiries(fa.UNDERLYINGS[sym]) if e >= t.date().isoformat()][:n]
        with gzip.open(os.path.join(folder, "test-%s.jsonl.gz" % sym), "at") as f:
            for snap in snapshot(client, sym, exps, t):
                f.write(json.dumps(snap) + "\n")
    save("test snapshot %s" % t.strftime("%Y-%m-%d %H:%M"))
    telegram("📼 Cloud recorder test OK at %s IST." % t.strftime("%H:%M"))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("session")
    s.add_argument("--tag", required=True)
    s.add_argument("--until", required=True)
    sub.add_parser("evening")
    sub.add_parser("once")
    a = p.parse_args()
    if a.cmd == "once":
        once()
    elif a.cmd == "session":
        session(a.tag, a.until)
    else:
        if now().weekday() < 5:
            evening()


if __name__ == "__main__":
    sys.exit(main())
