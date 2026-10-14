"""Side-effecting services: recurring payment generation, price fetching, bank holiday sync, Trading 212 CSV import."""
import csv
import io
import json
import logging
import re
import time
from datetime import date, datetime, timedelta

import httpx

from . import calc
from .db import get_setting, rows, set_setting

log = logging.getLogger("finance")
UA = {"User-Agent": "Mozilla/5.0 (finance-tracker self-hosted)"}


def calendar_for(con):
    return calc.Calendar(get_setting(con, "bank_holidays_extra"))


# ---------------------------------------------------------------- recurring payments

def amount_on(amounts, x):
    val = None
    for a in sorted(amounts, key=lambda a: a["effective_from"]):
        if calc.d(a["effective_from"]) <= x:
            val = a["amount"]
    return val


def schedule_dates(con, rec, until):
    cal = calendar_for(con)
    start = calc.d(rec["start_date"])
    end = calc.d(rec["end_date"]) if rec.get("end_date") else None
    out = []
    for y, m in calc.months_between(start, until):
        pd = cal.pay_date(y, m, rec["rule"], rec["day"])
        if pd < start or pd > until or (end and pd > end):
            continue
        out.append((f"{y:04d}-{m:02d}", pd))
    return out


def generate_recurring(con, today=None):
    """Create any due recurring transactions up to today. Idempotent."""
    today = today or date.today()
    created = 0
    for rec in rows(con.execute("SELECT * FROM recurring WHERE active=1")):
        amounts = rows(con.execute("SELECT * FROM recurring_amounts WHERE recurring_id=?", (rec["id"],)))
        skips = {r["recurring_key"] for r in con.execute("SELECT recurring_key FROM recurring_skips WHERE recurring_id=?", (rec["id"],))}
        for key, pd in schedule_dates(con, rec, today):
            if key in skips:
                continue
            amt = amount_on(amounts, pd)
            if not amt:
                continue
            cur = con.execute(
                "INSERT OR IGNORE INTO transactions(account_id,date,type,amount,notes,recurring_id,recurring_key) VALUES(?,?,?,?,?,?,?)",
                (rec["account_id"], pd.isoformat(), rec["txn_type"], round(amt, 2), rec["label"], rec["id"], key))
            created += cur.rowcount
            if rec.get("tax_relief_pct"):
                cur = con.execute(
                    "INSERT OR IGNORE INTO transactions(account_id,date,type,amount,notes,recurring_id,recurring_key) VALUES(?,?,?,?,?,?,?)",
                    (rec["account_id"], pd.isoformat(), "tax_relief", round(amt * rec["tax_relief_pct"] / 100, 2),
                     f"{rec['label']} – tax relief", rec["id"], key + ":relief"))
                created += cur.rowcount
        if rec.get("invest_holding_id"):
            created += _auto_invest(con, rec, skips)
    return created


INVEST_PRICE_WINDOW = 14  # days either side of the payment we'll accept a fund price from


def price_near(con, holding_id, day):
    """Fund price on the payment date or the closest earlier day; else the closest later one (within the window)."""
    lo = (day - timedelta(days=INVEST_PRICE_WINDOW)).isoformat()
    hi = (day + timedelta(days=INVEST_PRICE_WINDOW)).isoformat()
    r = con.execute("SELECT date, price FROM prices WHERE holding_id=? AND date<=? AND date>=? ORDER BY date DESC LIMIT 1",
                    (holding_id, day.isoformat(), lo)).fetchone()
    if not r:
        r = con.execute("SELECT date, price FROM prices WHERE holding_id=? AND date>? AND date<=? ORDER BY date LIMIT 1",
                        (holding_id, day.isoformat(), hi)).fetchone()
    return (r["date"], r["price"]) if r and r["price"] > 0 else (None, None)


