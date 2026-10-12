"""Side-effecting services: recurring payment generation, price fetching, bank holiday sync, Trading 212 CSV import."""
import csv
import io
import json
import logging
import re
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


def upcoming(con, months=12):
    today = date.today()
    out = []
    for rec in rows(con.execute("SELECT r.*, a.name AS account_name FROM recurring r JOIN accounts a ON a.id=r.account_id WHERE r.active=1")):
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
    years = {"5d": 0, "1y": 1, "5y": 5, "max": 15}.get(rng, 1)
    with httpx.Client(timeout=30, follow_redirects=True, headers=FT_HEADERS) as client:
        r = client.get(f"{FT_BASE}/data/funds/tearsheet/historical", params={"s": f"{isin}:{cur}"})
        r.raise_for_status()
        xid, price_cur, latest = ft_parse_tearsheet(r.text)
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
        r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": rng, "interval": "1d"}, headers=UA)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        currency = res["meta"].get("currency", "GBP")
        fx = _fx_to_gbp(client, currency)  # current FX applied to history: good enough for GBP-listed lines
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    n = 0
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.utcfromtimestamp(t).date().isoformat()
        _store_price(con, holding["id"], day, c * fx)
        n += 1
    return n


def refresh_all_prices(con, rng="5d"):
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
        cur = con.execute(
            "INSERT OR IGNORE INTO transactions(account_id,date,type,amount,holding_id,units,notes,external_id) VALUES(?,?,?,?,?,?,?,?)",
            (account_id, when, ttype, round(total, 2), hid, units, f"T212: {_col(row, 'Action')}", ext))
        stats["imported" if cur.rowcount else "skipped"] += 1
    stats["unknown"] = sorted(set(stats["unknown"]))
    return stats
