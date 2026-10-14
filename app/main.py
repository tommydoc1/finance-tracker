"""Finance Tracker – FastAPI app serving the API and the single-page frontend."""
import base64
import csv
import io
import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import calc, services
from .db import db, get_setting, init, rows, set_setting

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("finance")

APP_VERSION = os.environ.get("APP_VERSION", "dev")
def _env_secret(name, default=""):
    """Read a credential from the environment, ignoring stray whitespace or wrapping quotes from .env/Portainer."""
    v = os.environ.get(name, default).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


USER = _env_secret("APP_USERNAME", "admin") or "admin"
PASSWORD = _env_secret("APP_PASSWORD")
AUTO_PRICES = os.environ.get("AUTO_PRICES", "true").lower() == "true"
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "6"))
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Finance Tracker", docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- auth (HTTP Basic, optional)

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if PASSWORD and request.url.path != "/api/health":
        ok = False
        hdr = request.headers.get("authorization", "")
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:].strip())
                try:
                    creds = raw.decode("utf-8")
                except UnicodeDecodeError:
                    creds = raw.decode("latin-1")
                u, _, p = creds.partition(":")
                # compare as bytes: compare_digest raises on non-ASCII str (e.g. a £ in the password)
                user_ok = secrets.compare_digest(u.encode(), USER.encode())
                pass_ok = secrets.compare_digest(p.encode(), PASSWORD.encode())
                ok = user_ok and pass_ok
                if not ok:  # explain the failure in the container log, never the password itself
                    log.warning("login failed from %s: %s", request.client.host if request.client else "?",
                                "wrong username (got %r, expected %r)" % (u, USER) if not user_ok
                                else "wrong password (got %d characters, expected %d)" % (len(p), len(PASSWORD)))
            except Exception as e:
                log.warning("login failed: unreadable credentials (%s)", e)
                ok = False
        if not ok:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Finance Tracker"'})
    return await call_next(request)


# ---------------------------------------------------------------- background jobs

def background_loop():
    while True:
        try:
            with db() as con:
                if AUTO_PRICES:
                    services.refresh_all_prices(con)
                n = services.run_generators(con)
                if n:
                    log.info("created %s recurring transactions", n)
        except Exception as e:
            log.warning("background job failed: %s", e)
        time.sleep(REFRESH_HOURS * 3600)


@app.on_event("startup")
def startup():
    init()
    threading.Thread(target=background_loop, daemon=True).start()


# ---------------------------------------------------------------- generic CRUD helpers

FIELDS = {
    "accounts": ["name", "type", "provider", "valuation_mode", "opened_on", "notes", "archived", "interest_mode",
                 "interest_auto_from", "hidden"],
    "holdings": ["account_id", "ticker", "name", "isin", "ocf_pct", "price_symbol", "notes", "dividend_mode", "dividends_auto_from"],
    "transactions": ["account_id", "date", "type", "amount", "holding_id", "units", "notes"],
    "rates": ["account_id", "kind", "effective_date", "value", "notes"],
    "valuations": ["account_id", "date", "value", "notes"],
    "recurring": ["account_id", "label", "txn_type", "rule", "day", "start_date", "end_date", "tax_relief_pct", "active",
                  "invest_holding_id"],
    "recurring_amounts": ["recurring_id", "effective_from", "amount", "notes"],
}
TXN_TYPES = {"deposit", "withdrawal", "contribution", "tax_relief", "transfer_in", "transfer_out",
             "buy", "sell", "dividend", "interest", "fee", "bonus", "opening_balance", "unit_adjust"}


def _clean(table, data, partial=False):
    out = {}
    for k in FIELDS[table]:
        if k in data:
            v = data[k]
            out[k] = None if v == "" and k in ("holding_id", "units", "end_date", "opened_on", "invest_holding_id", "interest_auto_from", "dividends_auto_from") else v
    if table == "transactions":
        if "type" in out and out["type"] not in TXN_TYPES:
            raise HTTPException(400, f"unknown transaction type {out['type']}")
        if "amount" in out:
            out["amount"] = abs(float(out["amount"]))
    if not partial:
        req = FIELDS[table][:4] if table == "transactions" else FIELDS[table][:2]
        missing = [k for k in req if out.get(k) in (None, "")]
        if missing:
            raise HTTPException(400, f"missing: {', '.join(missing)}")
    return out