def _auto_invest(con, rec, skips):
    """Turn each of this schedule's contributions (and tax relief) into a buy of the chosen fund.
    If no price exists yet for that date, it's left as cash and retried on the next run."""
    hid = rec["invest_holding_id"]
    if not con.execute("SELECT 1 FROM holdings WHERE id=?", (hid,)).fetchone():
        return 0
    paid = rows(con.execute(
        "SELECT * FROM transactions WHERE recurring_id=? AND recurring_key NOT LIKE '%:buy' AND type IN "
        "('contribution','deposit','tax_relief')", (rec["id"],)))
    made = 0
    for t in paid:
        bkey = t["recurring_key"] + ":buy"
        if bkey in skips:
            continue
        if con.execute("SELECT 1 FROM transactions WHERE recurring_id=? AND recurring_key=?", (rec["id"], bkey)).fetchone():
            continue
        pdate, price = price_near(con, hid, calc.d(t["date"]))
        if not price:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions(account_id,date,type,amount,holding_id,units,notes,recurring_id,recurring_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (t["account_id"], t["date"], "buy", t["amount"], hid, round(t["amount"] / price, 6),
             f"Auto-invest @ £{price:.4f} ({pdate})", rec["id"], bkey))
        made += cur.rowcount
    return made


# ---------------------------------------------------------------- automatic daily interest

AUTO_INT_PREFIX = "auto-interest:"
DAILY_NOTE = "Daily interest (auto)"


def generate_auto_interest(con, today=None):
    """For accounts set to daily interest: credit interest for every day from interest_auto_from to yesterday,
    compounding (each day's interest is added to the cash balance before the next day is calculated).

    Matches Trading 212's published method: daily payment = cash x ((1 + AER)^(1/365.25) - 1), rounded down to the
    penny with the remainder carried to the next day. One 'interest' transaction per day, so the balance grows
    day by day; the UI groups them by month. Rate changes are taken from the account's rate history."""
    today = today or date.today()
    end = today - timedelta(days=1)  # interest for a day is paid once that day is over
    changed = 0
    for a in rows(con.execute("SELECT * FROM accounts")):
        existing = {r["external_id"]: r for r in rows(con.execute(
            "SELECT * FROM transactions WHERE account_id=? AND external_id LIKE ?", (a["id"], AUTO_INT_PREFIX + "%")))}
        if a.get("interest_mode") != "daily" or not a.get("interest_auto_from") or a["valuation_mode"] != "holdings":
            if existing:  # switched off: auto entries are derived data, remove them
                con.execute("DELETE FROM transactions WHERE account_id=? AND external_id LIKE ?", (a["id"], AUTO_INT_PREFIX + "%"))
                changed += len(existing)
            continue
        start = calc.d(a["interest_auto_from"])
        rates = rows(con.execute("SELECT * FROM rates WHERE account_id=? AND kind='interest' ORDER BY effective_date, id", (a["id"],)))
        txns = rows(con.execute(
            "SELECT * FROM transactions WHERE account_id=? AND (external_id IS NULL OR external_id NOT LIKE ?) ORDER BY date, id",
            (a["id"], AUTO_INT_PREFIX + "%")))
        cash, i, carry = 0.0, 0, 0.0
        daily = {}  # 'YYYY-MM-DD' -> amount credited that day
        x = start
        while i < len(txns) and calc.d(txns[i]["date"]) < start:
            cash += calc.cash_effect(txns[i]); i += 1
        while x <= end:
            while i < len(txns) and calc.d(txns[i]["date"]) <= x:
                cash += calc.cash_effect(txns[i]); i += 1
            aer = calc.rate_on(rates, "interest", x) or 0.0
            carry += max(cash, 0) * calc.daily_rate(aer)
            pay = int(carry * 100 + 1e-9) / 100  # round down to the penny, keep the rest for tomorrow
            carry -= pay
            cash += pay
            if pay > 0:
                daily[x.isoformat()] = round(pay, 2)
            x += timedelta(days=1)
        for day, amt in daily.items():
            ext = AUTO_INT_PREFIX + day
            old = existing.pop(ext, None)
            if old:
                if abs(old["amount"] - amt) > 0.004 or old["date"] != day:
                    con.execute("UPDATE transactions SET amount=?, date=?, notes=? WHERE id=?", (amt, day, DAILY_NOTE, old["id"]))
                    changed += 1
            else:
                con.execute("INSERT INTO transactions(account_id,date,type,amount,notes,external_id) VALUES(?,?,'interest',?,?,?)",
                            (a["id"], day, amt, DAILY_NOTE, ext))
                changed += 1
        for old in existing.values():  # days no longer in range (start moved later) and old monthly entries
            con.execute("DELETE FROM transactions WHERE id=?", (old["id"],)); changed += 1
    return changed


# ---------------------------------------------------------------- automatic dividends (+ reinvestment)

