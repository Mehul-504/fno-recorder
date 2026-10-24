#!/usr/bin/env python3
"""India F&O trade alerts: rule-based option-buying signals on Telegram.

Watches index spot candles, and when a 5-minute candle closes outside the
opening range it checks the day's open-interest change near the ATM strike.
If both agree it sends a signal to Telegram:

    BUY NIFTY 22600 CE @ 137.65   SL 110.10   T1 165.20   T2 192.75

then follows that option's premium and reports SL / T1 / T2 / time exit,
and sends a summary after the close. It never places an order.

Data, "data_source" in config.json:
  "nse" (default, free, no account):
       NSE website option chain  /api/option-chain-v3   LTP, bid/ask, OI, OI change, IV
       NSE website expiries      /api/option-chain-contract-info
       Yahoo Finance chart       5-minute index candles (^NSEI, ^NSEBANK, ...)
       Unofficial public endpoints: they can lag, change or block; polled gently.
       SENSEX is not available (BSE blocks automated access).
       With "record_chains" on, the chain near ATM is saved every poll to
       fno/record/<date>/<index>.jsonl.gz, building intraday history for backtests.
  "dhan" (Data API plan, daily token):
       POST /v2/charts/intraday, /v2/optionchain/expirylist, /v2/optionchain
       One option-chain request per 3 seconds; every call is spaced 3.1 s apart.
Alerts: Telegram Bot API sendMessage.

Commands:
    fno_alerts.py init                 write fno/config.json and fno/secrets.example.json
    fno_alerts.py check                test the data source (NIFTY spot and expiries)
    fno_alerts.py telegram-chat-id     print chat ids that messaged your bot
    fno_alerts.py test-telegram        send a test message
    fno_alerts.py run [--once] [--dry-run]
                                       watch from 09:15 to 15:35 IST on weekdays
    fno_alerts.py status               today's signals and results

Secrets come from environment variables (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) or fno/secrets.json. Standard library only.
"""

import argparse
import gzip
import http.cookiejar
import json
import math
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DHAN = "https://api.dhan.co/v2"
TELEGRAM = "https://api.telegram.org/bot%s/%s"
BAR = timedelta(minutes=5)
TICK = 0.05

# id/seg: Dhan scrip master (segment IDX_I); nse: NSE option-chain symbol; yahoo: chart symbol.
# Stock F&O: add {"RELIANCE": {"nse": "RELIANCE", "nse_type": "Equity", "yahoo": "RELIANCE.NS"}}
# (plus "id"/"seg" from Dhan's scrip master for the dhan source) under "custom_underlyings".
UNDERLYINGS = {
    "NIFTY": {"id": 13, "seg": "IDX_I", "nse": "NIFTY", "yahoo": "^NSEI"},
    "BANKNIFTY": {"id": 25, "seg": "IDX_I", "nse": "BANKNIFTY", "yahoo": "^NSEBANK"},
    "FINNIFTY": {"id": 27, "seg": "IDX_I", "nse": "FINNIFTY", "yahoo": "NIFTY_FIN_SERVICE.NS"},
    "MIDCPNIFTY": {"id": 442, "seg": "IDX_I", "nse": "MIDCPNIFTY", "yahoo": "NIFTY_MID_SELECT.NS"},
    "SENSEX": {"id": 51, "seg": "IDX_I", "yahoo": "^BSESN"},
}

DEFAULT_CONFIG = {
    "data_source": "nse",
    "record_chains": True,
    "underlyings": ["NIFTY", "BANKNIFTY"],
    "custom_underlyings": {},
    "poll_seconds": 60,
    "opening_range_minutes": 15,
    "first_entry": "09:30",
    "last_entry": "14:30",
    "square_off": "15:15",
    "fresh_minutes": 10,
    "min_range_pct": 0.15,
    "max_range_pct": 1.2,
    "oi_strikes": 5,
    "min_oi_ratio": 1.2,
    "sl_min_pct": 15,
    "sl_max_pct": 35,
    "targets_r": [1, 2],
    "trail_to_entry_after_t1": True,
    "max_spread_pct": 2.0,
    "max_signals_per_day": 4,
    "expiry_day": "next",
    "notify_skips": False,
}

SECRET_KEYS = ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


# ---------------------------------------------------------------- helpers

def now_ist():
    return datetime.now(IST)


def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def minute_of(dt):
    return dt.hour * 60 + dt.minute


def at(day_dt, minutes):
    return day_dt.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def tick(x):
    return round(round(x / TICK) * TICK, 2)