def _insert(table, data):
    data = _clean(table, data)
    with db() as con:
        cur = con.execute(f"INSERT INTO {table}({','.join(data)}) VALUES({','.join('?' * len(data))})", list(data.values()))
        if table in ("recurring", "recurring_amounts", "holdings", "rates", "transactions", "accounts"):
            services.run_generators(con)
        return dict(con.execute(f"SELECT * FROM {table} WHERE id=?", (cur.lastrowid,)).fetchone())


def _update(table, item_id, data):
    data = _clean(table, data, partial=True)
    if not data:
        raise HTTPException(400, "nothing to update")
    with db() as con:
        old = con.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        con.execute(f"UPDATE {table} SET {','.join(f'{k}=?' for k in data)} WHERE id=?", [*data.values(), item_id])
        if table == "holdings" and old and "price_symbol" in data and (data["price_symbol"] or "").strip() != (old["price_symbol"] or "").strip():
            # symbol changed: prices fetched for the old symbol belong to a different security – drop them
            con.execute("DELETE FROM prices WHERE holding_id=? AND source='auto'", (item_id,))
            con.execute("UPDATE holdings SET price_info=NULL WHERE id=?", (item_id,))
        r = con.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        return dict(r)


def _delete(table, item_id):
    with db() as con:
        con.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
    return {"ok": True}


for _t, _path in [("accounts", "accounts"), ("holdings", "holdings"), ("rates", "rates"), ("valuations", "valuations"),
                  ("recurring", "recurring"), ("recurring_amounts", "recurring-amounts")]:
    def _mk(t, path):
        def create(body: dict = Body(...)):
            return _insert(t, body)

        def update(item_id: int, body: dict = Body(...)):
            return _update(t, item_id, body)

        def remove(item_id: int):
            return _delete(t, item_id)

        app.post(f"/api/{path}", name=f"create_{t}")(create)
        app.put(f"/api/{path}/{{item_id}}", name=f"update_{t}")(update)
        app.delete(f"/api/{path}/{{item_id}}", name=f"delete_{t}")(remove)
    _mk(_t, _path)


# ---------------------------------------------------------------- transactions

@app.get("/api/transactions")
def list_transactions(account_id: int | None = None, limit: int = 100000):
    q = ("SELECT t.*, a.name AS account_name, h.ticker FROM transactions t JOIN accounts a ON a.id=t.account_id "
         "LEFT JOIN holdings h ON h.id=t.holding_id")
    args = []
    if account_id:
        q += " WHERE t.account_id=?"; args.append(account_id)
    q += " ORDER BY t.date DESC, t.id DESC LIMIT ?"
    with db() as con:
        return rows(con.execute(q, [*args, limit]))


@app.post("/api/transactions")
def add_transaction(body: dict = Body(...)):
    return _insert("transactions", body)


@app.put("/api/transactions/{item_id}")
def edit_transaction(item_id: int, body: dict = Body(...)):
    return _update("transactions", item_id, body)


@app.delete("/api/transactions/{item_id}")
def delete_transaction(item_id: int):
    with db() as con:
        t = con.execute("SELECT * FROM transactions WHERE id=?", (item_id,)).fetchone()
        if t and t["recurring_id"]:  # remember the skip so it isn't regenerated
            con.execute("INSERT OR IGNORE INTO recurring_skips VALUES(?,?)", (t["recurring_id"], t["recurring_key"]))
        con.execute("DELETE FROM transactions WHERE id=?", (item_id,))
    return {"ok": True}


# ---------------------------------------------------------------- holdings & prices

@app.get("/api/holdings")
def list_holdings():
    with db() as con:
        return rows(con.execute("SELECT * FROM holdings ORDER BY account_id, ticker"))