AUTO_DIV_PREFIX = "auto-div:"
AUTO_DRIP_PREFIX = "auto-drip:"


def _units_before(con, hid, day):
    """Units held at the close of the day before `day` – who qualifies for a dividend with that ex-date."""
    r = con.execute(
        "SELECT COALESCE(SUM(CASE type WHEN 'buy' THEN units WHEN 'unit_adjust' THEN units WHEN 'sell' THEN -units ELSE 0 END),0) u "
        "FROM transactions WHERE holding_id=? AND date<? AND units IS NOT NULL", (hid, day)).fetchone()
    return r["u"] or 0.0


def generate_auto_dividends(con, today=None):
    """For holdings set to 'cash' or 'reinvest': one dividend per ex-date = units held x amount per share
    (rounded down to the penny). 'reinvest' also buys units with it at that day's price, so later dividends are
    paid on the extra units too. Recalculated in date order every run, so back-dated buys/sells flow through."""
    today = (today or date.today()).isoformat()
    changed = 0
    for h in rows(con.execute("SELECT * FROM holdings")):
        auto = {r["external_id"]: r for r in rows(con.execute(
            "SELECT * FROM transactions WHERE holding_id=? AND (external_id LIKE ? OR external_id LIKE ?)",
            (h["id"], AUTO_DIV_PREFIX + "%", AUTO_DRIP_PREFIX + "%")))}
        mode = h.get("dividend_mode") or "none"
        if mode not in ("cash", "reinvest"):
            if auto:
                con.execute("DELETE FROM transactions WHERE holding_id=? AND (external_id LIKE ? OR external_id LIKE ?)",
                            (h["id"], AUTO_DIV_PREFIX + "%", AUTO_DRIP_PREFIX + "%"))
                changed += len(auto)
            continue
        since = h.get("dividends_auto_from") or "0000-00-00"
        events = rows(con.execute("SELECT * FROM dividend_events WHERE holding_id=? AND ex_date>=? AND ex_date<=? ORDER BY ex_date",
                                  (h["id"], since, today)))
        keep = set()
        for ev in events:
            ex = ev["ex_date"]
            dkey, bkey = f"{AUTO_DIV_PREFIX}{h['id']}:{ex}", f"{AUTO_DRIP_PREFIX}{h['id']}:{ex}"
            units = _units_before(con, h["id"], ex)
            amt = int(max(units, 0) * ev["per_share"] * 100 + 1e-9) / 100
            if amt <= 0:
                continue
            note = f"{h['ticker']} dividend: {units:g} units × £{ev['per_share']:.4f} (auto)"
            changed += _upsert_auto(con, auto.get(dkey), dkey, h["account_id"], ex, "dividend", amt, h["id"], None, note)
            keep.add(dkey)
            if mode == "reinvest":
                pdate, price = price_near(con, h["id"], calc.d(ex))
                if price:
                    changed += _upsert_auto(con, auto.get(bkey), bkey, h["account_id"], ex, "buy", amt, h["id"],
                                            round(amt / price, 6), f"Dividend reinvested @ £{price:.4f} ({pdate}, auto)")
                    keep.add(bkey)
        for ext, old in auto.items():  # events no longer applicable (start date moved, mode changed to cash, …)
            if ext not in keep:
                con.execute("DELETE FROM transactions WHERE id=?", (old["id"],)); changed += 1
    return changed


def _upsert_auto(con, old, ext, account_id, day, ttype, amount, hid, units, note):
    if old:
        if abs(old["amount"] - amount) > 0.004 or (units is not None and abs((old["units"] or 0) - units) > 1e-7) or old["notes"] != note:
            con.execute("UPDATE transactions SET amount=?, units=?, notes=?, date=? WHERE id=?", (amount, units, note, day, old["id"]))
            return 1
        return 0
    con.execute("INSERT INTO transactions(account_id,date,type,amount,holding_id,units,notes,external_id) VALUES(?,?,?,?,?,?,?,?)",
                (account_id, day, ttype, amount, hid, units, note, ext))
    return 1


def run_generators(con):
    """Everything derived automatically: recurring payments (+ auto-invest), dividends (+ reinvest), daily interest.
    Dividends run before interest so cash dividends earn interest from the day they arrive."""
    n = generate_recurring(con)
    n += generate_auto_dividends(con)
    n += generate_auto_interest(con)
    return n


