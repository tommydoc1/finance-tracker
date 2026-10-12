/* Finance Tracker – single page frontend (no build step). */
'use strict';

// ------------------------------------------------------------ helpers
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const view = $('#view');
const GBP = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' });
const GBP0 = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP', maximumFractionDigits: 0 });
const fmt = v => (v == null || isNaN(v)) ? '—' : GBP.format(v);
const fmt0 = v => (v == null || isNaN(v)) ? '—' : GBP0.format(v);
const signed = v => (v == null) ? '—' : (v > 0 ? '+' : '') + GBP.format(v);
const pct = (v, dp = 2) => (v == null || isNaN(v)) ? '—' : `${(+v).toFixed(dp)}%`;
const cls = v => v > 0.005 ? 'up' : v < -0.005 ? 'down' : '';
const num = (v, dp = 4) => v == null ? '—' : (+v).toLocaleString('en-GB', { maximumFractionDigits: dp });
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fdate = s => s ? new Date(s.slice(0, 10) + 'T00:00').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }) : '—';
const today = () => new Date().toISOString().slice(0, 10);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

const ACCOUNT_TYPES = {
  stocks_isa: 'Stocks & Shares ISA', cash_isa: 'Cash ISA', lisa: 'Lifetime ISA', cash: 'Cash / savings',
  pension: 'Workplace pension', sipp: 'SIPP', gia: 'General investment',
};
const ISA = ['stocks_isa', 'cash_isa', 'lisa'];
const TXN = {
  deposit: 'Deposit', withdrawal: 'Withdrawal', contribution: 'Pension contribution', tax_relief: 'Tax relief',
  opening_balance: 'Opening balance', transfer_in: 'Transfer in', transfer_out: 'Transfer out',
  buy: 'Buy', sell: 'Sell', dividend: 'Dividend', interest: 'Interest', fee: 'Fee / charge', bonus: 'Bonus (e.g. LISA)',
  unit_adjust: 'Unit adjustment',
};
const RULES = {
  last_working_day: 'Last working day of month', first_working_day: 'First working day of month',
  day_or_before: 'Fixed day (earlier if weekend/holiday)', day_or_after: 'Fixed day (later if weekend/holiday)',
};
const RATE_KINDS = { interest: 'Interest rate (AER %)', fee_pct: 'Platform fee (% per year)', fee_flat: 'Flat fee (£ per month)' };

async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const r = await fetch('/api/' + path, {
    ...opts,
    headers: opts.body && !isForm ? { 'content-type': 'application/json' } : {},
    body: opts.body && !isForm ? JSON.stringify(opts.body) : opts.body,
  });
  if (!r.ok) {
    let m = await r.text();
    try { m = JSON.parse(m).detail; } catch { /* plain text */ }
    throw new Error(typeof m === 'string' ? m : JSON.stringify(m));
  }
  return r.json();
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), Math.max(2600, msg.length * 55));
}

async function guard(fn, ok) {
  try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast('⚠ ' + e.message); throw e; }
}

// ------------------------------------------------------------ modal form
function form({ title, fields, initial = {}, hint = '', submit = 'Save', onSubmit }) {
  const dlg = $('#modal'), body = $('#modal-body');
  $('#modal-title').textContent = title;
  $('#modal-hint').textContent = hint;
  $('#modal-submit').textContent = submit;
  body.innerHTML = fields.map(f => {
    const v = initial[f.name] ?? f.value ?? '';
    const req = f.required ? 'required' : '';
    let input;
    if (f.type === 'select') {
      const opts = typeof f.options === 'function' ? f.options(initial) : f.options;
      input = `<select name="${f.name}" ${req}>${opts.map(([k, l]) => `<option value="${esc(k)}" ${String(k) === String(v) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
    } else if (f.type === 'textarea') {
      input = `<textarea name="${f.name}" rows="2">${esc(v)}</textarea>`;
    } else if (f.type === 'file') {
      input = `<input type="file" name="${f.name}" accept="${f.accept || ''}" ${req}>`;
    } else {
      input = `<input name="${f.name}" type="${f.type || 'text'}" value="${esc(v)}" ${f.step ? `step="${f.step}"` : (f.type === 'number' ? 'step="any"' : '')} ${f.placeholder ? `placeholder="${esc(f.placeholder)}"` : ''} ${req}>`;
    }
    return `<label class="${f.full ? 'full' : ''}" data-field="${f.name}">${esc(f.label)}${input}${f.help ? `<span class="muted" style="font-weight:400">${esc(f.help)}</span>` : ''}</label>`;
  }).join('');

  const values = () => {
    const out = {};
    for (const f of fields) {
      const el = body.querySelector(`[name="${f.name}"]`);
      if (f.type === 'file') { out[f.name] = el.files[0]; continue; }
      let v = el.value;
      if (f.type === 'number' && v !== '') v = parseFloat(v);
      out[f.name] = v;
    }
    return out;
  };
  const refresh = () => {
    const vals = values();
    for (const f of fields) {
      const lab = body.querySelector(`[data-field="${f.name}"]`);
      const show = !f.show || f.show(vals);
      lab.style.display = show ? '' : 'none';
      const el = lab.querySelector('[name]');
      if (f.required) el.required = show;
      if (f.type === 'select' && typeof f.options === 'function' && f.dynamic) {
        const cur = el.value;
        el.innerHTML = f.options(vals).map(([k, l]) => `<option value="${esc(k)}">${esc(l)}</option>`).join('');
        if ([...el.options].some(o => o.value === cur)) el.value = cur;
      }
    }
  };
  body.oninput = refresh;
  refresh();
  const frm = $('#modal-form');
  frm.onsubmit = async e => {
    e.preventDefault();
    const vals = values();
    for (const f of fields) if (f.show && !f.show(vals)) delete vals[f.name];
    try {
      $('#modal-submit').disabled = true;
      await onSubmit(vals);
      dlg.close();
    } catch (err) {
      toast('⚠ ' + err.message);
    } finally { $('#modal-submit').disabled = false; }
  };
  $$('[data-close]', dlg).forEach(b => b.onclick = () => dlg.close());
  dlg.showModal();
}

function confirmDo(msg, fn) { if (confirm(msg)) return fn(); }

// ------------------------------------------------------------ charts
let charts = [];
function chart(canvas, cfg) {
  const ink = css('--muted'), line = css('--line');
  Chart.defaults.color = ink;
  Chart.defaults.borderColor = line;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  const c = new Chart(canvas, cfg);
  charts.push(c);
  return c;
}
const palette = () => ['--c1', '--c2', '--c3', '--c4', '--c5', '--c6'].map(css);
const moneyAxis = { ticks: { callback: v => GBP0.format(v) }, grid: { color: () => css('--line') } };
const tooltipMoney = { callbacks: { label: c => `${c.dataset.label}: ${fmt(c.parsed.y)}` } };

function growthChart(canvas, points, { stacked = false, accounts = [] } = {}) {
  const [c1, c2] = palette();
  const labels = points.map(p => p.date);
  const datasets = stacked
    ? accounts.map((a, i) => ({ label: a.name, data: points.map(p => p.by_account[a.id] ?? 0), fill: i === 0 ? 'origin' : '-1', borderWidth: 1.5, pointRadius: 0, tension: .2,
      borderColor: palette()[i % 6], backgroundColor: palette()[i % 6] + '55', stack: 'v' }))
    : [
      { label: 'Value', data: points.map(p => p.value), borderColor: c1, backgroundColor: c1 + '22', fill: true, borderWidth: 2, pointRadius: 0, tension: .2 },
      { label: 'Paid in', data: points.map(p => p.contributed), borderColor: c2, borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, stepped: true },
    ];
  return chart(canvas, {
    type: 'line', data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'bottom', labels: { boxWidth: 12 } }, tooltip: { ...tooltipMoney, callbacks: { ...tooltipMoney.callbacks, title: i => fdate(i[0].label) } } },
      scales: {
        x: { ticks: { maxTicksLimit: 8, callback(v) { return new Date(this.getLabelForValue(v)).toLocaleDateString('en-GB', { month: 'short', year: '2-digit' }); } }, grid: { display: false } },
        y: { ...moneyAxis, stacked },
      },
    },
  });
}