def _units_held(con, hid, on):
    r = con.execute(
        "SELECT COALESCE(SUM(CASE type WHEN 'buy' THEN units WHEN 'unit_adjust' THEN units WHEN 'sell' THEN -units ELSE 0 END),0) AS u "
        "FROM transactions WHERE holding_id=? AND date<=? AND units IS NOT NULL", (hid, on)).fetchone()
    return r["u"]


@app.post("/api/holdings/{hid}/opening")
def opening_position(hid: int, body: dict = Body(...)):
    """Start tracking a fund you already hold: records what you've paid in so far and the units it bought."""
    day, units, paid = body.get("date"), float(body.get("units") or 0), abs(float(body.get("paid_in") or 0))
    if not day or units <= 0 or paid <= 0:
        raise HTTPException(400, "date, units and paid_in are required")
    with db() as con:
        h = con.execute("SELECT * FROM holdings WHERE id=?", (hid,)).fetchone()
        if not h:
            raise HTTPException(404)
        con.execute("INSERT INTO transactions(account_id,date,type,amount,notes) VALUES(?,?,'opening_balance',?,?)",
                    (h["account_id"], day, paid, f"Paid in before tracking ({h['ticker']})"))
        con.execute("INSERT INTO transactions(account_id,date,type,amount,holding_id,units,notes) VALUES(?,?,'buy',?,?,?,?)",
                    (h["account_id"], day, paid, hid, units, "Opening position"))
        services.run_generators(con)
    return {"ok": True}


@app.post("/api/holdings/{hid}/set-units")
def set_units(hid: int, body: dict = Body(...)):
    """Match the unit count your provider shows: adds a unit adjustment for any difference."""
    day, actual = body.get("date") or date.today().isoformat(), float(body.get("units"))
    with db() as con:
        h = con.execute("SELECT * FROM holdings WHERE id=?", (hid,)).fetchone()
        if not h:
            raise HTTPException(404)
        diff = round(actual - _units_held(con, hid, day), 6)
        if abs(diff) > 1e-6:
            con.execute("INSERT INTO transactions(account_id,date,type,amount,holding_id,units,notes) VALUES(?,?,'unit_adjust',0,?,?,?)",
                        (h["account_id"], day, hid, diff, f"Matched provider unit count ({actual:g})"))
    return {"adjusted_by": diff}


@app.get("/api/holdings/{hid}/prices")
def holding_prices(hid: int):
    with db() as con:
        return rows(con.execute("SELECT * FROM prices WHERE holding_id=? ORDER BY date", (hid,)))


@app.post("/api/holdings/{hid}/prices")
def add_price(hid: int, body: dict = Body(...)):
    with db() as con:
        con.execute("INSERT INTO prices(holding_id,date,price,source) VALUES(?,?,?,'manual') "
                    "ON CONFLICT(holding_id,date) DO UPDATE SET price=excluded.price, source='manual'",
                    (hid, body["date"], float(body["price"])))
    return {"ok": True}


@app.delete("/api/holdings/{hid}/prices/{day}")
def delete_price(hid: int, day: str):
    with db() as con:
        con.execute("DELETE FROM prices WHERE holding_id=? AND date=?", (hid, day))
    return {"ok": True}


@app.post("/api/holdings/{hid}/fetch")
def fetch_holding(hid: int, range: str = "max"):
    with db() as con:
        h = con.execute("SELECT * FROM holdings WHERE id=?", (hid,)).fetchone()
        if not h:
            raise HTTPException(404)
        try:
            n = services.fetch_prices(con, dict(h), range)
        except Exception as e:
            raise HTTPException(502, f"Price fetch failed: {e}")
        info = con.execute("SELECT price_info FROM holdings WHERE id=?", (hid,)).fetchone()["price_info"]
    return {"prices": n, "matched": info}


@app.post("/api/prices/refresh")
def refresh_prices():
    with db() as con:
        return services.refresh_all_prices(con)


# ---------------------------------------------------------------- recurring