def upcoming(con, months=12, include_hidden=True):
    today = date.today()
    out = []
    for rec in rows(con.execute("SELECT r.*, a.name AS account_name FROM recurring r JOIN accounts a ON a.id=r.account_id "
                                "WHERE r.active=1 AND a.archived=0" + ("" if include_hidden else " AND a.hidden=0"))):
        amounts = rows(con.execute("SELECT * FROM recurring_amounts WHERE recurring_id=?", (rec["id"],)))
        horizon = today + timedelta(days=31 * months)
        for key, pd in schedule_dates(con, rec, horizon):
            if pd <= today:
                continue
            out.append({"recurring_id": rec["id"], "label": rec["label"], "account_id": rec["account_id"],
                        "account_name": rec["account_name"], "date": pd.isoformat(), "amount": amount_on(amounts, pd),
                        "type": rec["txn_type"]})
    return sorted(out, key=lambda r: r["date"])


# ---------------------------------------------------------------- prices (Yahoo Finance chart API)

def _fx_to_gbp(client, currency):
    if currency in ("GBP", "GBp", "GBX"):
        return 0.01 if currency in ("GBp", "GBX") else 1.0
    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{currency}GBP=X", params={"range": "5d", "interval": "1d"}, headers=UA)
    r.raise_for_status()
    return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])


class FxSeries:
    """Daily <currency> -> GBP rates, so each day's price/dividend is converted at that day's rate
    (not today's). Pence lines are a fixed 1/100; GBP is 1."""

    def __init__(self, client, currency, rng):
        self.fixed = 0.01 if currency in ("GBp", "GBX") else (1.0 if currency == "GBP" else None)
        self.days, self.rates = [], []
        if self.fixed is not None:
            return
        r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{currency.upper()}GBP=X",
                       params={"range": rng, "interval": "1d"}, headers=UA)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        pts = sorted((datetime.utcfromtimestamp(t).date().isoformat(), c)
                     for t, c in zip(res.get("timestamp") or [], (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or [])
                     if c)
        self.days, self.rates = [p[0] for p in pts], [p[1] for p in pts]
        self.latest = float(res["meta"].get("regularMarketPrice") or (self.rates[-1] if self.rates else 0))
        if not self.latest:
            raise ValueError(f"no {currency}/GBP exchange rate available")

    def on(self, day):
        """Rate for `day`: that day's close, else the last earlier one, else the first available."""
        if self.fixed is not None:
            return self.fixed
        import bisect
        i = bisect.bisect_right(self.days, day) - 1
        if i >= 0:
            return self.rates[i]
        return self.rates[0] if self.rates else self.latest


def _set_price_info(con, holding_id, info):
    con.execute("UPDATE holdings SET price_info=? WHERE id=?", (info, holding_id))


def _store_price(con, holding_id, day, price):
    con.execute("INSERT INTO prices(holding_id,date,price,source) VALUES(?,?,?,'auto') "
                "ON CONFLICT(holding_id,date) DO UPDATE SET price=excluded.price WHERE source='auto'",
                (holding_id, day, round(price, 6)))


# ---------------------------------------------------------------- prices (FT.com, for funds without a ticker)
# Pension and unit-trust funds (e.g. Aviva pension funds) have no exchange ticker but FT publishes a daily
# price by ISIN. Use the ISIN as the price symbol: "GB00B0ZDNB53" (or "GB00B0ZDNB53:GBP" / "ft:GB00B0ZDNB53").
# FT has no official API, so this reads its public pages – if FT changes them, fetching stops with a logged error.

ISIN_SYMBOL = re.compile(r"^(?:ft:)?([A-Z]{2}[A-Z0-9]{9}[0-9])(?::([A-Z]{3}))?$", re.I)
FT_BASE = "https://markets.ft.com"
FT_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
              "Accept-Language": "en-GB,en;q=0.9"}


def is_ft_symbol(sym):
    return bool(ISIN_SYMBOL.match((sym or "").strip()))


def ft_parse_tearsheet(html):
    """-> (xid, price_currency, latest_price) from an FT fund tearsheet page."""
    m = re.search(r"xid(?:&quot;|\"):(?:&quot;|\")(\d+)", html)
    if not m:
        raise ValueError("FT page has no price data – check the ISIN")
    pm = re.search(r">Price \(([A-Za-z]{3})\)</span>\s*<span class=\"mod-ui-data-list__value\">([\d,.]+)<", html)
    cur = pm.group(1) if pm else None
    price = float(pm.group(2).replace(",", "")) if pm else None
    return m.group(1), cur, price