// ------------------------------------------------------------ shared data
let HOLDINGS = [];
let SUMMARY = null;
async function loadSummary() { SUMMARY = await api('summary'); return SUMMARY; }
async function loadHoldings() { HOLDINGS = await api('holdings'); return HOLDINGS; }
const accountOptions = (filter = () => true) => (SUMMARY?.accounts || []).filter(a => !a.archived && filter(a)).map(a => [a.id, a.name]);

// ------------------------------------------------------------ forms
function accountForm(acc) {
  form({
    title: acc ? 'Edit account' : 'Add account',
    initial: acc || { type: 'stocks_isa', valuation_mode: 'holdings' },
    hint: 'Valuation "from holdings" = cash + units × latest price (ISAs, cash accounts). "From statements" = you enter the value from your provider (typical for workplace pensions).',
    fields: [
      { name: 'name', label: 'Name', required: true, full: true, placeholder: 'e.g. Stocks & Shares ISA' },
      { name: 'type', label: 'Type', type: 'select', options: Object.entries(ACCOUNT_TYPES) },
      { name: 'provider', label: 'Provider', placeholder: 'e.g. Vanguard, Aviva' },
      { name: 'valuation_mode', label: 'Valuation', type: 'select', options: [['holdings', 'From holdings & cash'], ['manual', 'From statements']] },
      { name: 'opened_on', label: 'Opened', type: 'date' },
      ...(acc ? [{ name: 'archived', label: 'Status', type: 'select', options: [[0, 'Active'], [1, 'Archived (hidden)']] }] : []),
      { name: 'notes', label: 'Notes', type: 'textarea', full: true },
    ],
    onSubmit: async v => {
      if (acc) await api(`accounts/${acc.id}`, { method: 'PUT', body: v });
      else { const a = await api('accounts', { method: 'POST', body: v }); location.hash = `#/account/${a.id}`; }
      toast('Saved'); route();
    },
  });
}

async function txnForm(txn, accountId) {
  if (!SUMMARY) await loadSummary();
  await loadHoldings();
  const holdOpts = vals => [['', '—'], ...HOLDINGS.filter(h => String(h.account_id) === String(vals.account_id)).map(h => [h.id, `${h.ticker}${h.name ? ' – ' + h.name : ''}`])];
  const needsHolding = v => ['buy', 'sell', 'dividend'].includes(v.type);
  form({
    title: txn ? 'Edit transaction' : 'Add transaction',
    initial: txn || { account_id: accountId || accountOptions()[0]?.[0], date: today(), type: 'deposit' },
    hint: 'Amounts are always positive – the type sets the direction. For buys/sells enter the total cost/proceeds in £ including any fees or FX charges.',
    fields: [
      { name: 'account_id', label: 'Account', type: 'select', options: accountOptions(), full: true },
      { name: 'date', label: 'Date', type: 'date', required: true },
      { name: 'type', label: 'Type', type: 'select', options: Object.entries(TXN) },
      { name: 'amount', label: 'Amount (£)', type: 'number', step: '0.01', required: true },
      { name: 'holding_id', label: 'Holding', type: 'select', options: holdOpts, dynamic: true, show: needsHolding },
      { name: 'units', label: 'Units / shares', type: 'number', show: v => ['buy', 'sell'].includes(v.type), required: true },
      { name: 'notes', label: 'Notes', full: true },
    ],
    onSubmit: async v => {
      if (v.holding_id === '') v.holding_id = null;
      if (txn) await api(`transactions/${txn.id}`, { method: 'PUT', body: v });
      else await api('transactions', { method: 'POST', body: v });
      toast('Saved'); route();
    },
  });
}

function holdingForm(h, accountId) {
  form({
    title: h ? 'Edit holding' : 'Add holding',
    initial: h || { account_id: accountId },
    hint: 'Price symbol is used for automatic daily prices. ETFs and shares: the Yahoo Finance code – LSE lines end in .L (e.g. VUAG.L, VWRP.L). Pension and unit-trust funds with no ticker: the fund\'s ISIN (e.g. GB00B0ZDNB53), priced from FT.com. Pence are converted to £ automatically. Leave blank to enter prices by hand.',
    fields: [
      { name: 'account_id', label: 'Account', type: 'select', options: accountOptions(), full: true },
      { name: 'ticker', label: 'Ticker', required: true, placeholder: 'VUAG' },
      { name: 'price_symbol', label: 'Price symbol or ISIN', placeholder: 'VUAG.L or GB00B0ZDNB53' },
      { name: 'name', label: 'Name', full: true, placeholder: 'Vanguard S&P 500 UCITS ETF' },
      { name: 'isin', label: 'ISIN' },
      { name: 'ocf_pct', label: 'OCF / TER (% per year)', type: 'number', step: '0.001' },
      { name: 'notes', label: 'Notes', full: true },
    ],
    onSubmit: async v => {
      if (h) {
        await api(`holdings/${h.id}`, { method: 'PUT', body: v });
        if ((v.price_symbol || '').trim() !== (h.price_symbol || '').trim() && v.price_symbol) fetchAndReport({ ...h, ...v });
      } else {
        const nh = await api('holdings', { method: 'POST', body: v });
        if (nh.price_symbol) fetchAndReport(nh);
      }
      toast('Saved'); route();
    },
  });
}

function fetchAndReport(h) {
  toast(`Fetching prices for ${h.ticker}…`);
  return api(`holdings/${h.id}/fetch?range=max`, { method: 'POST' })
    .then(r => { toast(r.matched ? `${h.ticker}: matched ${r.matched} (${r.prices} prices)` : `Fetched ${r.prices} prices for ${h.ticker}`); route(); })
    .catch(e => toast('⚠ ' + e.message));
}

function priceForm(h) {
  form({
    title: `Add price – ${h.ticker}`, initial: { date: today() },
    hint: 'Price per unit in pounds (not pence). A manual price overrides an automatic one for that day.',
    fields: [{ name: 'date', label: 'Date', type: 'date', required: true }, { name: 'price', label: 'Price (£)', type: 'number', step: 'any', required: true }],
    onSubmit: async v => { await api(`holdings/${h.id}/prices`, { method: 'POST', body: v }); toast('Price saved'); route(); },
  });
}

function openingForm(holdings) {
  form({
    title: 'Add existing position', initial: { holding_id: holdings[0]?.id, date: today() },
    hint: 'For a fund you already hold when you start tracking (e.g. your pension). Enter the units your provider shows today and the total paid in so far (yours + employer + tax relief). Growth is then value minus paid in. Contributions from this date on are added by your recurring payment.',
    fields: [
      { name: 'holding_id', label: 'Fund', type: 'select', options: holdings.map(h => [h.id, h.ticker + (h.name ? ' – ' + h.name : '')]), full: true },
      { name: 'date', label: 'As of', type: 'date', required: true },
      { name: 'units', label: 'Units held', type: 'number', step: 'any', required: true },
      { name: 'paid_in', label: 'Total paid in so far (£)', type: 'number', step: '0.01', required: true, full: true },
    ],
    onSubmit: async v => { await api(`holdings/${v.holding_id}/opening`, { method: 'POST', body: v }); toast('Position added'); route(); },
  });
}

function setUnitsForm(h) {
  form({
    title: `Set units – ${h.ticker}`, initial: { date: today(), units: h.units ? +(+h.units).toFixed(4) : '' },
    hint: 'Enter the exact unit count shown by your provider. Auto-invested units are estimated from the daily price, and providers usually buy a few days after the payment date, so they drift slightly – this corrects the difference without changing what you paid in.',
    fields: [
      { name: 'date', label: 'As of', type: 'date', required: true },
      { name: 'units', label: 'Units held', type: 'number', step: 'any', required: true },
    ],
    onSubmit: async v => {
      const r = await api(`holdings/${h.id}/set-units`, { method: 'POST', body: v });
      toast(Math.abs(r.adjusted_by) > 0 ? `Adjusted by ${r.adjusted_by > 0 ? '+' : ''}${num(r.adjusted_by)} units` : 'Already matches');
      route();
    },
  });
}