def lakh(n):
    sign = "+" if n >= 0 else "-"
    n = abs(n)
    if n >= 1e7:
        return "%s%.2f Cr" % (sign, n / 1e7)
    if n >= 1e5:
        return "%s%.1f L" % (sign, n / 1e5)
    return "%s%d" % (sign, n)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def load_config(workdir):
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(os.path.join(workdir, "config.json"), {}))
    return cfg


def load_secrets(workdir):
    path = os.path.join(workdir, "secrets.json")
    file_secrets = load_json(path, {})
    if file_secrets and os.path.exists(path):
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            print("warning: %s is readable by other users; run: chmod 600 %s" % (path, path), file=sys.stderr)
    return {k: os.environ.get(k) or str(file_secrets.get(k) or "") for k in SECRET_KEYS}


# ---------------------------------------------------------------- Dhan

class DhanError(Exception):
    pass


class DhanAuthError(DhanError):
    pass


class Dhan:
    def __init__(self, client_id, token, gap=3.1, opener=None):
        self.client_id = client_id
        self.token = token
        self.gap = gap
        self.last = 0.0
        self.opener = opener or urllib.request.urlopen

    def post(self, path, body):
        wait = self.gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(
            DHAN + path, data=json.dumps(body).encode(), method="POST",
            headers={"access-token": self.token, "client-id": self.client_id,
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener(req, timeout=30) as r:
                data = json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403) or "DH-901" in text or "DH-902" in text:
                raise DhanAuthError("Dhan refused the request (%s): %s" % (e.code, text))
            raise DhanError("Dhan %s on %s: %s" % (e.code, path, text))
        finally:
            self.last = time.monotonic()
        if isinstance(data, dict) and data.get("errorCode") in ("DH-901", "DH-902"):
            raise DhanAuthError("Dhan: %s" % data.get("errorMessage", data))
        if isinstance(data, dict) and (data.get("status") == "failure" or data.get("errorCode")):
            raise DhanError("Dhan error on %s: %s" % (path, data))
        return data

    def expiries(self, u):
        d = self.post("/optionchain/expirylist", {"UnderlyingScrip": u["id"], "UnderlyingSeg": u["seg"]})
        return sorted(d.get("data") or [])

    def chain(self, u, expiry):
        d = self.post("/optionchain", {"UnderlyingScrip": u["id"], "UnderlyingSeg": u["seg"], "Expiry": expiry})
        return parse_chain(d)

    def candles(self, u, now):
        instrument = "INDEX" if u["seg"] == "IDX_I" else "FUTIDX"
        body = {"securityId": str(u["id"]), "exchangeSegment": u["seg"], "instrument": instrument,
                "interval": "5", "oi": False,
                "fromDate": now.strftime("%Y-%m-%d 09:00:00"),
                "toDate": (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:00")}
        return parse_candles(self.post("/charts/intraday", body))


def bs_delta(spot, strike, years, iv_pct, side, rate=0.065):
    """Black-Scholes delta, for sources that give IV but no greeks."""
    vol = iv_pct / 100.0
    if spot <= 0 or strike <= 0 or years <= 0 or vol <= 0:
        return 0.5 if side == "CE" else -0.5
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    n = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    return n if side == "CE" else n - 1


class NseYahoo:
    """Free data: NSE website option chain plus Yahoo 5-minute index candles."""

    NSE = "https://www.nseindia.com"
    YAHOO = "https://query%d.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

    def __init__(self, gap=2.0, opener=None):
        self.gap = gap
        self.last = 0.0
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar)).open
        self.warm = 0.0

    def get(self, url, referer=None, plain=False):
        wait = self.gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        if plain:  # Yahoo rate-limits browser-looking requests more than plain ones
            headers = {"User-Agent": "Mozilla/5.0"}
        else:
            headers = {"User-Agent": self.UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                       "Accept-Language": "en-US,en;q=0.9"}
        if referer:
            headers["Referer"] = referer
        opener = urllib.request.urlopen if plain else self.opener
        try:
            with opener(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return r.read()
        finally:
            self.last = time.monotonic()

    def nse(self, path):
        # NSE's API answers only with the cookies its option-chain page sets; refresh them every 10 min.
        for attempt in (0, 1):
            if attempt or time.monotonic() - self.warm > 600:
                self.get(self.NSE + "/option-chain")
                self.warm = time.monotonic()
            try:
                return json.loads(self.get(self.NSE + path, referer=self.NSE + "/option-chain").decode())
            except (urllib.error.HTTPError, ValueError) as e:
                if attempt:
                    raise DhanError("NSE %s: %s" % (path, e))

    def expiries(self, u):
        d = self.nse("/api/option-chain-contract-info?symbol=%s" % u["nse"])
        return sorted(datetime.strptime(e, "%d-%b-%Y").strftime("%Y-%m-%d") for e in d.get("expiryDates") or [])

    def chain(self, u, expiry):
        exp = datetime.strptime(expiry, "%Y-%m-%d").strftime("%d-%b-%Y")
        d = self.nse("/api/option-chain-v3?type=%s&symbol=%s&expiry=%s" % (
            u.get("nse_type", "Indices"), u["nse"], exp))
        return parse_nse_chain(d, expiry, now_ist())

    def candles(self, u, now):
        for host in (2, 1, 2):
            try:
                data = json.loads(self.get(self.YAHOO % (host, urllib.request.quote(u["yahoo"])), plain=True).decode())
                res = data["chart"]["result"][0]
                break
            except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError) as e:
                err = e
        else:
            raise DhanError("Yahoo %s: %s" % (u["yahoo"], err))
        q = res["indicators"]["quote"][0]
        keep = [i for i, c in enumerate(q.get("close") or []) if c is not None and q["open"][i] is not None]
        return parse_candles({"timestamp": [res["timestamp"][i] for i in keep],
                              "open": [q["open"][i] for i in keep], "high": [q["high"][i] for i in keep],
                              "low": [q["low"][i] for i in keep], "close": [q["close"][i] for i in keep]})


def parse_nse_chain(data, expiry, now):
    """NSE option-chain-v3 JSON -> (spot, rows) in the same shape as parse_chain."""
    rec = data.get("records") or {}
    rows_in = rec.get("data") or (data.get("filtered") or {}).get("data") or []
    spot = num(rec.get("underlyingValue"))
    end = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=IST)
    years = max((end - now).total_seconds(), 0) / (365 * 24 * 3600)
    rows = []
    for r in rows_in:
        strike = num(r.get("strikePrice"))
        row = {"strike": strike, "ce": {}, "pe": {}}
        for side in ("CE", "PE"):
            leg = r.get(side)
            if not leg:
                continue
            spot = spot or num(leg.get("underlyingValue"))
            oi, chg, iv = num(leg.get("openInterest")), num(leg.get("changeinOpenInterest")), num(leg.get("impliedVolatility"))
            row[side.lower()] = {
                "last_price": num(leg.get("lastPrice")), "top_bid_price": num(leg.get("buyPrice1")),
                "top_ask_price": num(leg.get("sellPrice1")), "oi": oi, "previous_oi": oi - chg,
                "implied_volatility": iv, "volume": num(leg.get("totalTradedVolume")),
                "greeks": {"delta": bs_delta(spot, strike, years, iv, side)},
            }
        rows.append(row)
    rows.sort(key=lambda r: r["strike"])
    return spot, rows


