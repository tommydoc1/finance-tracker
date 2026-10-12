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
from datetime import date, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import calc, services
from .db import db, get_setting, init, rows

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
                n = services.generate_recurring(con)
                if n:
                    log.info("created %s recurring transactions", n)
                if AUTO_PRICES:
                    services.refresh_all_prices(con)
        except Exception as e:
            log.warning("background job failed: %s", e)
        time.sleep(REFRESH_HOURS * 3600)


@app.on_event("startup")
def startup():
    init()
    threading.Thread(target=background_loop, daemon=True).start()


# ---------------------------------------------------------------- generic CRUD helpers

FIELDS = {
    "accounts": ["name", "type", "provider", "valuation_mode", "opened_on", "notes", "archived"],
    "holdings": ["account_id", "ticker", "name", "isin", "ocf_pct", "price_symbol", "notes"],
    "transactions": ["account_id", "date", "type", "amount", "holding_id", "units", "notes"],
    "rates": ["account_id", "kind", "effective_date", "value", "notes"],
    "valuations": ["account_id", "date", "value", "notes"],
    "recurring": ["account_id", "label", "txn_type", "rule", "day", "start_date", "end_date", "tax_relief_pct", "active"],
    "recurring_amounts": ["recurring_id", "effective_from", "amount", "notes"],
}
TXN_TYPES = {"deposit", "withdrawal", "contribution", "tax_relief", "transfer_in", "transfer_out",
             "buy", "sell", "dividend", "interest", "fee", "bonus", "opening_balance"}


def _clean(table, data, partial=False):
    out = {}
    for k in FIELDS[table]:
        if k in data:
            v = data[k]
            out[k] = None if v == "" and k in ("holding_id", "units", "end_date", "opened_on") else v
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
        if table in ("recurring", "recurring_amounts"):
            services.generate_recurring(con)
        return dict(con.execute(f"SELECT * FROM {table} WHERE id=?", (cur.lastrowid,)).fetchone())


def _update(table, item_id, data):
    data = _clean(table, data, partial=True)
    if not data:
        raise HTTPException(400, "nothing to update")
    with db() as con:
        con.execute(f"UPDATE {table} SET {','.join(f'{k}=?' for k in data)} WHERE id=?", [*data.values(), item_id])
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
def list_transactions(account_id: int | None = None, limit: int = 2000):
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
    return {"prices": n}


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
def get_upcoming(months: int = 12):
    with db() as con:
        return services.upcoming(con, months)


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
    interest_base = snap["value"] if a["valuation_mode"] == "manual" or a["type"] in ("cash", "cash_isa") else snap["cash"]
    est_int_ty = calc.estimate_interest(st, rts, ty_start, today) if int_rate is not None else 0.0
    month_start = today.replace(day=1)
    est_int_month = calc.estimate_interest(st, rts, month_start, today) if int_rate is not None else 0.0
    platform_cost = fee_pct * snap["value"] / 100 + fee_flat * 12
    return {
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
def summary():
    today = date.today()
    with db() as con:
        services.generate_recurring(con)
        accounts, states, T, H, R, _ = _load(con)
        refreshed = get_setting(con, "prices_refreshed_at")
    accs = [_account_summary(a, states[a["id"]], T[a["id"]], H[a["id"]], R[a["id"]], today) for a in accounts]
    live = [a for a in accs if not a["archived"]]
    tot = lambda k: sum(a[k] or 0 for a in live)  # noqa: E731
    flows = []
    for a in live:
        for t in T[a["id"]]:
            f = calc.external_flow(t)
            if f:
                flows.append((t["date"], -f))
    if tot("value"):
        flows.append((today.isoformat(), tot("value")))
    isa_used = tot("isa_subscribed_ty")
    lisa_used = sum(a["isa_subscribed_ty"] for a in live if a["type"] == "lisa")
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
        services.generate_recurring(con)
        accounts, states, T, H, R, _ = _load(con)
        detail = _account_summary(dict(a), states[aid], T[aid], H[aid], R[aid], date.today())
        detail["all_holdings"] = H[aid]
        detail["rates"] = R[aid]
        detail["valuations"] = rows(con.execute("SELECT * FROM valuations WHERE account_id=? ORDER BY date DESC", (aid,)))
        detail["recurring"] = [r for r in list_recurring() if r["account_id"] == aid]
        return detail


@app.get("/api/series")
def series(account_id: int | None = None, start: str | None = None):
    """Value vs net contributions over time (whole portfolio or one account)."""
    today = date.today()
    with db() as con:
        accounts, states, T, *_ = _load(con)
    ids = [account_id] if account_id else [a["id"] for a in accounts if not a["archived"]]
    firsts = [calc.d(T[i][0]["date"]) for i in ids if T.get(i)]
    firsts += [calc.d(v["date"]) for i in ids for v in states[i].vals[:1]]
    if not firsts:
        return {"points": [], "accounts": []}
    s = max(min(firsts), calc.d(start)) if start else min(firsts)
    pts = []
    for x in calc.sample_dates(s, today):
        row = {"date": x.isoformat(), "value": 0.0, "contributed": 0.0, "by_account": {}}
        for i in ids:
            sn = states[i].snapshot(x)
            row["value"] += sn["value"]; row["contributed"] += sn["contributed"]
            row["by_account"][i] = round(sn["value"], 2)
        row["value"] = round(row["value"], 2); row["contributed"] = round(row["contributed"], 2)
        pts.append(row)
    return {"points": pts, "accounts": [{"id": a["id"], "name": a["name"]} for a in accounts if a["id"] in ids]}


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


# ---------------------------------------------------------------- export / import

TABLES = ["accounts", "holdings", "prices", "recurring", "recurring_amounts", "recurring_skips", "transactions",
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


@app.get("/{path:path}")
def spa(path: str):
    return FileResponse(STATIC / "index.html")