@app.get("/api/recurring")
def list_recurring():
    with db() as con:
        recs = rows(con.execute("SELECT r.*, a.name AS account_name FROM recurring r JOIN accounts a ON a.id=r.account_id ORDER BY r.id"))
        for r in recs:
            r["amounts"] = rows(con.execute("SELECT * FROM recurring_amounts WHERE recurring_id=? ORDER BY effective_from", (r["id"],)))
        return recs


@app.post("/api/recurring/{rid}/rebuild")
def rebuild_recurring(rid: int):
    """Delete this schedule's auto-created transactions and regenerate (use after back-dating an amount change)."""
    with db() as con:
        con.execute("DELETE FROM transactions WHERE recurring_id=?", (rid,))
        con.execute("DELETE FROM recurring_skips WHERE recurring_id=?", (rid,))
        return {"created": services.generate_recurring(con)}


@app.get("/api/upcoming")
def get_upcoming(months: int = 12, include_hidden: bool = True):
    with db() as con:
        return services.upcoming(con, months, include_hidden)


@app.get("/api/paydates")
def paydates(year: int, rule: str = "last_working_day", day: int = 28):
    with db() as con:
        cal = services.calendar_for(con)
    return [{"month": m, "date": cal.pay_date(year, m, rule, day).isoformat()} for m in range(1, 13)]


@app.post("/api/bank-holidays/sync")
def sync_holidays():
    with db() as con:
        try:
            return {"dates": services.sync_bank_holidays(con)}
        except Exception as e:
            raise HTTPException(502, f"gov.uk fetch failed: {e}")


# ---------------------------------------------------------------- analytics

def _load(con):
    accounts = rows(con.execute("SELECT * FROM accounts ORDER BY archived, id"))
    txns = rows(con.execute("SELECT * FROM transactions ORDER BY date, id"))
    vals = rows(con.execute("SELECT * FROM valuations ORDER BY date"))
    holds = rows(con.execute("SELECT * FROM holdings"))
    prices = calc.PriceBook(rows(con.execute("SELECT holding_id,date,price FROM prices")))
    rts = rows(con.execute("SELECT * FROM rates ORDER BY effective_date, id"))
    by = lambda lst, k="account_id": {a["id"]: [x for x in lst if x[k] == a["id"]] for a in accounts}  # noqa: E731
    T, V, H, R = by(txns), by(vals), by(holds), by(rts)
    states = {a["id"]: calc.AccountState(a, T[a["id"]], V[a["id"]], H[a["id"]], prices) for a in accounts}
    return accounts, states, T, H, R, prices


def _paid_in_warnings(a, st, snap, today):
    """Spot value with no matching 'paid in' record – it shows up as fake growth on the overview."""
    w = []
    value, paid = snap["value"], snap["contributed"]
    if value > 1 and paid <= 0:
        w.append("Nothing is recorded as paid in, so its whole value counts as growth. Add an Opening balance "
                 "(what you'd paid in when you started tracking) or a deposit.")
    if a["valuation_mode"] == "holdings":
        if snap["cash"] < -1:
            w.append(f"Cash is {snap['cash']:,.2f} – there are buys or withdrawals without the deposits that paid for them, "
                     "so paid in is understated. Add the missing deposits or an Opening balance.")
        has_position = any(t["type"] == "buy" and (t.get("notes") or "") == "Opening position" for t in st.txns)
        extra_ob = [t for t in st.txns if t["type"] == "opening_balance" and not (t.get("notes") or "").startswith("Paid in before tracking")]
        if has_position and extra_ob and snap["cash"] > 1:
            w.append(f"An extra Opening balance of £{sum(t['amount'] for t in extra_ob):,.2f} was added on top of "
                     "\"Add existing position\", which already records what was paid in – so the money is counted twice "
                     "(it shows up as cash). Delete that opening balance on the Transactions tab unless it really is uninvested cash.")
        no_cost = sum(h["value"] for h in snap["holdings"].values() if h["cost"] <= 0.01 and h["value"] > 1)
        if no_cost > 1:
            w.append(f"£{no_cost:,.2f} of holdings have no purchase cost (e.g. added with Set units). Use "
                     "\"Add existing position\" on the Holdings tab to record what was paid for them.")
    elif st.vals and paid > 0:
        first = st.vals[0]
        if st.snapshot(calc.d(first["date"]))["contributed"] <= 0:
            w.append(f"The first statement value ({calc.d(first['date']).strftime('%d %b %Y')}) has nothing paid in "
                     "recorded on or before it. Add an Opening balance dated the same day.")
    if not w and paid > 0 and (value - paid) / paid > 3:
        w.append(f"Growth of {(value - paid) / paid:.0%} looks unusually high – check that every deposit or "
                 "opening balance has been recorded.")
    return w


