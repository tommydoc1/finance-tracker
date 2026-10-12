"""Finance calculations: UK bank holidays, pay dates, tax years, valuations, interest, charges, XIRR."""
import bisect
import calendar
import json
from collections import defaultdict
from datetime import date, timedelta

# ---------------------------------------------------------------- dates

INFLOW = {"deposit", "contribution", "tax_relief", "transfer_in", "bonus", "opening_balance"}   # external money in
OUTFLOW = {"withdrawal", "transfer_out"}                                      # external money out
ISA_TYPES = {"stocks_isa", "cash_isa", "lisa"}
ISA_ALLOWANCE = 20000.0
LISA_ALLOWANCE = 4000.0


def d(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def easter(y):
    a = y % 19; b = y // 100; c = y % 100
    dd = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - dd - g + 15) % 30
    i = c // 4; k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def _first_monday(y, m):
    x = date(y, m, 1)
    return x + timedelta(days=(7 - x.weekday()) % 7)


def _last_monday(y, m):
    x = date(y, m, calendar.monthrange(y, m)[1])
    return x - timedelta(days=x.weekday())


# One-off changes announced by government (England & Wales)
_EXTRA = {"2022-06-03", "2022-09-19", "2023-05-08", "2022-06-02", "2020-05-08", "2012-06-04", "2012-06-05", "2011-04-29"}
_REMOVED = {"2022-05-30", "2020-05-04", "2012-05-28"}


def computed_bank_holidays(y):
    """England & Wales bank holidays computed from the rules (works offline, any year)."""
    hols = set()
    ny = date(y, 1, 1)
    hols.add(ny + timedelta(days={5: 2, 6: 1}.get(ny.weekday(), 0)))
    e = easter(y)
    hols |= {e - timedelta(days=2), e + timedelta(days=1)}
    hols |= {_first_monday(y, 5), _last_monday(y, 5), _last_monday(y, 8)}
    xmas = date(y, 12, 25)
    wd = xmas.weekday()
    if wd == 5:      # Sat
        hols |= {date(y, 12, 27), date(y, 12, 28)}
    elif wd == 6:    # Sun
        hols |= {date(y, 12, 26), date(y, 12, 27)}
    elif wd == 4:    # Fri -> Boxing Day Sat moves to Mon
        hols |= {xmas, date(y, 12, 28)}
    else:
        hols |= {xmas, date(y, 12, 26)}
    hols = {h for h in hols if h.isoformat() not in _REMOVED}
    hols |= {d(x) for x in _EXTRA if x.startswith(str(y))}
    return hols


class Calendar:
    """Bank holidays = computed rules + anything fetched from gov.uk (stored in settings)."""

    def __init__(self, extra_json=None):
        self.extra = set()
        if extra_json:
            try:
                self.extra = {d(x) for x in json.loads(extra_json)}
            except Exception:
                pass
        self._cache = {}

    def holidays(self, y):
        if y not in self._cache:
            self._cache[y] = computed_bank_holidays(y) | {x for x in self.extra if x.year == y}
        return self._cache[y]

    def is_working(self, x):
        return x.weekday() < 5 and x not in self.holidays(x.year)

    def prev_working(self, x):
        while not self.is_working(x):
            x -= timedelta(days=1)
        return x

    def next_working(self, x):
        while not self.is_working(x):
            x += timedelta(days=1)
        return x

    def pay_date(self, y, m, rule, day=28):
        last = calendar.monthrange(y, m)[1]
        if rule == "last_working_day":
            return self.prev_working(date(y, m, last))
        if rule == "first_working_day":
            return self.next_working(date(y, m, 1))
        target = date(y, m, min(max(int(day or 1), 1), last))
        if rule == "day_or_after":
            return self.next_working(target)
        return self.prev_working(target)  # day_or_before (typical payroll)


def tax_year_of(x):
    """UK tax year label, e.g. 2026-10-12 -> '2026/27' (6 Apr to 5 Apr)."""
    x = d(x)
    start = x.year if (x.month, x.day) >= (4, 6) else x.year - 1
    return f"{start}/{str(start + 1)[-2:]}"


def tax_year_bounds(label):
    start = int(label[:4])
    return date(start, 4, 6), date(start + 1, 4, 5)


def months_between(a, b):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


# ---------------------------------------------------------------- rates

def rate_on(rates, kind, x):
    """Value of a time-varying rate on date x. rates: list of dicts sorted by effective_date."""
    val = None
    for r in rates:
        if r["kind"] == kind and d(r["effective_date"]) <= x:
            val = r["value"]
    return val


# ---------------------------------------------------------------- ledger / valuation

