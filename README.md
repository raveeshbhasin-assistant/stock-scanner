# Momentum Scanner

An automated day-trading research tool that scans S&P 500 / Nasdaq 100 stocks
five times daily and publishes results to GitHub Pages.

---

## How It Works

`scanner.py` applies a strict 4-rule framework:

| Rule | Filter |
|------|--------|
| 1. Liquidity | Market Cap > $50B · ADV > 5M shares · ATR ≥ 2.5% |
| 2. Momentum  | Price > VWAP · RVOL > 1.5x · Above 9/20-EMA · No climax volume |
| 3. Catalyst  | Sentiment > 75% positive · Outperforming SPY/QQQ |
| 4. R/R       | Runway to resistance ≥ 2% · Risk/Reward ≥ 1:2 |

Results are written to `index.html` and served via GitHub Pages.

---

## Setup (One-Time)

### 1. Fork / Clone This Repo

Push all files to a new GitHub repository (public or private).

### 2. Add Your API Key as a Secret

1. Go to your repo on GitHub
2. Click **Settings → Secrets and variables → Actions**
3. Click **New repository secret**
4. Name: `ALPHA_VANTAGE_KEY`
5. Value: your Alpha Vantage API key
6. Click **Add secret**

### 3. Enable GitHub Pages

1. Go to **Settings → Pages**
2. Under **Source**, select `Deploy from a branch`
3. Branch: `main` · Folder: `/ (root)`
4. Click **Save**

Your live URL will be: `https://<your-username>.github.io/<repo-name>/`

### 4. Enable GitHub Actions

Go to the **Actions** tab and confirm workflows are enabled.
The scanner will run automatically on the schedule below.

---

## Schedule (Weekdays Only)

| Time (EDT / UTC-4) | Focus |
|--------------------|-------|
| 8:00 AM  | Pre-Market — overnight catalysts, gap-ups > 2% |
| 8:45 AM  | Macro Check — reactions to 8:30 AM data |
| 9:45 AM  | True Open — breakouts surviving opening volatility |
| 11:30 AM | Midday Shift — momentum into European close |
| 3:00 PM  | Power Hour — consolidation near HOD |

> **Winter note:** GitHub Actions cron runs in UTC. The workflow is set for EDT (UTC-4).
> During EST (UTC-5), November–March, edit `.github/workflows/scanner.yml` and add
> 1 hour to each UTC time (e.g., `0 12` → `0 13` for the 8:00 AM slot).

---

## Running Manually

```bash
pip install -r requirements.txt
python scanner.py
```

You can also trigger a manual run anytime from the GitHub Actions tab →
**Stock Scanner → Run workflow**.

---

## Data Sources

| Data | Source | Cost |
|------|--------|------|
| Price / Volume / History | yfinance (Yahoo Finance) | Free |
| News Sentiment | Alpha Vantage | Free (25 req/day) |
| Technical Indicators | Calculated from raw bars | — |

---

## Disclaimer

For informational and research purposes only. Not financial advice.
All trading involves substantial risk of loss.