def parse_chain(data):
    d = data.get("data", data) if isinstance(data, dict) else {}
    spot = num(d.get("last_price"))
    rows = []
    for k, v in (d.get("oc") or {}).items():
        v = v or {}
        rows.append({"strike": num(k), "ce": v.get("ce") or {}, "pe": v.get("pe") or {}})
    rows.sort(key=lambda r: r["strike"])
    return spot, rows


def parse_candles(data):
    ts = data.get("timestamp") or []
    cols = [data.get(k) or [] for k in ("open", "high", "low", "close")]
    n = min([len(ts)] + [len(c) for c in cols])
    times = [datetime.fromtimestamp(num(t), IST) for t in ts[:n]]
    # Some Dhan responses carry IST wall-clock time as if it were UTC; a session
    # that seems to open after 14:00 IST is one of those.
    if times and min(minute_of(t) for t in times) >= 14 * 60:
        times = [datetime.fromtimestamp(num(t), timezone.utc).replace(tzinfo=IST) for t in ts[:n]]
    return [{"t": times[i], "o": num(cols[0][i]), "h": num(cols[1][i]),
             "l": num(cols[2][i]), "c": num(cols[3][i])} for i in range(n)]


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token, chat_id, dry_run=False, opener=None):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run or not (token and chat_id)
        self.opener = opener or urllib.request.urlopen
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        stamp = now_ist().strftime("%H:%M:%S")
        print("\n[%s] %s\n" % (stamp, text))
        if self.dry_run:
            return True
        body = json.dumps({"chat_id": self.chat_id, "text": text,
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(TELEGRAM % (self.token, "sendMessage"), data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(req, timeout=20) as r:
                return json.loads(r.read().decode()).get("ok", False)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print("telegram send failed: %s" % e, file=sys.stderr)
            return False


# ---------------------------------------------------------------- the rules

def opening_range(bars, day, minutes, now):
    start = at(day, 9 * 60 + 15)
    end = start + timedelta(minutes=minutes)
    inside = [b for b in bars if start <= b["t"] < end]
    if len(inside) < minutes // 5 or end > now:
        return None
    return {"high": max(b["h"] for b in inside), "low": min(b["l"] for b in inside), "end": end}


def first_breaks(bars, rng, now):
    """First completed candle closing above the range (CE) and below it (PE)."""
    out = {}
    for b in bars:
        if b["t"] < rng["end"] or b["t"] + BAR > now:
            continue
        if b["c"] > rng["high"] and "CE" not in out:
            out["CE"] = b
        elif b["c"] < rng["low"] and "PE" not in out:
            out["PE"] = b
    return out


def oi_confirmation(rows, spot, n, side):
    """Day's OI change in the n strikes on each side of ATM.

    Bullish (CE) wants put writers adding more than call writers; bearish (PE)
    the reverse. Returns (ratio, call change, put change); ratio is supportive
    over opposing change, inf when the opposing side is unwinding."""
    if not rows:
        return 0.0, 0, 0
    atm = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
    near = rows[max(0, atm - n): atm + n + 1]
    ce = sum(num(r["ce"].get("oi")) - num(r["ce"].get("previous_oi")) for r in near)
    pe = sum(num(r["pe"].get("oi")) - num(r["pe"].get("previous_oi")) for r in near)
    support, oppose = (pe, ce) if side == "CE" else (ce, pe)
    if support <= 0:
        return 0.0, ce, pe
    if oppose <= 0:
        return float("inf"), ce, pe
    return support / oppose, ce, pe


def pick_strike(rows, spot, side, max_spread_pct):
    """ATM strike, else one strike in the money, with a live and tight quote."""
    if not rows:
        return None, "empty option chain"
    atm = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
    itm = atm - 1 if side == "CE" else atm + 1
    reason = "no quote"
    for i in (atm, itm):
        if not 0 <= i < len(rows):
            continue
        leg = rows[i]["ce" if side == "CE" else "pe"]
        ltp, ask, bid = num(leg.get("last_price")), num(leg.get("top_ask_price")), num(leg.get("top_bid_price"))
        if ltp <= 0 or ask <= 0 or bid <= 0:
            reason = "no bid/ask at %g" % rows[i]["strike"]
            continue
        spread = (ask - bid) / ask * 100
        if spread > max_spread_pct:
            reason = "spread %.1f%% at %g" % (spread, rows[i]["strike"])
            continue
        return rows[i], None
    return None, reason


def build_plan(entry, delta, spot_now, spot_sl, cfg):
    """Option SL from the spot invalidation level through delta, clamped to
    sl_min_pct..sl_max_pct of the premium; targets at multiples of the risk."""
    delta = abs(delta) if delta else 0.5
    raw = entry - delta * abs(spot_now - spot_sl)
    lo = entry * (1 - cfg["sl_max_pct"] / 100.0)
    hi = entry * (1 - cfg["sl_min_pct"] / 100.0)
    sl = tick(min(max(raw, lo), hi))
    risk = round(entry - sl, 2)
    targets = [tick(entry + r * risk) for r in cfg["targets_r"]]
    return {"entry": tick(entry), "sl": sl, "risk": risk, "targets": targets,
            "sl_basis": "delta" if lo <= raw <= hi else ("capped %d%%" % cfg["sl_max_pct"] if raw < lo
                                                         else "floor %d%%" % cfg["sl_min_pct"])}


def fmt_expiry(e):
    try:
        return datetime.strptime(e, "%Y-%m-%d").strftime("%d-%b")
    except ValueError:
        return e


def signal_message(s):
    t = s["targets"]
    lines = [
        "%s BUY %s %g %s  (%s expiry)" % ("🟢" if s["side"] == "CE" else "🔴", s["underlying"], s["strike"],
                                          s["side"], fmt_expiry(s["expiry"])),
        "Entry: %.2f (ask at %s)" % (s["entry"], s["time"]),
        "SL: %.2f  (risk %.2f/unit = 1R, %s)" % (s["sl"], s["risk"], s["sl_basis"]),
    ]
    for i, tgt in enumerate(t):
        lines.append("T%d: %.2f  (%gR)" % (i + 1, tgt, s["targets_r"][i]))
    if s.get("trail"):
        lines.append("After T1: SL moves to entry.")
    lines += [
        "",
        "Why: %s candle closed %.2f, %s the opening range %.2f-%.2f (%.2f%% wide)." % (
            s["bar_time"], s["bar_close"], "above" if s["side"] == "CE" else "below",
            s["or_low"], s["or_high"], s["or_pct"]),
        "OI near ATM today: calls %s, puts %s (ratio %s). Delta %.2f, IV %.1f%%." % (
            lakh(s["ce_oi_chg"]), lakh(s["pe_oi_chg"]),
            "inf" if s["oi_ratio"] == float("inf") else "%.1f" % s["oi_ratio"], s["delta"], s["iv"]),
        "Spot invalidation: %.2f (range midpoint). Square off by %s." % (s["spot_sl"], s["square_off"]),
        "",
        "Rule-based signal, not advice. Paper trade it first.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(self, cfg, client, notifier, workdir):
        self.cfg = cfg
        self.client = client
        self.tg = notifier
        self.workdir = workdir
        self.unds = dict(UNDERLYINGS)
        self.unds.update(cfg.get("custom_underlyings") or {})
        self.state = None

    # state -------------------------------------------------------
    def state_path(self, day):
        return os.path.join(self.workdir, "state", day.strftime("%Y-%m-%d") + ".json")

    def load_state(self, now):
        day = now.strftime("%Y-%m-%d")
        if not self.state or self.state.get("day") != day:
            self.state = load_json(self.state_path(now), None) or {
                "day": day, "signals": [], "evaluated": {}, "expiries": {}, "flags": {}}
        return self.state

    def save_state(self, now):
        save_json(self.state_path(now), self.state)

    def log(self, msg):
        print("%s  %s" % (now_ist().strftime("%H:%M:%S"), msg))

    # one pass ----------------------------------------------------
    def cycle(self, now):
        st = self.load_state(now)
        any_bars = False
        for name in self.cfg["underlyings"]:
            u = self.unds.get(name)
            if not u:
                self.log("unknown underlying %s (add it under custom_underlyings)" % name)
                continue
            if isinstance(self.client, NseYahoo) and not u.get("nse"):
                if not st["flags"].get("unsupported:" + name):
                    st["flags"]["unsupported:" + name] = True
                    self.log("%s has no free option chain (BSE blocks automated access); skipped" % name)
                continue
            try:
                any_bars |= self.process(name, u, now)
            except DhanAuthError:
                raise
            except (DhanError, urllib.error.URLError, OSError, ValueError, KeyError) as e:
                self.log("%s: %s" % (name, e))
        if not any_bars and minute_of(now) >= 9 * 60 + 35 and not st["flags"].get("no_data"):
            st["flags"]["no_data"] = True
            self.tg.send("No index candles from Dhan today by %s. Market holiday, or a data problem. "
                         "Stopping for today." % now.strftime("%H:%M"))
        self.save_state(now)
        return any_bars

    def expiry_for(self, name, u, now):
        st = self.state
        if name not in st["expiries"]:
            st["expiries"][name] = self.client.expiries(u)
        today = now.strftime("%Y-%m-%d")
        upcoming = [e for e in st["expiries"][name] if e >= today]
        if not upcoming:
            return None
        if upcoming[0] == today and self.cfg["expiry_day"] == "next" and len(upcoming) > 1:
            return upcoming[1]
        return upcoming[0]

    def process(self, name, u, now):
        cfg, st = self.cfg, self.state
        bars = [b for b in self.client.candles(u, now) if b["t"].date() == now.date()]
        if not bars:
            return False
        chains = {}

        def chain(expiry):
            if expiry not in chains:
                chains[expiry] = self.client.chain(u, expiry)
                if cfg.get("record_chains"):
                    self.record(name, expiry, now, *chains[expiry])
            return chains[expiry]

        if cfg.get("record_chains") and minute_of(now) <= 15 * 60 + 30:
            expiry = self.expiry_for(name, u, now)
            if expiry:
                chain(expiry)
        self.track(name, u, now, chain)

        if any(s["underlying"] == name for s in st["signals"]):
            return True
        if len(st["signals"]) >= cfg["max_signals_per_day"]:
            return True
        rng = opening_range(bars, now, cfg["opening_range_minutes"], now)
        if not rng:
            return True
        for side, bar in sorted(first_breaks(bars, rng, now).items(), key=lambda kv: kv[1]["t"]):
            key = "%s:%s" % (name, side)
            if key in st["evaluated"]:
                continue
            bar_end = bar["t"] + BAR
            if now - bar_end > timedelta(minutes=cfg["fresh_minutes"]):
                st["evaluated"][key] = "stale: %s candle" % bar["t"].strftime("%H:%M")
                continue
            if not hm(cfg["first_entry"]) <= minute_of(bar_end) <= hm(cfg["last_entry"]):
                st["evaluated"][key] = "outside entry window"
                continue
            st["evaluated"][key] = self.evaluate(name, u, now, side, bar, rng, chain)
            self.log("%s %s: %s" % (name, side, st["evaluated"][key]))
            if st["evaluated"][key] != "signal" and cfg["notify_skips"]:
                self.tg.send("%s %s breakout skipped: %s" % (name, side, st["evaluated"][key]))
            if st["evaluated"][key] == "signal":
                break
        return True

    def record(self, name, expiry, now, spot, rows, width=15):
        """Append the chain near ATM to fno/record/<date>/<name>.jsonl.gz, one line per poll."""
        if not rows:
            return
        atm = min(range(len(rows)), key=lambda i: abs(rows[i]["strike"] - spot))
        keep = []
        for r in rows[max(0, atm - width): atm + width + 1]:
            line = [r["strike"]]
            for leg in (r["ce"], r["pe"]):
                line += [num(leg.get(k)) for k in ("last_price", "top_bid_price", "top_ask_price", "oi",
                                                   "previous_oi", "implied_volatility", "volume")]
            keep.append(line)
        folder = os.path.join(self.workdir, "record", now.strftime("%Y-%m-%d"))
        os.makedirs(folder, exist_ok=True)
        with gzip.open(os.path.join(folder, "%s.jsonl.gz" % name), "at") as f:
            f.write(json.dumps({"t": now.strftime("%H:%M:%S"), "spot": spot, "expiry": expiry,
                                "cols": "strike,ce[ltp,bid,ask,oi,prev_oi,iv,vol],pe[same]",
                                "rows": keep}) + "\n")

    def evaluate(self, name, u, now, side, bar, rng, chain):
        cfg = self.cfg
        mid = (rng["high"] + rng["low"]) / 2
        width = (rng["high"] - rng["low"]) / mid * 100
        if width < cfg["min_range_pct"]:
            return "range too narrow (%.2f%%)" % width
        if width > cfg["max_range_pct"]:
            return "range too wide (%.2f%%)" % width
        expiry = self.expiry_for(name, u, now)
        if not expiry:
            return "no expiry"
        spot, rows = chain(expiry)
        spot = spot or bar["c"]
        if (side == "CE" and spot <= rng["high"]) or (side == "PE" and spot >= rng["low"]):
            return "spot %.2f already back inside the range" % spot
        ratio, ce_chg, pe_chg = oi_confirmation(rows, spot, cfg["oi_strikes"], side)
        if ratio < cfg["min_oi_ratio"]:
            return "OI does not confirm (calls %s, puts %s)" % (lakh(ce_chg), lakh(pe_chg))
        row, why = pick_strike(rows, spot, side, cfg["max_spread_pct"])
        if not row:
            return "no tradable strike: %s" % why
        leg = row["ce" if side == "CE" else "pe"]
        entry = num(leg.get("top_ask_price")) or num(leg.get("last_price"))
        delta = num((leg.get("greeks") or {}).get("delta"), 0.5)
        plan = build_plan(entry, delta, spot, mid, cfg)
        if plan["risk"] <= 0:
            return "premium too small to plan"
        sig = {
            "id": "%s-%s-%s" % (self.state["day"], name, side), "underlying": name, "side": side,
            "strike": row["strike"], "expiry": expiry, "time": now.strftime("%H:%M"),
            "bar_time": bar["t"].strftime("%H:%M"), "bar_close": bar["c"],
            "or_high": rng["high"], "or_low": rng["low"], "or_pct": width,
            "spot": spot, "spot_sl": round(mid, 2), "delta": abs(delta),
            "iv": num(leg.get("implied_volatility")), "oi_ratio": ratio,
            "ce_oi_chg": ce_chg, "pe_oi_chg": pe_chg, "targets_r": cfg["targets_r"],
            "trail": cfg["trail_to_entry_after_t1"], "square_off": cfg["square_off"],
            "status": "open", "hits": [], "stop": plan["sl"], "updates": [],
        }
        sig.update(plan)
        self.state["signals"].append(sig)
        self.tg.send(signal_message(sig))
        return "signal"

    def track(self, name, u, now, chain):
        cfg = self.cfg
        open_sigs = [s for s in self.state["signals"] if s["underlying"] == name and s["status"] == "open"]
        for s in open_sigs:
            _, rows = chain(s["expiry"])
            row = next((r for r in rows if r["strike"] == s["strike"]), None)
            if not row:
                continue
            ltp = num(row["ce" if s["side"] == "CE" else "pe"].get("last_price"))
            if ltp <= 0:
                continue
            s["last"] = ltp
            label = "%s %g %s" % (name, s["strike"], s["side"])
            if minute_of(now) >= hm(cfg["square_off"]):
                self.close(s, ltp, "time exit at %s" % now.strftime("%H:%M"), label)
                continue
            if ltp <= s["stop"]:
                why = "SL hit" if s["stop"] < s["entry"] else "stopped at entry after T1"
                self.close(s, ltp, why, label)
                continue
            for i, tgt in enumerate(s["targets"]):
                if i in s["hits"] or ltp < tgt:
                    continue
                s["hits"].append(i)
                if i == len(s["targets"]) - 1:
                    self.close(s, ltp, "T%d hit" % (i + 1), label)
                    break
                msg = "🎯 %s: T%d %.2f hit (LTP %.2f, %+.1fR)." % (
                    label, i + 1, tgt, ltp, (ltp - s["entry"]) / s["risk"])
                if i == 0 and s["trail"]:
                    s["stop"] = s["entry"]
                    msg += " SL moves to entry %.2f." % s["entry"]
                self.tg.send(msg)

    def close(self, s, ltp, why, label):
        s["status"] = "closed"
        s["exit"] = ltp
        s["result_r"] = round((ltp - s["entry"]) / s["risk"], 2)
        s["exit_reason"] = why
        icon = "✅" if s["result_r"] > 0 else ("⚪" if s["result_r"] == 0 else "❌")
        self.tg.send("%s %s: %s. Exit %.2f vs entry %.2f = %+.2f/unit (%+.2fR)." % (
            icon, label, why, ltp, s["entry"], ltp - s["entry"], s["result_r"]))
        with open(os.path.join(self.workdir, "journal.jsonl"), "a") as f:
            f.write(json.dumps({k: s[k] for k in s if k != "updates"}, default=str) + "\n")

    def summary(self, now):
        st = self.load_state(now)
        if st["flags"].get("summary"):
            return
        st["flags"]["summary"] = True
        sigs = st["signals"]
        if not sigs:
            self.tg.send("F&O alerts %s: no signals today." % st["day"])
        else:
            lines = ["F&O alerts %s: %d signal(s)" % (st["day"], len(sigs))]
            total = 0.0
            for s in sigs:
                r = s.get("result_r")
                total += r or 0
                lines.append("%s %g %s @ %.2f: %s" % (
                    s["underlying"], s["strike"], s["side"], s["entry"],
                    "%s, %+.2fR" % (s["exit_reason"], r) if r is not None else "still open, LTP %s" % s.get("last")))
            lines.append("Total: %+.2fR" % total)
            self.tg.send("\n".join(lines))
        self.save_state(now)


# ---------------------------------------------------------------- commands

def make(args, need_dhan=True):
    workdir = os.path.abspath(args.dir)
    cfg = load_config(workdir)
    sec = load_secrets(workdir)
    dhan = cfg.get("data_source") == "dhan"
    if need_dhan and dhan and not (sec["DHAN_CLIENT_ID"] and sec["DHAN_ACCESS_TOKEN"]):
        sys.exit("Dhan credentials missing: set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN, or fill %s"
                 % os.path.join(workdir, "secrets.json"))
    tg = Telegram(sec["TELEGRAM_BOT_TOKEN"], sec["TELEGRAM_CHAT_ID"], dry_run=getattr(args, "dry_run", False))
    client = Dhan(sec["DHAN_CLIENT_ID"], sec["DHAN_ACCESS_TOKEN"]) if dhan else NseYahoo()
    return workdir, cfg, sec, tg, client


def cmd_init(args):
    workdir = os.path.abspath(args.dir)
    os.makedirs(workdir, exist_ok=True)
    cpath = os.path.join(workdir, "config.json")
    if not os.path.exists(cpath):
        save_json(cpath, DEFAULT_CONFIG)
        print("wrote", cpath)
    else:
        print("kept", cpath)
    epath = os.path.join(workdir, "secrets.example.json")
    save_json(epath, {k: "" for k in SECRET_KEYS})
    print("wrote", epath)
    print("Copy it to secrets.json, fill it in yourself, then: chmod 600 %s" % os.path.join(workdir, "secrets.json"))


def cmd_set_secret(args):
    import getpass
    workdir = os.path.abspath(args.dir)
    path = os.path.join(workdir, "secrets.json")
    if args.key not in SECRET_KEYS:
        sys.exit("key must be one of: %s" % ", ".join(SECRET_KEYS))
    value = getpass.getpass("%s (hidden, paste then Enter): " % args.key).strip()
    if not value:
        sys.exit("nothing entered, file unchanged")
    data = {k: "" for k in SECRET_KEYS}
    data.update(load_json(path, {}))
    data[args.key] = value
    save_json(path, data)
    os.chmod(path, 0o600)
    print("saved %s (%d characters) to %s" % (args.key, len(value), path))


def cmd_check(args):
    _, cfg, _, _, client = make(args)
    u = UNDERLYINGS["NIFTY"]
    try:
        ex = client.expiries(u)
        spot, rows = client.chain(u, [e for e in ex if e >= now_ist().strftime("%Y-%m-%d")][0])
        bars = client.candles(u, now_ist())
    except (DhanError, urllib.error.URLError, IndexError) as e:
        sys.exit(str(e))
    print("%s OK. NIFTY spot %.2f, %d strikes, expiries %s" % (
        cfg.get("data_source", "nse"), spot, len(rows), ", ".join(ex[:4])))
    if bars:
        b = bars[-1]
        print("Last 5-min candle %s  O %.2f H %.2f L %.2f C %.2f" % (
            b["t"].strftime("%Y-%m-%d %H:%M"), b["o"], b["h"], b["l"], b["c"]))


def cmd_chat_id(args):
    sec = load_secrets(os.path.abspath(args.dir))
    if not sec["TELEGRAM_BOT_TOKEN"]:
        sys.exit("TELEGRAM_BOT_TOKEN missing")
    with urllib.request.urlopen(TELEGRAM % (sec["TELEGRAM_BOT_TOKEN"], "getUpdates"), timeout=20) as r:
        data = json.loads(r.read().decode())
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        c = msg.get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = c.get("title") or c.get("username") or c.get("first_name")
    if not chats:
        print("No messages yet. Send any message to your bot in Telegram, then run this again.")
    for cid, who in chats.items():
        print("chat id %s  (%s)" % (cid, who))


def cmd_test_telegram(args):
    _, _, sec, tg, _ = make(args, need_dhan=False)
    if tg.dry_run:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
    ok = tg.send("✅ F&O alerts connected. Signals will arrive here on market days.")
    print("sent" if ok else "failed")


def cmd_status(args):
    workdir = os.path.abspath(args.dir)
    st = load_json(os.path.join(workdir, "state", now_ist().strftime("%Y-%m-%d") + ".json"), None)
    if not st:
        print("No state for today.")
        return
    for s in st["signals"]:
        print("%s %s %g %s entry %.2f SL %.2f T %s  %s %s" % (
            s["time"], s["underlying"], s["strike"], s["side"], s["entry"], s["stop"],
            "/".join("%.2f" % t for t in s["targets"]), s["status"],
            "%s %+.2fR" % (s.get("exit_reason", ""), s["result_r"]) if "result_r" in s else ""))
    for k, v in st["evaluated"].items():
        print("  %-16s %s" % (k, v))


def cmd_run(args):
    workdir, cfg, _, tg, client = make(args)
    eng = Engine(cfg, client, tg, workdir)
    print("Watching %s. Data dir %s. %s" % (", ".join(cfg["underlyings"]), workdir,
                                             "DRY RUN (no Telegram)" if tg.dry_run else "Telegram on"))
    while True:
        now = now_ist()
        if now.weekday() >= 5:
            print("Weekend. Nothing to watch.")
            return
        m = minute_of(now)
        if m >= 15 * 60 + 35:
            eng.summary(now)
            print("Market closed for today.")
            return
        if m < 9 * 60 + 15:
            if args.once:
                print("Before 09:15 IST.")
                return
            time.sleep(min(300, (9 * 60 + 15 - m) * 60 - now.second + 1))
            continue
        try:
            eng.cycle(now)
        except DhanAuthError as e:
            tg.send("⚠️ F&O alerts stopped: %s\nGenerate a new Dhan access token, update secrets, restart." % e)
            return
        if eng.state["flags"].get("no_data") or args.once:
            return
        time.sleep(cfg["poll_seconds"])


def main(argv=None):
    p = argparse.ArgumentParser(description="India F&O rule-based trade alerts on Telegram (Dhan data).")
    p.add_argument("--dir", default=os.environ.get("FNO_DIR", "fno"), help="data folder (default ./fno)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("check")
    sub.add_parser("telegram-chat-id")
    sub.add_parser("test-telegram")
    sub.add_parser("status")
    ss = sub.add_parser("set-secret")
    ss.add_argument("key", help="DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    args = p.parse_args(argv)
    {"init": cmd_init, "check": cmd_check, "telegram-chat-id": cmd_chat_id,
     "test-telegram": cmd_test_telegram, "status": cmd_status, "set-secret": cmd_set_secret,
     "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