def ft_parse_history(html):
    """-> [(iso_date, close)] from FT's historical-prices table rows (date, open, high, low, close, volume)."""
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        dm = re.search(r">\w+day, (\w+ \d{1,2}, \d{4})<", row)
        nums = re.findall(r"<td>([\d,.]+)</td>", row)
        if not dm or len(nums) < 4:
            continue
        try:
            day = datetime.strptime(dm.group(1), "%B %d, %Y").date().isoformat()
            out.append((day, float(nums[3].replace(",", ""))))
        except ValueError:
            continue
    return out


def fetch_ft(con, holding, rng="5d"):
    m = ISIN_SYMBOL.match(holding["price_symbol"].strip())
    isin, cur = m.group(1).upper(), (m.group(2) or "GBP").upper()
    years = {"5d": 0, "1mo": 0, "1y": 1, "5y": 5, "max": 15}.get(rng, 1)
    with httpx.Client(timeout=30, follow_redirects=True, headers=FT_HEADERS) as client:
        r = client.get(f"{FT_BASE}/data/funds/tearsheet/historical", params={"s": f"{isin}:{cur}"})
        r.raise_for_status()
        xid, price_cur, latest = ft_parse_tearsheet(r.text)
        tm = re.search(r"<title>\s*(.*?),\s*[A-Z]{2}[A-Z0-9]{10}", r.text, re.S)
        _set_price_info(con, holding["id"], f"{tm.group(1).strip() if tm else isin} · FT.com · {price_cur or cur}")
        price_cur = price_cur or cur
        if price_cur in ("GBX", "GBp"):   # pence
            fx = 0.01
        elif price_cur.upper() == "GBP":
            fx = 1.0
        else:                              # other currency: convert at today's rate
            fx = _fx_to_gbp(client, price_cur.upper())
        points = {}
        end = date.today()
        chunks = max(1, years)  # one request per year of history; "5d" fetches the last ~3 weeks
        for i in range(chunks):
            start = end - timedelta(days=21 if years == 0 else 365)
            rr = client.get(f"{FT_BASE}/data/equities/ajax/get-historical-prices",
                            params={"startDate": start.strftime("%Y/%m/%d"), "endDate": end.strftime("%Y/%m/%d"), "symbol": xid})
            rr.raise_for_status()
            got = ft_parse_history(rr.json().get("html", ""))
            if not got:
                break  # reached the start of the fund's history
            points.update(got)
            end = start - timedelta(days=1)
    if latest and not points:
        points[date.today().isoformat()] = latest
    for day, close in points.items():
        _store_price(con, holding["id"], day, close * fx)
    return len(points)


def fetch_prices(con, holding, rng="5d"):
    """Fetch daily closes for one holding and store them in GBP. rng: 5d, 1y, 5y, max.
    Ticker symbols (e.g. LQQ3.L) use Yahoo Finance; ISINs (e.g. GB00B0ZDNB53) use FT.com."""
    sym = (holding.get("price_symbol") or "").strip()
    if not sym:
        return 0
    if is_ft_symbol(sym):
        return fetch_ft(con, holding, rng)
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                       params={"range": rng, "interval": "1d", "events": "div"}, headers=UA)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        currency = meta.get("currency", "GBP")
        _set_price_info(con, holding["id"], " · ".join(x for x in (meta.get("longName") or meta.get("shortName") or sym,
                                                                   meta.get("exchangeName"), currency) if x))
        fx = FxSeries(client, currency, rng)  # USD/EUR lines: each day converted at that day's exchange rate
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        divs = (res.get("events") or {}).get("dividends") or {}
    for dv in divs.values():  # per-share amounts are in the quote currency (pence for GBp lines), same as prices
        if dv.get("amount") and dv.get("date"):
            con.execute("INSERT INTO dividend_events(holding_id,ex_date,per_share) VALUES(?,?,?) "
                        "ON CONFLICT(holding_id,ex_date) DO UPDATE SET per_share=excluded.per_share",
                        (holding["id"], (ex := datetime.utcfromtimestamp(dv["date"]).date().isoformat()), round(dv["amount"] * fx.on(ex), 8)))
    n = 0
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.utcfromtimestamp(t).date().isoformat()
        _store_price(con, holding["id"], day, c * fx.on(day))
        n += 1
    return n