def _account_summary(a, st, txns, holds, rts, today):
    snap = st.snapshot(today)
    ty = calc.tax_year_of(today)
    ty_start, _ = calc.tax_year_bounds(ty)
    sums = defaultdict(float)
    sums_ty = defaultdict(float)
    flows = []
    for t in txns:
        sums[t["type"]] += t["amount"]
        if calc.d(t["date"]) >= ty_start:
            sums_ty[t["type"]] += t["amount"]
        f = calc.external_flow(t)
        if f:
            flows.append((t["date"], -f))
    if snap["value"]:
        flows.append((today.isoformat(), snap["value"]))
    hmap = {h["id"]: h for h in holds}
    holdings = []
    ocf_cost = 0.0
    for hid, info in snap["holdings"].items():
        h = hmap.get(hid, {})
        latest = st.prices.latest(hid)
        ocf = (h.get("ocf_pct") or 0) * info["value"] / 100
        ocf_cost += ocf
        holdings.append({**h, **info, "gain": info["value"] - info["cost"], "price_date": latest[0] if latest else None,
                         "ocf_annual": ocf, "dividends": sum(t["amount"] for t in txns if t["type"] == "dividend" and t.get("holding_id") == hid)})
    fee_pct = calc.rate_on(rts, "fee_pct", today) or 0.0
    fee_flat = calc.rate_on(rts, "fee_flat", today) or 0.0
    int_rate = calc.rate_on(rts, "interest", today)
    # interest is only ever earned on uninvested cash; statement-valued accounts have no cash/holdings split
    interest_base = snap["value"] if a["valuation_mode"] == "manual" else snap["cash"]
    est_int_ty = calc.estimate_interest(st, rts, ty_start, today) if int_rate is not None else 0.0
    month_start = today.replace(day=1)
    est_int_month = calc.estimate_interest(st, rts, month_start, today) if int_rate is not None else 0.0
    platform_cost = fee_pct * snap["value"] / 100 + fee_flat * 12
    return {
        "warnings": _paid_in_warnings(a, st, snap, today),
        **a,
        "value": snap["value"], "cash": snap["cash"], "holdings_value": snap["holdings_value"],
        "contributed": snap["contributed"], "gain": snap["value"] - snap["contributed"],
        "gain_pct": (snap["value"] - snap["contributed"]) / snap["contributed"] * 100 if snap["contributed"] > 0 else None,
        "xirr": calc.xirr(flows),
        "last_valuation": snap["last_valuation"],
        "holdings": sorted(holdings, key=lambda h: -h["value"]),
        "dividends_total": sums["dividend"], "dividends_ty": sums_ty["dividend"],
        "interest_total": sums["interest"], "interest_ty": sums_ty["interest"],
        "fees_total": sums["fee"], "fees_ty": sums_ty["fee"],
        "tax_relief_total": sums["tax_relief"],
        "interest_rate": int_rate, "interest_est_annual": (int_rate or 0) * max(interest_base, 0) / 100,
        "interest_est_ty": est_int_ty, "interest_est_month": est_int_month,
        "fee_pct": fee_pct, "fee_flat": fee_flat,
        "charges_platform_annual": platform_cost, "charges_ocf_annual": ocf_cost,
        "charges_total_annual": platform_cost + ocf_cost,
        "isa_subscribed_ty": (sums_ty["deposit"] + sums_ty["contribution"]) if a["type"] in calc.ISA_TYPES else 0.0,
        "tax_year": ty,
    }