class PriceBook:
    def __init__(self, prices):
        self.by = defaultdict(list)
        for p in sorted(prices, key=lambda p: p["date"]):
            self.by[p["holding_id"]].append((p["date"], p["price"]))

    def on(self, hid, x):
        lst = self.by.get(hid)
        if not lst:
            return None
        i = bisect.bisect_right(lst, (x.isoformat(), float("inf"))) - 1
        return lst[i][1] if i >= 0 else None

    def latest(self, hid):
        lst = self.by.get(hid)
        return lst[-1] if lst else None


def cash_effect(t):
    ty, a = t["type"], t["amount"]
    if ty in INFLOW or ty in {"sell", "dividend", "interest"}:
        return a
    if ty in OUTFLOW or ty in {"buy", "fee"}:
        return -a
    return 0.0


def external_flow(t):
    """Money in (+) / out (-) from the owner's pocket."""
    if t["type"] in INFLOW:
        return t["amount"]
    if t["type"] in OUTFLOW:
        return -t["amount"]
    return 0.0


class AccountState:
    """Replays an account's history; can value it at any date."""

    def __init__(self, account, txns, valuations, holdings, prices: PriceBook):
        self.a = account
        self.txns = sorted(txns, key=lambda t: (t["date"], t["id"]))
        self.vals = sorted(valuations, key=lambda v: v["date"])
        self.holdings = {h["id"]: h for h in holdings}
        self.prices = prices
        self.last_trade_price = {}

    def snapshot(self, x):
        """Returns dict(cash, units{hid}, contributed, value, holdings_value) at end of date x."""
        cash = 0.0
        contributed = 0.0
        units = defaultdict(float)
        cost = defaultdict(float)
        flows_after_val = 0.0
        val = None
        for v in self.vals:
            if d(v["date"]) <= x:
                val = v
        for t in self.txns:
            td = d(t["date"])
            if td > x:
                break
            cash += cash_effect(t)
            contributed += external_flow(t)
            hid = t.get("holding_id")
            if hid and t.get("units"):
                if t["type"] == "buy":
                    units[hid] += t["units"]; cost[hid] += t["amount"]
                    self.last_trade_price[hid] = t["amount"] / t["units"]
                elif t["type"] == "unit_adjust":
                    units[hid] += t["units"]
                elif t["type"] == "sell":
                    if units[hid] > 1e-12:
                        cost[hid] -= cost[hid] * min(t["units"] / units[hid], 1)
                    units[hid] -= t["units"]
                    self.last_trade_price[hid] = t["amount"] / t["units"]
            if val and td > d(val["date"]):
                if t["type"] not in ("buy", "sell"):
                    flows_after_val += cash_effect(t)
        hv = 0.0
        per = {}
        for hid, u in units.items():
            if abs(u) < 1e-9:
                continue
            p = self.prices.on(hid, x)
            if p is None:
                p = self.last_trade_price.get(hid, 0.0)
            per[hid] = {"units": u, "price": p, "value": u * p, "cost": cost[hid]}
            hv += u * p
        if self.a["valuation_mode"] == "manual":
            value = (val["value"] + flows_after_val) if val else contributed
        else:
            value = cash + hv
        return {"cash": cash, "holdings_value": hv, "holdings": per, "contributed": contributed,
                "value": value, "last_valuation": val}


def sample_dates(start, end, max_points=400):
    days = (end - start).days
    step = max(1, days // max_points)
    out = [start + timedelta(days=i) for i in range(0, days + 1, step)]
    if out[-1] != end:
        out.append(end)
    return out


# ---------------------------------------------------------------- interest

def estimate_interest(state: AccountState, rates, start, end):
    """Sum of daily interest on cash balance using the rate history (AER treated as simple daily)."""
    if not any(r["kind"] == "interest" for r in rates):
        return 0.0
    total = 0.0
    # compute daily cash balance incrementally
    txns = [t for t in state.txns if d(t["date"]) <= end]
    i = 0
    cash = 0.0
    x = start
    # balance before start
    while i < len(txns) and d(txns[i]["date"]) < start:
        cash += cash_effect(txns[i]); i += 1
    manual = state.a["valuation_mode"] == "manual"
    while x <= end:
        while i < len(txns) and d(txns[i]["date"]) <= x:
            cash += cash_effect(txns[i]); i += 1
        bal = state.snapshot(x)["value"] if manual else cash
        r = rate_on(rates, "interest", x) or 0.0
        total += max(bal, 0) * r / 100 / 365
        x += timedelta(days=1)
    return total


# ---------------------------------------------------------------- XIRR

def xirr(flows):
    """flows: list of (date, amount) with investor convention (money in negative)."""
    flows = [(d(a), b) for a, b in flows if abs(b) > 1e-9]
    if len(flows) < 2 or not (any(b < 0 for _, b in flows) and any(b > 0 for _, b in flows)):
        return None
    t0 = min(a for a, _ in flows)

    def npv(r):
        return sum(b / (1 + r) ** ((a - t0).days / 365.0) for a, b in flows)

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-7:
            break
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return mid