# ---------------------------------------------------------------- intraday prices (for the 1D / 5D chart)

_INTRADAY_CACHE = {}  # (symbol, range) -> (fetched_at, [(epoch_seconds, gbp_price)], previous close in GBP or None)
INTRADAY_TTL = 300    # seconds


def _intraday_fx(client, currency, rng, interval):
    """Exchange-rate bars for converting intraday prices at the rate *at that moment*, not a daily rate.
    Returns a function ts -> rate. Pence and pounds need no lookup."""
    if currency in ("GBp", "GBX"):
        return lambda ts: 0.01
    if currency == "GBP":
        return lambda ts: 1.0
    try:
        r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{currency.upper()}GBP=X",
                       params={"range": rng, "interval": interval}, headers=UA)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        pts = [(t, c) for t, c in zip(res.get("timestamp") or [], (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []) if c]
        if pts:
            import bisect
            ts_list, rates = [p[0] for p in pts], [p[1] for p in pts]
            return lambda ts: rates[max(0, bisect.bisect_right(ts_list, ts) - 1)]
    except Exception as e:
        log.warning("intraday FX fetch failed for %s: %s", currency, e)
    daily = FxSeries(client, currency, "1mo")   # fallback: one rate per day
    return lambda ts: daily.on(datetime.utcfromtimestamp(ts).date().isoformat())


def fetch_intraday(symbol, rng="1d"):
    """Intraday GBP prices from Yahoo for a ticker: 5-minute bars for '1d', 15-minute for '5d'.
    Each bar is placed at its *end* time with its closing price, plus the opening price at the start of
    each session, so 08:00 really is the 08:00 price. Foreign-currency lines are converted at the exchange
    rate at that moment. Cached for 5 minutes. Returns [] for ISIN/FT funds or if Yahoo can't be reached."""
    sym = (symbol or "").strip()
    if not sym or is_ft_symbol(sym):
        return []
    key = (sym, rng)
    hit = _INTRADAY_CACHE.get(key)
    if hit and time.time() - hit[0] < INTRADAY_TTL:
        return hit[1]
    interval = "5m" if rng == "1d" else "15m"
    prev = None
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                           params={"range": rng, "interval": interval, "includePrePost": "false"}, headers=UA)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            cur = res["meta"].get("currency", "GBP")
            fx = _intraday_fx(client, cur, rng, interval)
            pc = res["meta"].get("chartPreviousClose")
            if pc and ts_first(res):
                prev = pc * _prev_fx(client, cur, fx, ts_first(res))
            step = 300 if interval == "5m" else 900
            ts = res.get("timestamp") or []
            q = (res.get("indicators", {}).get("quote") or [{}])[0]
            opens, closes = q.get("open") or [None] * len(ts), q.get("close") or []
            bars, last_day = [], None
            for t, o, c in zip(ts, opens, closes):
                if c is None:
                    continue
                day = datetime.utcfromtimestamp(t).date()
                if day != last_day and o is not None:   # session open: the price at the very start
                    bars.append((t, o * fx(t)))
                last_day = day
                bars.append((t + step, c * fx(t + step)))
    except Exception as e:
        log.warning("intraday fetch failed for %s: %s", sym, e)
        bars, prev = (hit[1], hit[2]) if hit else ([], None)
    _INTRADAY_CACHE[key] = (time.time(), bars, prev)
    return bars


def ts_first(res):
    ts = res.get("timestamp") or []
    return ts[0] if ts else None


def _prev_fx(client, currency, fx, first_ts):
    """Exchange rate for the previous close: the FX market's own previous close, else the first rate of the day."""
    if currency in ("GBp", "GBX", "GBP"):
        return fx(first_ts)
    try:
        r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{currency.upper()}GBP=X",
                       params={"range": "1d", "interval": "5m"}, headers=UA)
        r.raise_for_status()
        p = r.json()["chart"]["result"][0]["meta"].get("chartPreviousClose")
        if p:
            return float(p)
    except Exception:
        pass
    return fx(first_ts)


def intraday_prev_close(symbol, rng="1d"):
    """Previous session's closing price in GBP for a ticker fetched with fetch_intraday (None if unknown)."""
    hit = _INTRADAY_CACHE.get(((symbol or "").strip(), rng))
    return hit[2] if hit else None