@app.get("/api/summary")
def summary(include_hidden: bool = False):
    today = date.today()
    with db() as con:
        services.run_generators(con)
        accounts, states, T, H, R, _ = _load(con)
        refreshed = get_setting(con, "prices_refreshed_at")
    accs = [_account_summary(a, states[a["id"]], T[a["id"]], H[a["id"]], R[a["id"]], today) for a in accounts]
    active = [a for a in accs if not a["archived"]]
    live = [a for a in active if include_hidden or not a.get("hidden")]  # what the overview totals cover
    tot = lambda k: sum(a[k] or 0 for a in live)  # noqa: E731
    flows = []
    for a in live:
        for t in T[a["id"]]:
            f = calc.external_flow(t)
            if f:
                flows.append((t["date"], -f))
    if tot("value"):
        flows.append((today.isoformat(), tot("value")))
    isa_used = sum(a["isa_subscribed_ty"] for a in active)  # the allowance is a legal limit – count every ISA
    lisa_used = sum(a["isa_subscribed_ty"] for a in active if a["type"] == "lisa")
    return {
        "today": today.isoformat(), "tax_year": calc.tax_year_of(today), "version": APP_VERSION,
        "prices_refreshed_at": refreshed,
        "totals": {k: tot(k) for k in ["value", "contributed", "cash", "dividends_total", "dividends_ty", "interest_total",
                                        "interest_ty", "fees_total", "fees_ty", "charges_total_annual", "interest_est_annual",
                                        "tax_relief_total", "interest_est_ty"]}
                  | {"gain": tot("value") - tot("contributed"), "xirr": calc.xirr(flows)},
        "isa": {"allowance": calc.ISA_ALLOWANCE, "used": isa_used, "remaining": calc.ISA_ALLOWANCE - isa_used,
                "lisa_used": lisa_used, "lisa_allowance": calc.LISA_ALLOWANCE,
                "tax_year_end": calc.tax_year_bounds(calc.tax_year_of(today))[1].isoformat()},
        "accounts": accs,
        "include_hidden": include_hidden,
        "hidden_count": sum(1 for a in active if a.get("hidden")),
    }


@app.get("/api/accounts")
def list_accounts():
    return summary()["accounts"]


@app.get("/api/accounts/{aid}")
def account_detail(aid: int):
    with db() as con:
        a = con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
        if not a:
            raise HTTPException(404)
        services.run_generators(con)
        accounts, states, T, H, R, _ = _load(con)
        detail = _account_summary(dict(a), states[aid], T[aid], H[aid], R[aid], date.today())
        detail["all_holdings"] = H[aid]
        detail["rates"] = R[aid]
        detail["valuations"] = rows(con.execute("SELECT * FROM valuations WHERE account_id=? ORDER BY date DESC", (aid,)))
        detail["recurring"] = [r for r in list_recurring() if r["account_id"] == aid]
        return detail


@app.get("/api/series")
def series(account_id: int | None = None, start: str | None = None, include_hidden: bool = False):
    """Value vs net contributions over time (whole portfolio or one account)."""
    today = date.today()
    with db() as con:
        accounts, states, T, *_ = _load(con)
    ids = [account_id] if account_id else [a["id"] for a in accounts if not a["archived"] and (include_hidden or not a["hidden"])]
    firsts = [calc.d(T[i][0]["date"]) for i in ids if T.get(i)]
    firsts += [calc.d(v["date"]) for i in ids for v in states[i].vals[:1]]
    if not firsts:
        return {"points": [], "accounts": []}
    s = max(min(firsts), calc.d(start)) if start else min(firsts)
    pts = []
    dates = calc.sample_dates(s, today)
    per_acct = {i: states[i].series(dates) for i in ids}
    for n, x in enumerate(dates):
        row = {"date": x.isoformat(), "value": 0.0, "contributed": 0.0, "by_account": {}}
        for i in ids:
            v, c = per_acct[i][n]
            row["value"] += v; row["contributed"] += c
            row["by_account"][i] = round(v, 2)
        row["value"] = round(row["value"], 2); row["contributed"] = round(row["contributed"], 2)
        pts.append(row)
    return {"points": pts, "accounts": [{"id": a["id"], "name": a["name"]} for a in accounts if a["id"] in ids]}


