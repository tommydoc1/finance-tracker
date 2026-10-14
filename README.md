# Finance Tracker

A self-hosted personal finance tracker for UK ISAs, cash savings and pensions. One Docker container, one SQLite file, no cloud.

**Tracks**
- Accounts: Stocks & Shares ISA, Cash ISA, LISA, cash/savings, workplace pension, SIPP, GIA
- Holdings (ETFs/funds/shares) with units, cost, value, gain, and **automatic daily prices** from Yahoo Finance (e.g. `VUAG.L` – pence converted to £)
- **Pension & unit-trust funds without a ticker**: use the fund's ISIN as the price symbol (e.g. `GB00B0ZDNB53`) and prices come daily from FT.com
- **Auto-invest**: recurring contributions can buy units of a fund at that day's price; **Set units** reconciles with the count your provider shows
- **Interest rate history** per account – every change is kept, and interest is estimated daily from your actual balance × the rate in force that day
- **Charges**: platform % fee, flat monthly fees, and each fund's OCF/TER → estimated £/year, plus actual fees you record
- **Automatic dividends**: distributing ETFs get each dividend from Yahoo (units held before the ex-date × amount per share), paid as cash or **reinvested** into more units
- **Daily compounding interest** on cash (Trading 212's AER method), credited automatically
- **Dividend & interest income** by month, tax year and holding
- **Recurring payments** – e.g. a monthly pension contribution, auto-created on the last working day of each month (weekends + England & Wales bank holidays handled). Add a "new amount" from April for pay rises; past months keep the old figure
- Growth: value vs paid-in over time, gain £/%, annualised return (XIRR)
- **ISA allowance** used/remaining for the current tax year (6 April – 5 April)
- Trading 212 CSV import, JSON backup/restore, CSV export
- Light/dark mode, works on mobile

---

## Setup (one-off)

### 1. Put the code on GitHub
```bash
cd finance-tracker
git init -b main && git add . && git commit -m "Initial commit"
# create an empty repo called finance-tracker on github.com (private is fine), then:
git remote add origin https://github.com/<you>/finance-tracker.git
git push -u origin main
```
The push triggers **Actions → Build & publish image**, which builds the Docker image and publishes it to `ghcr.io/<you>/finance-tracker:latest`. Wait for the green tick (~2 min).

### 2. Allow your server to pull the image
GitHub → your profile → Packages → `finance-tracker` → Package settings → Change visibility → **Public**. The image contains only code, never data.
(Keeping it private instead means giving Portainer and Watchtower a ghcr.io token with `read:packages`.)

### 3. Run it (Portainer)
Stacks → Add stack → Web editor → paste `docker-compose.yml`. Under **Environment variables** add `APP_USERNAME` and `APP_PASSWORD` (Portainer saves them to `stack.env`, which the compose file loads). Adjust the volume path, then **Deploy the stack**.

Open `http://<server-ip>:8080` and sign in.

## Deploying changes
Edit code → `git push` → GitHub Actions builds a new image → your Watchtower (respecting the `com.centurylinklabs.watchtower.enable` label) pulls it and recreates the container. The data volume is never touched. The commit hash shows in the sidebar footer and on Data & settings so you can confirm what's running.

## Getting started in the app
1. **Accounts → Add account** for each of: your S&S ISA, cash account, pension.
   - Pension: choose **Valuation: From statements** – you'll type in the value from your provider occasionally.
   - If an account already has money in it, add an **Opening balance** transaction (what you'd paid in so far) and, for statement accounts, a **valuation** on the same date.
2. **Recurring → Add recurring payment**: pension account, your monthly amount, *Last working day of month*, start date = when contributions began. Past months are back-filled automatically. In April add a **New amount**.
3. **Interest & fees** tab on each account: add the current AER (and platform fee if any). When the rate changes, add a new row – don't edit the old one.
4. **Holdings** tab on the ISA: add each ETF with its price symbol and OCF, then record buys (or import your Trading 212 history CSV).

## Backups
Everything is in `finance.db` inside the data volume. Use **Data & settings → Download backup** regularly, or back up the `data` folder (stop the container first, or copy the `.db`, `.db-wal` and `.db-shm` together).

## Security
Sign-in uses its own page and a signed, HttpOnly session cookie (it holds only an expiry time and a signature – never the password). You're signed out after `SESSION_MINUTES` (default 60) without activity; changing `APP_USERNAME`/`APP_PASSWORD` signs every device out. After 5 wrong passwords from one address, sign-in pauses for a few minutes. Scripts can still use HTTP Basic auth.

On your home network this is fine over plain http. Don't expose port 8080 to the internet directly – put it behind your reverse proxy with HTTPS (or Tailscale/WireGuard) if you want remote access.

## Local development
```bash
pip install -r requirements.txt
DB_PATH=./data/dev.db AUTO_PRICES=false uvicorn app.main:app --reload --port 8080
```
API docs at `/api/docs`.

## Notes / limitations
- Interest estimates treat AER as simple daily interest – close enough to compare against what's actually paid.
- Prices for non-GBP listings are converted at today's FX rate across all history.
- Bank holidays are England & Wales (computed from the rules; "Sync from gov.uk" adds one-offs).