function rateForm(r, accountId) {
  form({
    title: r ? 'Edit rate / fee' : 'Add rate or fee change',
    initial: r || { account_id: accountId, kind: 'interest', effective_date: today() },
    hint: 'Each entry applies from its date until the next one of the same kind – add a new row whenever your provider changes the rate, rather than editing the old one, so the history is kept.',
    fields: [
      { name: 'account_id', label: 'Account', type: 'select', options: accountOptions(), full: true },
      { name: 'kind', label: 'What', type: 'select', options: Object.entries(RATE_KINDS) },
      { name: 'effective_date', label: 'From', type: 'date', required: true },
      { name: 'value', label: 'Value', type: 'number', step: 'any', required: true },
      { name: 'notes', label: 'Notes', placeholder: 'e.g. BoE cut', },
    ],
    onSubmit: async v => {
      if (r) await api(`rates/${r.id}`, { method: 'PUT', body: v }); else await api('rates', { method: 'POST', body: v });
      toast('Saved'); route();
    },
  });
}

function valuationForm(v0, accountId) {
  form({
    title: v0 ? 'Edit valuation' : 'Add statement valuation',
    initial: v0 || { account_id: accountId, date: today() },
    hint: 'The total value shown by your provider on that date. Contributions after this date are added on top until the next valuation.',
    fields: [
      { name: 'account_id', label: 'Account', type: 'select', options: accountOptions(), full: true },
      { name: 'date', label: 'Date', type: 'date', required: true },
      { name: 'value', label: 'Value (£)', type: 'number', step: '0.01', required: true },
      { name: 'notes', label: 'Notes', full: true },
    ],
    onSubmit: async v => {
      if (v0) await api(`valuations/${v0.id}`, { method: 'PUT', body: v }); else await api('valuations', { method: 'POST', body: v });
      toast('Saved'); route();
    },
  });
}

async function recurringForm(r, accountId) {
  await loadHoldings();
  const investOpts = v => [['', 'Keep as cash'], ...HOLDINGS.filter(h => String(h.account_id) === String(v.account_id)).map(h => [h.id, `Buy ${h.ticker}${h.name ? ' – ' + h.name : ''}`])];
  form({
    title: r ? 'Edit recurring payment' : 'Add recurring payment',
    initial: r ? { ...r, invest_holding_id: r.invest_holding_id ?? '' } : { account_id: accountId, txn_type: 'contribution', rule: 'last_working_day', day: 28, start_date: today().slice(0, 8) + '01', tax_relief_pct: 0, invest_holding_id: '' },
    hint: 'Transactions are created automatically on each pay date (weekends and England & Wales bank holidays handled). Change the amount with "New amount" so past months keep their old value.',
    fields: [
      { name: 'account_id', label: 'Account', type: 'select', options: accountOptions(), full: true },
      { name: 'label', label: 'Label', required: true, placeholder: 'Pension contribution', full: true },
      { name: 'txn_type', label: 'Creates', type: 'select', options: [['contribution', 'Pension contribution'], ['deposit', 'Deposit'], ['fee', 'Fee'], ['withdrawal', 'Withdrawal']] },
      { name: 'rule', label: 'Pay date rule', type: 'select', options: Object.entries(RULES) },
      { name: 'day', label: 'Day of month', type: 'number', step: '1', show: v => v.rule?.startsWith('day_') },
      { name: 'start_date', label: 'Start', type: 'date', required: true },
      { name: 'end_date', label: 'End (optional)', type: 'date' },
      { name: 'tax_relief_pct', label: 'Add tax relief %', type: 'number', step: 'any', help: '25 for relief-at-source pensions; 0 for net pay / salary sacrifice', show: v => v.txn_type === 'contribution' },
      { name: 'invest_holding_id', label: 'Invest each payment', type: 'select', options: investOpts, dynamic: true, full: true,
        help: 'Converts each payment into units of this fund at that day\'s price (add the fund under Holdings first)', show: v => ['contribution', 'deposit'].includes(v.txn_type) },
      ...(r ? [{ name: 'active', label: 'Status', type: 'select', options: [[1, 'Active'], [0, 'Paused']] }]
        : [{ name: 'amount', label: 'Amount (£)', type: 'number', step: '0.01', required: true }]),
    ],
    onSubmit: async v => {
      const amount = v.amount; delete v.amount;
      if (r) await api(`recurring/${r.id}`, { method: 'PUT', body: v });
      else {
        const nr = await api('recurring', { method: 'POST', body: v });
        await api('recurring-amounts', { method: 'POST', body: { recurring_id: nr.id, effective_from: v.start_date, amount } });
      }
      toast('Saved'); route();
    },
  });
}

function amountForm(r) {
  form({
    title: `New amount – ${r.label}`, initial: { effective_from: `${new Date().getFullYear() + (new Date().getMonth() >= 3 ? 1 : 0)}-04-01` },
    hint: 'e.g. after your April pay rise. Payments on or after this date use the new amount. If you back-date it, use "Rebuild" to regenerate past payments.',
    fields: [
      { name: 'effective_from', label: 'From', type: 'date', required: true },
      { name: 'amount', label: 'Amount (£)', type: 'number', step: '0.01', required: true },
      { name: 'notes', label: 'Notes', placeholder: 'Annual pay rise', full: true },
    ],
    onSubmit: async v => { await api('recurring-amounts', { method: 'POST', body: { ...v, recurring_id: r.id } }); toast('Saved'); route(); },
  });
}

// ------------------------------------------------------------ views
function kpi(label, value, sub = '', extra = '') {
  return `<div class="kpi ${extra}"><div class="l">${label}</div><div class="v">${value}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>`;
}
const typeTag = t => `<span class="tag ${ISA.includes(t) ? 'isa' : ''}">${esc(ACCOUNT_TYPES[t] || t)}</span>`;