@app.get("/api/series/intraday")
def series_intraday(range: str = "1d", account_id: int | None = None, include_hidden: bool = False):
    """1D / 5D chart: value through the trading day(s) using live intraday prices for ETFs and shares.
    Cash, units and paid-in come from the ledger for each day; holdings without intraday data (FT pension funds,
    manual prices) use their daily price; statement-valued accounts use their daily value."""
    rng = "5d" if range == "5d" else "1d"
    with db() as con:
        accounts, states, T, H, *_ = _load(con)
    ids = [account_id] if account_id else [a["id"] for a in accounts if not a["archived"] and (include_hidden or not a["hidden"])]
    bars = {}  # holding id -> [(ts, price)]
    for i in ids:
        if states[i].a["valuation_mode"] == "manual":
            continue
        for h in H[i]:
            b = services.fetch_intraday(h.get("price_symbol"), rng)
            if b:
                bars[h["id"]] = b
    stamps = sorted({t for b in bars.values() for t, _ in b})
    if not stamps:  # nothing trades intraday (or Yahoo unreachable): fall back to daily values
        days = 1 if rng == "1d" else 5
        out = series(account_id=account_id, start=(date.today() - timedelta(days=days)).isoformat(), include_hidden=include_hidden)
        out["intraday"] = False
        return out
    import bisect
    keyed = {hid: ([t for t, _ in b], [p for _, p in b]) for hid, b in bars.items()}
    snaps = {}  # (account, day) -> snapshot, so each day's ledger is only replayed once
    pts = []
    for ts in stamps:
        day = datetime.fromtimestamp(ts, timezone.utc).date()
        row = {"date": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "value": 0.0, "contributed": 0.0, "by_account": {}}
        for i in ids:
            sn = snaps.get((i, day)) or snaps.setdefault((i, day), states[i].snapshot(day))
            if states[i].a["valuation_mode"] == "manual":
                v = sn["value"]
            else:
                v = sn["cash"]
                for hid, info in sn["holdings"].items():
                    px = info["price"]
                    if hid in keyed:
                        k = bisect.bisect_right(keyed[hid][0], ts) - 1
                        if k >= 0:
                            px = keyed[hid][1][k]
                    v += info["units"] * px
            row["value"] += v; row["contributed"] += sn["contributed"]
            row["by_account"][i] = round(v, 2)
        row["value"] = round(row["value"], 2); row["contributed"] = round(row["contributed"], 2)
        pts.append(row)
    return {"points": pts, "accounts": [{"id": a["id"], "name": a["name"]} for a in accounts if a["id"] in ids], "intraday": True}


@app.get("/api/income")
def income():
    """Dividends, interest and fees grouped by tax year, month and holding."""
    with db() as con:
        txs = rows(con.execute(
            "SELECT t.*, a.name AS account_name, h.ticker FROM transactions t JOIN accounts a ON a.id=t.account_id "
            "LEFT JOIN holdings h ON h.id=t.holding_id WHERE t.type IN ('dividend','interest','fee','bonus','tax_relief') ORDER BY t.date"))
    by_ty = defaultdict(lambda: defaultdict(float))
    by_month = defaultdict(lambda: defaultdict(float))
    by_holding = defaultdict(float)
    by_account = defaultdict(lambda: defaultdict(float))
    for t in txs:
        by_ty[calc.tax_year_of(t["date"])][t["type"]] += t["amount"]
        by_month[t["date"][:7]][t["type"]] += t["amount"]
        by_account[t["account_name"]][t["type"]] += t["amount"]
        if t["type"] == "dividend":
            by_holding[t["ticker"] or "(unassigned)"] += t["amount"]
    return {"by_tax_year": by_ty, "by_month": by_month, "by_holding": by_holding, "by_account": by_account,
            "recent": list(reversed(txs[-50:]))}