def refresh_all_prices(con, rng="1mo"):  # a month back each run: catches any missed days and dividends
    results = {}
    for h in rows(con.execute("SELECT * FROM holdings WHERE price_symbol <> ''")):
        try:
            results[h["ticker"]] = fetch_prices(con, h, rng)
        except Exception as e:  # network down, symbol wrong, etc.
            log.warning("price fetch failed for %s: %s", h["price_symbol"], e)
            results[h["ticker"]] = f"error: {e}"
    set_setting(con, "prices_refreshed_at", datetime.now().isoformat(timespec="seconds"))
    return results


# ---------------------------------------------------------------- bank holidays

def sync_bank_holidays(con):
    """Pull the official England & Wales list from gov.uk to catch one-off holidays. Optional – rules work offline."""
    r = httpx.get("https://www.gov.uk/bank-holidays.json", timeout=20, headers=UA)
    r.raise_for_status()
    events = r.json()["england-and-wales"]["events"]
    dates = sorted({e["date"] for e in events})
    set_setting(con, "bank_holidays_extra", json.dumps(dates))
    return len(dates)


# ---------------------------------------------------------------- Trading 212 CSV import

def _col(row, *names):
    low = {k.strip().lower(): v for k, v in row.items() if k}
    for n in names:
        v = low.get(n.lower())
        if v not in (None, ""):
            return v
    return ""


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0.0


def import_trading212(con, account_id, text):
    """Import a Trading 212 'History' CSV export. Re-importing the same file is safe (dedup on the ID column)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    stats = {"imported": 0, "skipped": 0, "unknown": []}
    acc = con.execute("SELECT interest_mode, interest_auto_from FROM accounts WHERE id=?", (account_id,)).fetchone()
    auto_from = acc["interest_auto_from"] if acc and acc["interest_mode"] == "daily" else None
    for row in reader:
        action = _col(row, "Action").lower()
        when = _col(row, "Time")[:10]
        ext = _col(row, "ID") or f"{_col(row, 'Time')}|{action}|{_col(row, 'Total')}"
        total = abs(_f(_col(row, "Total")))
        if not when or not action:
            continue
        if "buy" in action or "sell" in action:
            ttype = "buy" if "buy" in action else "sell"
        elif action.startswith("dividend"):
            ttype = "dividend"
        elif "interest" in action:
            ttype = "interest"
            if auto_from and when >= auto_from:  # already calculated automatically – don't count it twice
                stats["skipped"] += 1
                continue
        elif action == "deposit":
            ttype = "deposit"
        elif action == "withdrawal":
            ttype = "withdrawal"
        elif "fee" in action or "stamp" in action:
            ttype = "fee"
        else:
            stats["unknown"].append(action)
            continue
        hid = None
        units = None
        ticker = _col(row, "Ticker")
        if ttype in ("buy", "sell", "dividend") and ticker:
            h = con.execute("SELECT id FROM holdings WHERE account_id=? AND ticker=?", (account_id, ticker)).fetchone()
            if not h:
                isin = _col(row, "ISIN")
                guess = f"{ticker}.L" if isin[:2] in ("GB", "IE", "JE", "GG", "LU") else ticker
                cur = con.execute("INSERT INTO holdings(account_id,ticker,name,isin,price_symbol) VALUES(?,?,?,?,?)",
                                  (account_id, ticker, _col(row, "Name"), isin, guess))
                hid = cur.lastrowid
            else:
                hid = h["id"]
            if ttype in ("buy", "sell"):
                units = abs(_f(_col(row, "No. of shares")))
        if ttype == "dividend" and hid:
            hm = con.execute("SELECT dividend_mode, dividends_auto_from FROM holdings WHERE id=?", (hid,)).fetchone()
            if hm and hm["dividend_mode"] in ("cash", "reinvest") and when >= (hm["dividends_auto_from"] or ""):
                stats["skipped"] += 1  # calculated automatically from Yahoo – don't count it twice
                continue
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions(account_id,date,type,amount,holding_id,units,notes,external_id) VALUES(?,?,?,?,?,?,?,?)",
            (account_id, when, ttype, round(total, 2), hid, units, f"T212: {_col(row, 'Action')}", ext))
        stats["imported" if cur.rowcount else "skipped"] += 1
    stats["unknown"] = sorted(set(stats["unknown"]))
    return stats