async function viewDashboard() {
  const [s, up] = await Promise.all([loadSummary(), api('upcoming?months=3')]);
  const t = s.totals, isa = s.isa;
  const live = s.accounts.filter(a => !a.archived);
  const gainPct = t.contributed > 0 ? t.gain / t.contributed * 100 : null;
  const isaPct = Math.min(100, isa.used / isa.allowance * 100);
  view.innerHTML = `
    <div class="page-head">
      <div><h1>Overview</h1><div class="sub">Tax year ${s.tax_year} · ${fdate(s.today)}</div></div>
      <div class="btns"><button id="b-refresh">Refresh prices</button><button class="primary" id="b-txn">Add transaction</button></div>
    </div>
    ${live.length ? '' : `<div class="empty card" style="margin-bottom:16px">No accounts yet. <button class="link" id="b-first">Add your first account</button> – e.g. your Stocks &amp; Shares ISA, cash account and pension.</div>`}
    <div class="kpis">
      ${kpi('Net worth', fmt(t.value), `${signed(t.gain)} all-time`, 'hero')}
      ${kpi('Paid in', fmt(t.contributed), t.tax_relief_total ? `incl. ${fmt(t.tax_relief_total)} tax relief` : 'deposits + contributions')}
      ${kpi('Growth', `<span class="${cls(t.gain)}">${pct(gainPct, 1)}</span>`, `Annualised (XIRR) ${t.xirr == null ? '—' : pct(t.xirr * 100, 1)}`)}
      ${kpi(`Dividends ${s.tax_year}`, fmt(t.dividends_ty), `${fmt(t.dividends_total)} all-time`)}
      ${kpi(`Interest ${s.tax_year}`, fmt(t.interest_ty), `≈ ${fmt(t.interest_est_annual)}/yr at current rates`)}
      ${kpi('Est. annual charges', fmt(t.charges_total_annual), `platform + fund OCFs · ${fmt(t.fees_ty)} paid this TY`)}
    </div>
    <div class="grid g3">
      <div class="card">
        <div class="card-head"><h2>Growth</h2>
          <div class="btns">
            <span class="pill-row" id="ranges">${['1Y', '3Y', 'All'].map(r => `<button class="sm ${r === 'All' ? 'primary' : ''}" data-r="${r}">${r}</button>`).join('')}</span>
            <button class="sm" id="b-stack">By account</button>
          </div>
        </div>
        <div class="chart-box"><canvas id="c-growth"></canvas></div>
      </div>
      <div class="stack">
        <div class="card">
          <h2>ISA allowance ${s.tax_year}</h2>
          <div class="bar ${isa.used > isa.allowance ? 'over' : ''}"><span style="width:${isaPct}%"></span></div>
          <div class="statline"><div>Used<b>${fmt(isa.used)}</b></div><div>Remaining<b class="${isa.remaining < 0 ? 'down' : ''}">${fmt(isa.remaining)}</b></div></div>
          <div class="note">Resets 6 April · tax year ends ${fdate(isa.tax_year_end)}${isa.lisa_used ? ` · LISA ${fmt(isa.lisa_used)} of ${fmt0(isa.lisa_allowance)}` : ''}. Transfers between ISAs don't count.</div>
        </div>
        <div class="card">
          <div class="card-head"><h2>Coming up</h2><a href="#/recurring" class="muted" style="font-size:12px">All</a></div>
          ${up.length ? `<table><tbody>${up.slice(0, 5).map(u => `<tr><td>${fdate(u.date)}<div class="muted" style="font-size:12px">${esc(u.label)}</div></td><td class="n">${fmt(u.amount)}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">No recurring payments set up.</div>'}
        </div>
      </div>
    </div>
    <h2 style="margin:24px 0 12px">Accounts</h2>
    <div class="acct-cards">
      ${live.map(a => `
        <a class="card acct" href="#/account/${a.id}">
          <div class="top"><b>${esc(a.name)}</b>${typeTag(a.type)}</div>
          <div class="val">${fmt(a.value)}</div>
          <div class="${cls(a.gain)}" style="font-size:13px">${signed(a.gain)} ${a.gain_pct != null ? `(${pct(a.gain_pct, 1)})` : ''}</div>
          <div class="statline">
            ${a.interest_rate != null ? `<div>Rate<b>${pct(a.interest_rate)}</b></div>` : ''}
            ${a.holdings.length ? `<div>Holdings<b>${a.holdings.length}</b></div>` : ''}
            ${a.dividends_ty ? `<div>Divs TY<b>${fmt(a.dividends_ty)}</b></div>` : ''}
            ${a.valuation_mode === 'manual' && a.last_valuation ? `<div>Statement<b>${fdate(a.last_valuation.date)}</b></div>` : ''}
          </div>
        </a>`).join('')}
      <button class="card acct empty" id="b-add-acct" style="cursor:pointer">+ Add account</button>
    </div>`;

  $('#b-txn').onclick = () => txnForm();
  $('#b-add-acct').onclick = () => accountForm();
  if ($('#b-first')) $('#b-first').onclick = () => accountForm();
  $('#b-refresh').onclick = () => guard(() => api('prices/refresh', { method: 'POST' }), 'Prices refreshed').then(route);

  let range = 'All', stacked = false;
  const draw = async () => {
    charts.forEach(c => c.destroy()); charts = [];
    const start = range === 'All' ? '' : new Date(Date.now() - (range === '1Y' ? 365 : 1095) * 864e5).toISOString().slice(0, 10);
    const ser = await api('series' + (start ? `?start=${start}` : ''));
    if (ser.points.length) growthChart($('#c-growth'), ser.points, { stacked, accounts: ser.accounts });
  };
  $$('#ranges button').forEach(b => b.onclick = () => { range = b.dataset.r; $$('#ranges button').forEach(x => x.classList.toggle('primary', x === b)); draw(); });
  $('#b-stack').onclick = e => { stacked = !stacked; e.target.classList.toggle('primary', stacked); draw(); };
  draw();
}

async function viewAccounts() {
  const s = await loadSummary();
  view.innerHTML = `
    <div class="page-head"><div><h1>Accounts</h1><div class="sub">${s.accounts.length} account${s.accounts.length === 1 ? '' : 's'}</div></div>
      <button class="primary" id="b-add">Add account</button></div>
    <div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Account</th><th>Type</th><th class="n">Value</th><th class="n">Paid in</th><th class="n">Gain</th><th class="n">XIRR</th><th class="n">Rate</th><th class="n">Charges/yr</th></tr></thead>
      <tbody>${s.accounts.map(a => `
        <tr style="cursor:pointer;${a.archived ? 'opacity:.55' : ''}" onclick="location.hash='#/account/${a.id}'">
          <td><b>${esc(a.name)}</b><div class="muted" style="font-size:12px">${esc(a.provider || '')}${a.archived ? ' · archived' : ''}</div></td>
          <td>${typeTag(a.type)}</td><td class="n">${fmt(a.value)}</td><td class="n">${fmt(a.contributed)}</td>
          <td class="n ${cls(a.gain)}">${signed(a.gain)}</td><td class="n">${a.xirr == null ? '—' : pct(a.xirr * 100, 1)}</td>
          <td class="n">${a.interest_rate == null ? '—' : pct(a.interest_rate)}</td><td class="n">${fmt(a.charges_total_annual)}</td>
        </tr>`).join('') || '<tr><td colspan="8" class="muted">No accounts yet.</td></tr>'}</tbody>
    </table></div></div>`;
  $('#b-add').onclick = () => accountForm();
}

const accTab = {};
async function viewAccount(id) {
  if (!SUMMARY) await loadSummary();
  const [a, txns, ser] = await Promise.all([api(`accounts/${id}`), api(`transactions?account_id=${id}`), api(`series?account_id=${id}`)]);
  await loadHoldings();
  const tabs = [['overview', 'Overview'], ['txns', `Transactions (${txns.length})`]];
  if (a.valuation_mode === 'holdings' && !['cash', 'cash_isa'].includes(a.type)) tabs.push(['holdings', 'Holdings']);
  tabs.push(['rates', 'Interest & fees']);
  if (a.valuation_mode === 'manual') tabs.push(['vals', 'Valuations']);
  tabs.push(['recurring', 'Recurring']);
  let tab = accTab[id] || 'overview';
  if (!tabs.some(t => t[0] === tab)) tab = 'overview';

  view.innerHTML = `
    <div class="page-head">
      <div><a href="#/accounts" class="muted" style="font-size:12px;text-decoration:none">← Accounts</a>
        <h1>${esc(a.name)}</h1><div class="sub">${typeTag(a.type)} ${esc(a.provider || '')}${a.valuation_mode === 'manual' ? ' · valued from statements' : ''}</div></div>
      <div class="btns">
        ${['stocks_isa', 'gia', 'cash_isa', 'lisa'].includes(a.type) ? '<button id="b-t212">Import Trading 212 CSV</button>' : ''}
        <button id="b-edit">Edit</button><button class="primary" id="b-txn">Add transaction</button>
      </div>
    </div>
    <div class="kpis">
      ${kpi('Value', fmt(a.value), a.valuation_mode === 'manual' ? (a.last_valuation ? `statement ${fdate(a.last_valuation.date)} + later payments` : 'no statement yet – showing paid in') : `cash ${fmt(a.cash)}`, 'hero')}
      ${kpi('Paid in', fmt(a.contributed), a.tax_relief_total ? `incl. ${fmt(a.tax_relief_total)} tax relief` : '')}
      ${kpi('Gain', `<span class="${cls(a.gain)}">${signed(a.gain)}</span>`, `${pct(a.gain_pct, 1)} · XIRR ${a.xirr == null ? '—' : pct(a.xirr * 100, 1)}`)}
      ${a.interest_rate != null ? kpi('Interest rate', pct(a.interest_rate), `≈ ${fmt(a.interest_est_annual)}/yr on current balance`) : ''}
      ${a.dividends_total ? kpi('Dividends', fmt(a.dividends_ty), `this TY · ${fmt(a.dividends_total)} all-time`) : ''}
      ${kpi('Est. charges / yr', fmt(a.charges_total_annual), `platform ${fmt(a.charges_platform_annual)} · funds ${fmt(a.charges_ocf_annual)}`)}
      ${ISA.includes(a.type) ? kpi(`ISA subs ${a.tax_year}`, fmt(a.isa_subscribed_ty), 'counts towards £20k') : ''}
    </div>
    <div class="tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${k === tab ? 'active' : ''}">${l}</button>`).join('')}</div>
    <div id="tab"></div>`;

  $('#b-edit').onclick = () => accountForm(a);
  $('#b-txn').onclick = () => txnForm(null, a.id);
  if ($('#b-t212')) $('#b-t212').onclick = () => t212Form(a.id);
  $$('.tabs button').forEach(b => b.onclick = () => { accTab[id] = b.dataset.tab; viewAccount(id); });
  const T = $('#tab');

  if (tab === 'overview') {
    T.innerHTML = `
      <div class="grid g3">
        <div class="card"><h2>Value vs paid in</h2><div class="chart-box">${ser.points.length ? '<canvas id="c-acc"></canvas>' : '<div class="empty">Add a transaction to see growth.</div>'}</div></div>
        <div class="card"><h2>Summary</h2><table><tbody>
          <tr><td>Interest received (TY / all)</td><td class="n">${fmt(a.interest_ty)} / ${fmt(a.interest_total)}</td></tr>
          ${a.interest_rate != null ? `<tr><td>Interest estimate this TY</td><td class="n">${fmt(a.interest_est_ty)}</td></tr><tr><td>Accruing this month (est.)</td><td class="n">${fmt(a.interest_est_month)}</td></tr>` : ''}
          <tr><td>Dividends (TY / all)</td><td class="n">${fmt(a.dividends_ty)} / ${fmt(a.dividends_total)}</td></tr>
          <tr><td>Fees paid (TY / all)</td><td class="n">${fmt(a.fees_ty)} / ${fmt(a.fees_total)}</td></tr>
          <tr><td>Platform fee</td><td class="n">${pct(a.fee_pct, 3)}${a.fee_flat ? ` + ${fmt(a.fee_flat)}/mo` : ''}</td></tr>
          ${a.valuation_mode === 'holdings' ? `<tr><td>Cash</td><td class="n">${fmt(a.cash)}</td></tr><tr><td>Invested</td><td class="n">${fmt(a.holdings_value)}</td></tr>` : ''}
        </tbody></table>${a.notes ? `<p class="note">${esc(a.notes)}</p>` : ''}</div>
      </div>`;
    if (ser.points.length) growthChart($('#c-acc'), ser.points);
  }

  if (tab === 'txns') {
    T.innerHTML = `<div class="card"><div class="table-wrap"><table>
      <thead><tr><th>Date</th><th>Type</th><th>Holding</th><th class="n">Units</th><th class="n">Amount</th><th>Notes</th><th></th></tr></thead>
      <tbody>${txns.map(t => {
      const neg = ['withdrawal', 'transfer_out', 'buy', 'fee'].includes(t.type);
      return `<tr><td>${fdate(t.date)}</td><td>${esc(TXN[t.type] || t.type)}${t.recurring_id ? ' <span class="tag">auto</span>' : ''}</td>
        <td>${esc(t.ticker || '')}</td><td class="n">${t.units ? num(t.units) : ''}</td>
        <td class="n ${neg ? 'down' : ''}">${neg ? '−' : ''}${fmt(t.amount)}</td><td class="muted">${esc(t.notes || '')}</td>
        <td class="actions"><button class="sm ghost" data-edit="${t.id}">Edit</button><button class="sm ghost danger" data-del="${t.id}">✕</button></td></tr>`;
    }).join('') || '<tr><td colspan="7" class="muted">No transactions yet.</td></tr>'}</tbody></table></div></div>`;
    $$('[data-edit]', T).forEach(b => b.onclick = () => txnForm(txns.find(t => t.id == b.dataset.edit)));
    $$('[data-del]', T).forEach(b => b.onclick = () => confirmDo('Delete this transaction?', () => guard(() => api(`transactions/${b.dataset.del}`, { method: 'DELETE' }), 'Deleted').then(route)));
  }

  if (tab === 'holdings') {
    const held = Object.fromEntries(a.holdings.map(h => [h.id, h]));
    const all = a.all_holdings.map(h => ({ ...h, ...(held[h.id] || { units: 0, value: 0, cost: 0, gain: 0, ocf_annual: 0 }) }));
    T.innerHTML = `<div class="card">
      <div class="card-head"><h2>Holdings</h2><div class="btns">${all.length ? '<button class="sm" id="b-open">Add existing position</button>' : ''}<button class="sm" id="b-addh">Add holding</button></div></div>
      <div class="table-wrap"><table>
      <thead><tr><th>Ticker</th><th class="n">Units</th><th class="n">Price</th><th class="n">Value</th><th class="n">Cost</th><th class="n">Gain</th><th class="n">OCF</th><th class="n">OCF £/yr</th><th class="n">Dividends</th><th></th></tr></thead>
      <tbody>${all.map(h => `<tr>
        <td><b>${esc(h.ticker)}</b><div class="muted" style="font-size:12px">${esc(h.name || '')}</div>${h.price_info ? `<div class="muted" style="font-size:11px" title="What the price symbol ${esc(h.price_symbol)} matched">${esc(h.price_symbol)} → ${esc(h.price_info)}</div>` : ''}</td>
        <td class="n">${num(h.units)}</td>
        <td class="n">${h.price != null ? fmt(h.price) : '—'}<div class="muted" style="font-size:11px">${h.price_date ? fdate(h.price_date) : (h.price_symbol ? 'no price yet' : 'manual')}</div></td>
        <td class="n">${fmt(h.value)}</td><td class="n">${fmt(h.cost)}</td>
        <td class="n ${cls(h.gain)}">${signed(h.gain)}${h.cost > 0 ? `<div style="font-size:11px">${pct(h.gain / h.cost * 100, 1)}</div>` : ''}</td>
        <td class="n">${pct(h.ocf_pct, 2)}</td><td class="n">${fmt(h.ocf_annual)}</td><td class="n">${fmt(h.dividends || 0)}</td>
        <td class="actions"><button class="sm ghost" data-chart="${h.id}">Chart</button><button class="sm ghost" data-units="${h.id}">Set units</button><button class="sm ghost" data-price="${h.id}">+ Price</button>
          ${h.price_symbol ? `<button class="sm ghost" data-fetch="${h.id}">Fetch</button>` : ''}<button class="sm ghost" data-edit="${h.id}">Edit</button><button class="sm ghost danger" data-del="${h.id}">✕</button></td>
      </tr>`).join('') || '<tr><td colspan="10" class="muted">No holdings yet – add one, then record buys as transactions.</td></tr>'}</tbody></table></div>
      <div id="price-chart"></div></div>`;
    $('#b-addh').onclick = () => holdingForm(null, a.id);
    if ($('#b-open')) $('#b-open').onclick = () => openingForm(all);
    $$('[data-units]', T).forEach(b => b.onclick = () => setUnitsForm(H(b.dataset.units)));
    const H = id => all.find(h => h.id == id);
    $$('[data-edit]', T).forEach(b => b.onclick = () => holdingForm(H(b.dataset.edit)));
    $$('[data-price]', T).forEach(b => b.onclick = () => priceForm(H(b.dataset.price)));
    $$('[data-fetch]', T).forEach(b => b.onclick = () => fetchAndReport(H(b.dataset.fetch)));
    $$('[data-del]', T).forEach(b => b.onclick = () => confirmDo('Delete this holding and its price history? Transactions are kept.', () => guard(() => api(`holdings/${b.dataset.del}`, { method: 'DELETE' }), 'Deleted').then(route)));
    $$('[data-chart]', T).forEach(b => b.onclick = async () => {
      const h = H(b.dataset.chart), prices = await api(`holdings/${h.id}/prices`);
      $('#price-chart').innerHTML = `<h2 style="margin-top:20px">${esc(h.ticker)} price</h2><div class="chart-box sm">${prices.length ? '<canvas id="c-price"></canvas>' : '<div class="empty">No prices yet.</div>'}</div>`;
      if (!prices.length) return;
      chart($('#c-price'), {
        type: 'line', data: { labels: prices.map(p => p.date), datasets: [{ label: h.ticker, data: prices.map(p => p.price), borderColor: palette()[2], pointRadius: 0, borderWidth: 1.5 }] },
        options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: tooltipMoney }, scales: { x: { ticks: { maxTicksLimit: 8 }, grid: { display: false } }, y: moneyAxis } },
      });
    });
  }

  if (tab === 'rates') {
    const kinds = Object.keys(RATE_KINDS);
    T.innerHTML = `<div class="grid g2">
      <div class="card"><div class="card-head"><h2>Rate & fee history</h2><button class="sm" id="b-addr">Add change</button></div>
        <div class="table-wrap"><table><thead><tr><th>From</th><th>What</th><th class="n">Value</th><th>Notes</th><th></th></tr></thead>
        <tbody>${[...a.rates].reverse().map(r => `<tr><td>${fdate(r.effective_date)}</td><td>${esc(RATE_KINDS[r.kind])}</td>
          <td class="n">${r.kind === 'fee_flat' ? fmt(r.value) : pct(r.value, 3)}</td><td class="muted">${esc(r.notes || '')}</td>
          <td class="actions"><button class="sm ghost" data-edit="${r.id}">Edit</button><button class="sm ghost danger" data-del="${r.id}">✕</button></td></tr>`).join('')
      || '<tr><td colspan="5" class="muted">No rates recorded. Add your interest rate (AER) and any platform fee.</td></tr>'}</tbody></table></div>
        <p class="note">Interest estimates use your daily cash balance × the rate in force on each day. Compare with the actual interest transactions you record to spot changes.</p>
      </div>
      <div class="card"><h2>Interest rate over time</h2><div class="chart-box sm">${a.rates.some(r => r.kind === 'interest') ? '<canvas id="c-rate"></canvas>' : '<div class="empty">No interest rates yet.</div>'}</div>
        <div class="statline"><div>Est. this month<b>${fmt(a.interest_est_month)}</b></div><div>Est. this TY<b>${fmt(a.interest_est_ty)}</b></div><div>Received this TY<b>${fmt(a.interest_ty)}</b></div></div>
      </div></div>`;
    $('#b-addr').onclick = () => rateForm(null, a.id);
    $$('[data-edit]', T).forEach(b => b.onclick = () => rateForm(a.rates.find(r => r.id == b.dataset.edit)));
    $$('[data-del]', T).forEach(b => b.onclick = () => confirmDo('Delete this rate entry?', () => guard(() => api(`rates/${b.dataset.del}`, { method: 'DELETE' }), 'Deleted').then(route)));
    const ir = a.rates.filter(r => r.kind === 'interest');
    if (ir.length) {
      const pts = ir.map(r => ({ x: r.effective_date, y: r.value })).concat([{ x: today(), y: ir[ir.length - 1].value }]);
      chart($('#c-rate'), {
        type: 'line', data: { labels: pts.map(p => p.x), datasets: [{ label: 'AER', data: pts.map(p => p.y), stepped: true, borderColor: palette()[0], pointRadius: 3, borderWidth: 2 }] },
        options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => pct(c.parsed.y) } } }, scales: { x: { grid: { display: false } }, y: { ticks: { callback: v => v + '%' } } } },
      });
    }
  }

  if (tab === 'vals') {
    T.innerHTML = `<div class="card"><div class="card-head"><h2>Statement valuations</h2><button class="sm" id="b-addv">Add valuation</button></div>
      <table><thead><tr><th>Date</th><th class="n">Value</th><th>Notes</th><th></th></tr></thead><tbody>
      ${a.valuations.map(v => `<tr><td>${fdate(v.date)}</td><td class="n">${fmt(v.value)}</td><td class="muted">${esc(v.notes || '')}</td>
        <td class="actions"><button class="sm ghost" data-edit="${v.id}">Edit</button><button class="sm ghost danger" data-del="${v.id}">✕</button></td></tr>`).join('')
      || '<tr><td colspan="4" class="muted">Log in to your pension provider and add the current value here – then update it every month or so.</td></tr>'}
      </tbody></table></div>`;
    $('#b-addv').onclick = () => valuationForm(null, a.id);
    $$('[data-edit]', T).forEach(b => b.onclick = () => valuationForm(a.valuations.find(v => v.id == b.dataset.edit)));
    $$('[data-del]', T).forEach(b => b.onclick = () => confirmDo('Delete this valuation?', () => guard(() => api(`valuations/${b.dataset.del}`, { method: 'DELETE' }), 'Deleted').then(route)));
  }

  if (tab === 'recurring') {
    T.innerHTML = recurringCards(a.recurring) + `<button class="primary" id="b-addrec" style="margin-top:12px">Add recurring payment</button>`;
    bindRecurring(T, a.recurring);
    $('#b-addrec').onclick = () => recurringForm(null, a.id);
  }
}

function recurringCards(recs) {
  if (!recs.length) return '<div class="empty">No recurring payments for this account.</div>';
  return `<div class="grid g2">${recs.map(r => `
    <div class="card"><div class="card-head"><h2>${esc(r.label)} ${r.active ? '' : '<span class="tag">paused</span>'}</h2>
      <div class="btns"><button class="sm" data-amt="${r.id}">New amount</button><button class="sm ghost" data-redit="${r.id}">Edit</button>
      <button class="sm ghost" data-rebuild="${r.id}">Rebuild</button><button class="sm ghost danger" data-rdel="${r.id}">✕</button></div></div>
      <div class="muted" style="margin-bottom:8px">${esc(r.account_name)} · ${esc(RULES[r.rule])}${r.rule.startsWith('day_') ? ` (${r.day})` : ''} · from ${fdate(r.start_date)}${r.end_date ? ` to ${fdate(r.end_date)}` : ''}${r.tax_relief_pct ? ` · +${r.tax_relief_pct}% tax relief` : ''}${r.invest_holding_id ? ` · invests in ${esc((HOLDINGS.find(h => h.id === r.invest_holding_id) || {}).ticker || 'fund')}` : ''}</div>
      <table><thead><tr><th>Amount from</th><th class="n">Amount</th><th>Notes</th><th></th></tr></thead><tbody>
      ${r.amounts.map(x => `<tr><td>${fdate(x.effective_from)}</td><td class="n">${fmt(x.amount)}</td><td class="muted">${esc(x.notes || '')}</td><td class="actions"><button class="sm ghost danger" data-adel="${x.id}">✕</button></td></tr>`).join('')}
      </tbody></table></div>`).join('')}</div>`;
}

function bindRecurring(el, recs) {
  const R = id => recs.find(r => r.id == id);
  $$('[data-amt]', el).forEach(b => b.onclick = () => amountForm(R(b.dataset.amt)));
  $$('[data-redit]', el).forEach(b => b.onclick = () => recurringForm(R(b.dataset.redit)));
  $$('[data-rebuild]', el).forEach(b => b.onclick = () => confirmDo('Delete and regenerate all automatic payments for this schedule (manual skips are reset)?',
    () => guard(() => api(`recurring/${b.dataset.rebuild}/rebuild`, { method: 'POST' })).then(r => { toast(`${r.created} payments regenerated`); route(); })));
  $$('[data-rdel]', el).forEach(b => b.onclick = () => confirmDo('Delete this schedule? Payments already created are kept.', () => guard(() => api(`recurring/${b.dataset.rdel}`, { method: 'DELETE' }), 'Deleted').then(route)));
  $$('[data-adel]', el).forEach(b => b.onclick = () => confirmDo('Delete this amount?', () => guard(() => api(`recurring-amounts/${b.dataset.adel}`, { method: 'DELETE' }), 'Deleted').then(route)));
}

async function viewRecurring() {
  if (!SUMMARY) await loadSummary();
  const [recs, up] = await Promise.all([api('recurring'), api('upcoming?months=12'), loadHoldings()]);
  view.innerHTML = `
    <div class="page-head"><div><h1>Recurring payments</h1><div class="sub">Pension contributions and regular deposits, created automatically on each pay date</div></div>
      <button class="primary" id="b-add">Add recurring payment</button></div>
    ${recurringCards(recs)}
    <div class="card" style="margin-top:16px"><h2>Next 12 months</h2><div class="table-wrap"><table>
      <thead><tr><th>Date</th><th>Payment</th><th>Account</th><th class="n">Amount</th></tr></thead>
      <tbody>${up.map(u => `<tr><td>${fdate(u.date)} <span class="muted">${new Date(u.date).toLocaleDateString('en-GB', { weekday: 'short' })}</span></td><td>${esc(u.label)}</td><td>${esc(u.account_name)}</td><td class="n">${fmt(u.amount)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">Nothing scheduled.</td></tr>'}</tbody>
    </table></div></div>`;
  $('#b-add').onclick = () => recurringForm();
  bindRecurring(view, recs);
}

async function viewIncome() {
  const [inc, s] = await Promise.all([api('income'), loadSummary()]);
  const tys = Object.keys(inc.by_tax_year).sort().reverse();
  const months = Object.keys(inc.by_month).sort().slice(-24);
  const live = s.accounts.filter(a => !a.archived);
  view.innerHTML = `
    <div class="page-head"><div><h1>Income &amp; charges</h1><div class="sub">Dividends, interest and what you pay in fees</div></div></div>
    <div class="kpis">
      ${kpi(`Dividends ${s.tax_year}`, fmt(s.totals.dividends_ty), `${fmt(s.totals.dividends_total)} all-time`)}
      ${kpi(`Interest ${s.tax_year}`, fmt(s.totals.interest_ty), `${fmt(s.totals.interest_total)} all-time`)}
      ${kpi('Interest at current rates', fmt(s.totals.interest_est_annual), 'per year, estimated')}
      ${kpi('Est. annual charges', fmt(s.totals.charges_total_annual), `${pct(s.totals.value ? s.totals.charges_total_annual / s.totals.value * 100 : null, 2)} of portfolio`)}
    </div>
    <div class="grid g3">
      <div class="card"><h2>Monthly income</h2><div class="chart-box">${months.length ? '<canvas id="c-inc"></canvas>' : '<div class="empty">Record dividends or interest to see them here.</div>'}</div></div>
      <div class="card"><h2>Dividends by holding</h2><table><tbody>
        ${Object.entries(inc.by_holding).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${fmt(v)}</td></tr>`).join('') || '<tr><td class="muted">None yet.</td></tr>'}
      </tbody></table></div>
    </div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card"><h2>By tax year</h2><div class="table-wrap"><table>
        <thead><tr><th>Tax year</th><th class="n">Dividends</th><th class="n">Interest</th><th class="n">Bonus / relief</th><th class="n">Fees</th></tr></thead>
        <tbody>${tys.map(ty => { const r = inc.by_tax_year[ty]; return `<tr><td>${ty}</td><td class="n">${fmt(r.dividend || 0)}</td><td class="n">${fmt(r.interest || 0)}</td><td class="n">${fmt((r.bonus || 0) + (r.tax_relief || 0))}</td><td class="n down">${fmt(r.fee || 0)}</td></tr>`; }).join('') || '<tr><td colspan="5" class="muted">No data yet.</td></tr>'}</tbody>
      </table></div></div>
      <div class="card"><h2>Estimated annual charges</h2><div class="table-wrap"><table>
        <thead><tr><th>Account</th><th class="n">Platform</th><th class="n">Fund OCFs</th><th class="n">Total</th><th class="n">% of value</th></tr></thead>
        <tbody>${live.map(a => `<tr><td>${esc(a.name)}</td><td class="n">${fmt(a.charges_platform_annual)}</td><td class="n">${fmt(a.charges_ocf_annual)}</td><td class="n">${fmt(a.charges_total_annual)}</td><td class="n">${pct(a.value ? a.charges_total_annual / a.value * 100 : null)}</td></tr>`).join('')}</tbody>
      </table></div><p class="note">OCFs are taken inside the fund price, so you never see them as a transaction – this is what they cost you at today's values. Add platform fees under each account's "Interest &amp; fees" tab.</p></div>
    </div>`;
  if (months.length) {
    const [c1, c2, , , , c6] = palette();
    chart($('#c-inc'), {
      type: 'bar',
      data: {
        labels: months.map(m => new Date(m + '-01').toLocaleDateString('en-GB', { month: 'short', year: '2-digit' })), datasets: [
          { label: 'Dividends', data: months.map(m => inc.by_month[m].dividend || 0), backgroundColor: c1, stack: 'i' },
          { label: 'Interest', data: months.map(m => inc.by_month[m].interest || 0), backgroundColor: c2, stack: 'i' },
          { label: 'Fees', data: months.map(m => -(inc.by_month[m].fee || 0)), backgroundColor: c6, stack: 'i' },
        ],
      },
      options: { maintainAspectRatio: false, plugins: { legend: { position: 'bottom', labels: { boxWidth: 12 } }, tooltip: tooltipMoney }, scales: { x: { stacked: true, grid: { display: false } }, y: { ...moneyAxis, stacked: true } } },
    });
  }
}

function t212Form(accountId) {
  form({
    title: 'Import Trading 212 history', submit: 'Import', initial: { account_id: accountId },
    hint: 'In Trading 212: Menu → History → Export history (CSV). Imports buys, sells, deposits, withdrawals, dividends and interest. Re-importing an overlapping file is safe – duplicates are skipped. New holdings are created with a guessed price symbol you can check.',
    fields: [
      { name: 'account_id', label: 'Into account', type: 'select', options: accountOptions(), full: true },
      { name: 'file', label: 'CSV file', type: 'file', accept: '.csv', required: true, full: true },
    ],
    onSubmit: async v => {
      const fd = new FormData(); fd.append('file', v.file);
      const r = await api(`import/trading212/${v.account_id}`, { method: 'POST', body: fd });
      toast(`Imported ${r.imported}, skipped ${r.skipped}${r.unknown.length ? ` · ignored: ${r.unknown.join(', ')}` : ''}`);
      route();
    },
  });
}

// ------------------------------------------------------------ appearance
const THEMES = {
  auto: { name: 'Auto (match device)', pv: ['#f4f3ef', '#ffffff', '#1c1f1e', '#1f6f5c'], pv2: ['#121514', '#1a1e1d', '#e8ebe9', '#5cc0a2'] },
  light: { name: 'Light', pv: ['#f4f3ef', '#ffffff', '#1c1f1e', '#1f6f5c'] },
  dark: { name: 'Dark', pv: ['#121514', '#1a1e1d', '#e8ebe9', '#5cc0a2'] },
  black: { name: 'Black & white', pv: ['#000000', '#000000', '#ffffff', '#ffffff'] },
  midnight: { name: 'Midnight blue', pv: ['#0b1220', '#111a2e', '#e6ebf5', '#6aa6ff'] },
  paper: { name: 'Paper', pv: ['#efe6d4', '#faf5ea', '#33291d', '#8a4b1f'] },
};
const ACCENTS = [['', 'Theme default'], ['#1f6f5c', 'Green'], ['#0f8b8d', 'Teal'], ['#2f6fd6', 'Blue'], ['#7b4fc9', 'Purple'],
  ['#d6336c', 'Pink'], ['#c2413a', 'Red'], ['#d9822b', 'Orange'], ['#b8860b', 'Gold'], ['#5b6470', 'Slate']];
const SIZES = { small: ['Small', 0.92], normal: ['Normal', 1], large: ['Large', 1.12], xlarge: ['Extra large', 1.25] };
let UI = { theme: 'auto', accent: '', size: 'normal' };

function onColour(hex) { // black or white text, whichever reads better on this colour
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(c => c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 ? '#000000' : '#ffffff';
}

function applyUI(p) {
  UI = { ...UI, ...p };
  const root = document.documentElement, st = root.style;
  root.dataset.theme = THEMES[UI.theme] ? UI.theme : 'auto';
  if (UI.accent) {
    const on = onColour(UI.accent);
    st.setProperty('--accent', UI.accent); st.setProperty('--c1', UI.accent);
    st.setProperty('--accent-soft', `color-mix(in srgb, ${UI.accent} 18%, var(--panel))`);
    st.setProperty('--on-accent', on);
    st.setProperty('--on-accent-muted', on === '#ffffff' ? 'rgba(255,255,255,.8)' : 'rgba(0,0,0,.7)');
  } else ['--accent', '--c1', '--accent-soft', '--on-accent', '--on-accent-muted'].forEach(v => st.removeProperty(v));
  const scale = (SIZES[UI.size] || SIZES.normal)[1];
  st.setProperty('--text-scale', scale);
  if (window.Chart) Chart.defaults.font.size = Math.round(12 * scale);
  try { localStorage.setItem('ui', JSON.stringify(UI)); } catch { /* storage unavailable */ }
}

async function saveUI(p) {
  applyUI(p);
  await guard(() => api('settings/ui', { method: 'PUT', body: UI }));
  route(); // redraw so charts pick up the new colours
}

function appearanceCard() {
  const tile = (k, t) => {
    const [bg, panel, ink, acc] = t.pv, d = t.pv2;
    const pv = `<div class="pv" style="background:${d ? `linear-gradient(135deg, ${bg} 50%, ${d[0]} 50%)` : bg}">
      <div class="side" style="background:${panel};border:1px solid ${ink}22"></div>
      <div class="main" style="background:${panel};border:1px solid ${ink}22"><div class="ln" style="background:${acc};width:55%"></div>
      <div class="ln" style="background:${ink};opacity:.7"></div><div class="ln" style="background:${ink};opacity:.35;width:70%"></div></div></div>`;
    return `<button class="theme-tile ${UI.theme === k ? 'active' : ''}" data-theme-pick="${k}">${pv}<div class="nm">${esc(t.name)}</div></button>`;
  };
  return `<div class="card" style="grid-column:1/-1"><h2>Appearance</h2>
    <div class="settings-label">Theme</div>
    <div class="theme-grid">${Object.entries(THEMES).map(([k, t]) => tile(k, t)).join('')}</div>
    <div class="settings-label">Accent colour</div>
    <div class="swatches">${ACCENTS.map(([c, n]) => `<button class="swatch ${c ? '' : 'default'} ${UI.accent === c ? 'active' : ''}" data-accent="${c}" title="${n}" aria-label="${n}" style="${c ? `background:${c}` : ''}"></button>`).join('')}</div>
    <div class="settings-label">Text size</div>
    <div><div class="seg">${Object.entries(SIZES).map(([k, [n]]) => `<button data-size="${k}" class="${UI.size === k ? 'active' : ''}">${n}</button>`).join('')}</div></div>
    <p class="note">Saved on the server, so it applies on every device you sign in from.</p></div>`;
}

async function viewData() {
  if (!SUMMARY) await loadSummary();
  const health = await api('health');
  view.innerHTML = `
    <div class="page-head"><div><h1>Data &amp; settings</h1><div class="sub">Version ${esc(String(health.version).slice(0, 7))}</div></div></div>
    <div class="grid g2">
      ${appearanceCard()}
      <div class="card"><h2>Backup</h2><p class="muted">Your data lives in one SQLite file in the container's <code>/data</code> volume. Download a JSON backup regularly – it can be restored here.</p>
        <div class="btns"><a href="/api/export"><button class="primary">Download backup (JSON)</button></a><a href="/api/export/transactions.csv"><button>Transactions (CSV)</button></a><button id="b-restore" class="danger">Restore backup…</button></div></div>
      <div class="card"><h2>Imports</h2><p class="muted">Bring in history from your broker instead of typing it.</p>
        <div class="btns"><button id="b-t212">Trading 212 CSV…</button></div></div>
      <div class="card"><h2>Prices</h2><p class="muted">Holdings with a price symbol update automatically every few hours. Last refresh: ${SUMMARY.prices_refreshed_at ? esc(SUMMARY.prices_refreshed_at.replace('T', ' ')) : 'never'}.</p>
        <div class="btns"><button id="b-prices">Refresh now</button></div></div>
      <div class="card"><h2>Bank holidays</h2><p class="muted">Pay dates use England &amp; Wales bank holidays computed from the standard rules. Sync from gov.uk to also pick up one-off holidays (e.g. coronations).</p>
        <div class="btns"><button id="b-bh">Sync from gov.uk</button></div>
        <div id="paydates" class="note"></div></div>
    </div>`;
  $$('[data-theme-pick]').forEach(b => b.onclick = () => saveUI({ theme: b.dataset.themePick }));
  $$('[data-accent]').forEach(b => b.onclick = () => saveUI({ accent: b.dataset.accent }));
  $$('[data-size]').forEach(b => b.onclick = () => saveUI({ size: b.dataset.size }));
  $('#b-t212').onclick = () => t212Form();
  $('#b-prices').onclick = () => guard(() => api('prices/refresh', { method: 'POST' }), 'Prices refreshed');
  $('#b-bh').onclick = () => guard(() => api('bank-holidays/sync', { method: 'POST' })).then(r => toast(`Synced ${r.dates} bank holidays`));
  $('#b-restore').onclick = () => form({
    title: 'Restore backup', submit: 'Replace all data', hint: 'This REPLACES everything currently stored with the contents of the backup file.',
    fields: [{ name: 'file', label: 'Backup JSON', type: 'file', accept: '.json', required: true, full: true }],
    onSubmit: async v => { const fd = new FormData(); fd.append('file', v.file); await api('import', { method: 'POST', body: fd }); toast('Backup restored'); location.hash = '#/'; },
  });
  const y = new Date().getFullYear();
  const pd = await api(`paydates?year=${y}`);
  $('#paydates').innerHTML = `Last-working-day pay dates ${y}: ` + pd.map(p => new Date(p.date).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })).join(' · ');
}

// ------------------------------------------------------------ router
async function route() {
  charts.forEach(c => c.destroy()); charts = [];
  const [page, id] = location.hash.replace(/^#\/?/, '').split('/');
  const nav = page === 'account' ? 'accounts' : (page || 'dashboard');
  $$('nav a').forEach(a => a.classList.toggle('active', a.dataset.nav === nav));
  try {
    if (!page) await viewDashboard();
    else if (page === 'accounts') await viewAccounts();
    else if (page === 'account') await viewAccount(+id);
    else if (page === 'income') await viewIncome();
    else if (page === 'recurring') await viewRecurring();
    else if (page === 'data') await viewData();
    else view.innerHTML = '<div class="empty">Not found.</div>';
  } catch (e) {
    view.innerHTML = `<div class="empty">Something went wrong: ${esc(e.message)}</div>`;
    console.error(e);
  }
  if (SUMMARY) $('#foot').textContent = `v${String(SUMMARY.version).slice(0, 7)} · ${SUMMARY.tax_year}`;
}
window.addEventListener('hashchange', route);
// re-draw charts when the device switches light/dark while on "Auto"
window.matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => { if (UI.theme === 'auto') route(); });
try { applyUI(JSON.parse(localStorage.getItem('ui') || '{}')); } catch { applyUI({}); }
api('settings/ui').then(p => {
  const changed = JSON.stringify({ ...UI, ...p }) !== JSON.stringify(UI);
  applyUI(p);
  if (changed) route();
}).catch(() => { /* keep cached appearance */ });
route();