# ---------------------------------------------------------------- appearance settings

UI_DEFAULTS = {"theme": "auto", "accent": "", "size": "normal"}
UI_CHOICES = {"theme": {"auto", "light", "dark", "black", "midnight", "paper"},
              "size": {"small", "normal", "large", "xlarge"}}


@app.get("/api/settings/ui")
def get_ui_settings():
    with db() as con:
        raw = get_setting(con, "ui")
    try:
        saved = json.loads(raw) if raw else {}
    except ValueError:
        saved = {}
    return {**UI_DEFAULTS, **{k: v for k, v in saved.items() if k in UI_DEFAULTS}}


@app.put("/api/settings/ui")
def put_ui_settings(body: dict = Body(...)):
    cur = get_ui_settings()
    for k, v in body.items():
        if k not in UI_DEFAULTS:
            continue
        if k in UI_CHOICES and v not in UI_CHOICES[k]:
            raise HTTPException(400, f"invalid {k}: {v}")
        if k == "accent" and v and not (isinstance(v, str) and len(v) == 7 and v.startswith("#")
                                        and all(c in "0123456789abcdefABCDEF" for c in v[1:])):
            raise HTTPException(400, "accent must be a #rrggbb colour or empty")
        cur[k] = v
    with db() as con:
        set_setting(con, "ui", json.dumps(cur))
    return cur


# ---------------------------------------------------------------- export / import

TABLES = ["accounts", "holdings", "prices", "dividend_events", "recurring", "recurring_amounts", "recurring_skips", "transactions",
          "rates", "valuations", "settings"]


@app.get("/api/export")
def export_json():
    with db() as con:
        data = {t: rows(con.execute(f"SELECT * FROM {t}")) for t in TABLES}
    data["_meta"] = {"exported": date.today().isoformat(), "version": APP_VERSION}
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="finance-backup-{date.today()}.json"'})


@app.get("/api/export/transactions.csv")
def export_csv():
    txs = list_transactions(limit=1_000_000)
    buf = io.StringIO()
    cols = ["date", "account_name", "type", "amount", "ticker", "units", "notes"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader(); w.writerows(txs)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="transactions-{date.today()}.csv"'})


@app.post("/api/import")
async def import_json(file: UploadFile):
    """Restore a JSON backup. Replaces ALL current data."""
    data = json.loads(await file.read())
    with db() as con:
        con.execute("PRAGMA foreign_keys = OFF")
        for t in reversed(TABLES):
            con.execute(f"DELETE FROM {t}")
        for t in TABLES:
            for r in data.get(t, []):
                con.execute(f"INSERT INTO {t}({','.join(r)}) VALUES({','.join('?' * len(r))})", list(r.values()))
        con.execute("PRAGMA foreign_keys = ON")
    return {"ok": True}


@app.post("/api/import/trading212/{account_id}")
async def import_t212(account_id: int, file: UploadFile):
    text = (await file.read()).decode("utf-8", errors="replace")
    with db() as con:
        return services.import_trading212(con, account_id, text)


@app.get("/api/health")
def health():
    return {"ok": True, "version": APP_VERSION}


# ---------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory=STATIC), name="static")


_INDEX_HTML = None


@app.get("/{path:path}")
def spa(path: str):
    """The page itself. Script/style links carry the app version, so a browser loads the new files
    after every update instead of reusing cached ones; the page is never cached."""
    global _INDEX_HTML
    if _INDEX_HTML is None:
        html = (STATIC / "index.html").read_text()
        v = APP_VERSION[:12]
        for f in ("styles.css", "app.js", "chart.umd.min.js"):
            html = html.replace(f"/static/{f}\"", f"/static/{f}?v={v}\"")
        _INDEX_HTML = html
    return HTMLResponse(_INDEX_HTML, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        # versioned links (?v=...) can be cached for good; anything else must be re-checked
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.url.query.startswith("v=") else "no-cache"
    elif request.url.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp
