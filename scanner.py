#!/usr/bin/env python3
"""
Elite Day Trading Research Scanner
===================================
Scans S&P 500 / Nasdaq 100 at 5 scheduled times daily.
Applies a strict 4-rule momentum, liquidity, and catalyst framework.
Outputs index.html for GitHub Pages hosting.

Rules:
  1. Universe & Liquidity  — Market Cap >$50B, ADV >5M, ATR >=2.5%
  2. Volume & Momentum     — Price>VWAP, RVOL>1.5x, above 9/20-EMA, no climax
  3. Catalyst & Context    — Sentiment >75% positive, outperforming SPY/QQQ
  4. Risk/Reward & Levels  — Runway >=2%, R/R >= 1:2

Data:  yfinance (free, ~2-5 min delay)
News:  Alpha Vantage News Sentiment API
"""

import os
import sys
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
import pytz

# ─── CONFIG ───────────────────────────────────────────────────────────────────

ALPHA_VANTAGE_KEY = os.environ.get('ALPHA_VANTAGE_KEY', '10M4I9CYR7SPPVSE')
EST = pytz.timezone('US/Eastern')

# Rule 1 thresholds
MIN_MARKET_CAP = 50_000_000_000   # $50B
MIN_ADV        = 5_000_000        # 5M shares/day
MIN_ATR_PCT    = 2.5              # ATR as % of price

# Rule 2 thresholds
MIN_RVOL       = 1.5              # 1.5x normal volume for time of day

# Rule 4 thresholds
MIN_RUNWAY_PCT = 2.0              # 2% to nearest resistance
MIN_RR_RATIO   = 2.0              # 1:2 risk/reward minimum

# Time slot definitions
TIME_SLOTS = {
    'pre_market':  {
        'label': '8:00 AM EST — Pre-Market Setup',
        'focus': 'Overnight catalysts, earnings, and gap-ups > 2%',
    },
    'macro_check': {
        'label': '8:45 AM EST — Macro Check',
        'focus': 'Reactions to 8:30 AM economic data drops',
    },
    'true_open':   {
        'label': '9:45 AM EST — True Open Trend',
        'focus': 'Stocks holding morning breakouts and surviving opening volatility',
    },
    'midday':      {
        'label': '11:30 AM EST — Midday Shift',
        'focus': 'Sustained momentum into the European close',
    },
    'power_hour':  {
        'label': '3:00 PM EST — Power Hour Prep',
        'focus': 'Stocks consolidating near HOD preparing for end-of-day breakouts',
    },
}

CARD_COLORS = ['#00d4ff', '#00ff88', '#ffcc00', '#ff6b35', '#c77dff',
               '#ff6b6b', '#4ecdc4', '#ffe66d', '#a8e6cf', '#ffd3b6']

# ─── UNIVERSE ─────────────────────────────────────────────────────────────────

FALLBACK_TICKERS = [
    'AAPL','MSFT','NVDA','AMZN','GOOGL','GOOG','META','TSLA','BRK-B',
    'LLY','V','JPM','UNH','XOM','MA','AVGO','HD','PG','MRK','ABBV',
    'COST','NFLX','CRM','AMD','ADBE','NOW','QCOM','TXN','INTU','AMGN',
    'ARM','AMAT','MU','LRCX','KLAC','SNPS','CDNS','PANW','CRWD','DDOG',
    'SHOP','MELI','SE','BABA','JD','PDD','UBER','LYFT','ABNB','DASH',
    'GS','MS','BAC','WFC','C','BLK','SCHW','AXP','COF','USB',
    'SPG','PLD','EQIX','DLR','AMT','CCI','PSA','EXR','VTR','WELL',
]

def get_universe() -> list:
    """Fetch S&P 500 + Nasdaq 100 tickers from Wikipedia, with fallback."""
    tickers = set()

    try:
        sp500 = pd.read_html(
            'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
            storage_options={'User-Agent': 'Mozilla/5.0'}
        )[0]
        syms = sp500['Symbol'].str.replace('.', '-', regex=False).tolist()
        tickers.update(syms)
        print(f"  [OK] Loaded {len(syms)} S&P 500 tickers")
    except Exception as e:
        print(f"  [WARN] S&P 500 Wikipedia fetch failed: {e}")

    try:
        ndx_tables = pd.read_html(
            'https://en.wikipedia.org/wiki/Nasdaq-100',
            storage_options={'User-Agent': 'Mozilla/5.0'}
        )
        # The components table varies by Wikipedia edits — try a few indices
        for tbl in ndx_tables:
            if 'Ticker' in tbl.columns or 'Symbol' in tbl.columns:
                col = 'Ticker' if 'Ticker' in tbl.columns else 'Symbol'
                syms = tbl[col].dropna().tolist()
                if len(syms) > 50:
                    tickers.update(syms)
                    print(f"  [OK] Loaded {len(syms)} Nasdaq 100 tickers")
                    break
    except Exception as e:
        print(f"  [WARN] Nasdaq 100 Wikipedia fetch failed: {e}")

    if len(tickers) < 10:
        print(f"  [FALLBACK] Using hardcoded large-cap universe ({len(FALLBACK_TICKERS)} tickers)")
        tickers = set(FALLBACK_TICKERS)

    # Clean up any bad symbols
    clean = [t for t in sorted(tickers) if isinstance(t, str) and 1 <= len(t) <= 6]
    return clean


# ─── TIME SLOT DETECTION ──────────────────────────────────────────────────────

def get_slot() -> str:
    """Return the name of the most recently triggered time slot."""
    now   = datetime.now(EST)
    total = now.hour * 60 + now.minute

    # (minutes_since_midnight, slot_name)
    boundaries = [
        (8*60,      'pre_market'),
        (8*60+45,   'macro_check'),
        (9*60+45,   'true_open'),
        (11*60+30,  'midday'),
        (15*60,     'power_hour'),
    ]

    current_slot = boundaries[0][1]
    for threshold, name in boundaries:
        if total >= threshold:
            current_slot = name

    return current_slot


# ─── TECHNICAL HELPERS ────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_atr_pct(daily: pd.DataFrame, period: int = 14) -> float:
    """ATR expressed as a % of the last closing price."""
    h, l, c = daily['High'], daily['Low'], daily['Close']
    prev_c = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean().iloc[-1]
    return float(atr / c.iloc[-1] * 100)


def calc_vwap(df: pd.DataFrame) -> float:
    """Intraday VWAP (resets at open) from 5-min bars."""
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    cum_tpv = (tp * df['Volume']).cumsum()
    cum_vol = df['Volume'].cumsum()
    return float(cum_tpv.iloc[-1] / cum_vol.iloc[-1])


def calc_rvol(df: pd.DataFrame, adv: float) -> float:
    """
    Relative volume vs expected volume for this time of day.
    Compares today's accumulated volume to the fraction of ADV
    that would normally have traded by now.
    """
    today_vol = float(df['Volume'].sum())
    last_ts   = df.index[-1]

    # Convert to EST if needed
    if last_ts.tzinfo is not None:
        last_ts_est = last_ts.tz_convert(EST)
    else:
        last_ts_est = last_ts

    # Minutes elapsed since 9:30 open (pre-market bars are excluded from RVOL)
    open_min = 9 * 60 + 30
    now_min  = last_ts_est.hour * 60 + last_ts_est.minute

    if now_min < open_min:
        # Pre-market: compare to full pre-market ADV proxy (15% of ADV)
        elapsed_frac = 0.15
    else:
        elapsed_min  = max(now_min - open_min, 5)
        elapsed_frac = min(elapsed_min / 390.0, 1.0)

    expected = adv * elapsed_frac
    return today_vol / expected if expected > 0 else 0.0


def is_climax_volume(df: pd.DataFrame) -> bool:
    """
    True if the most recent bar is a parabolic (climax) spike.
    Defined as the last bar volume > 3x the mean of all prior bars.
    """
    if len(df) < 5:
        return False
    vols    = df['Volume']
    last    = float(vols.iloc[-1])
    avg_prev = float(vols.iloc[:-1].mean())
    return last > 3.0 * avg_prev


def find_resistance(price: float,
                    prev_day_high: float,
                    week52_high: float,
                    premarket_high: float | None) -> tuple:
    """Return (nearest_resistance_above_price, label) or (None, None)."""
    candidates = {
        'Previous Day High': prev_day_high,
        '52-Week High':      week52_high,
    }
    if premarket_high and premarket_high > price * 1.001:
        candidates['Pre-Market High'] = premarket_high

    above = {k: v for k, v in candidates.items() if v > price * 1.001}
    if not above:
        return None, None

    label = min(above, key=above.get)
    return above[label], label


# ─── NEWS & SENTIMENT ────────────────────────────────────────────────────────

_sentiment_cache: dict = {}


def get_sentiment(ticker: str) -> tuple:
    """
    Return (pct_positive: float|None, headline: str).
    pct_positive is 0-100 mapped from Alpha Vantage's -1…+1 score.
    Results are cached for the duration of the scan run.
    """
    if ticker in _sentiment_cache:
        return _sentiment_cache[ticker]

    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=NEWS_SENTIMENT"
            f"&tickers={ticker}"
            f"&apikey={ALPHA_VANTAGE_KEY}"
            f"&limit=20&sort=LATEST"
        )
        r    = requests.get(url, timeout=8)
        data = r.json()

        if 'feed' not in data or not data['feed']:
            _sentiment_cache[ticker] = (None, '')
            return None, ''

        scores, headline = [], ''
        for art in data['feed'][:15]:
            if not headline and art.get('title'):
                src      = art.get('source', '')
                headline = f"{art['title'][:90]}… ({src})"
            for ts in art.get('ticker_sentiment', []):
                if ts.get('ticker') == ticker:
                    try:
                        scores.append(float(ts['ticker_sentiment_score']))
                    except (ValueError, KeyError):
                        pass

        if not scores:
            _sentiment_cache[ticker] = (None, headline)
            return None, headline

        avg = sum(scores) / len(scores)
        pct = round((avg + 1) / 2 * 100, 1)   # map -1…+1  →  0…100
        _sentiment_cache[ticker] = (pct, headline)
        return pct, headline

    except Exception as e:
        _sentiment_cache[ticker] = (None, '')
        return None, ''


# ─── SINGLE STOCK SCAN ────────────────────────────────────────────────────────

def scan_ticker(ticker: str, slot: str,
                spy_ret: float, qqq_ret: float) -> dict | None:
    """
    Apply all 4 rules to a single ticker.
    Returns a setup dict on pass, None on any failure.
    """
    try:
        tk   = yf.Ticker(ticker)
        info = tk.fast_info          # lightweight call

        # ── Rule 1: Universe & Liquidity ──────────────────────────────────
        mkt_cap = getattr(info, 'market_cap', None) or 0
        if mkt_cap < MIN_MARKET_CAP:
            return None

        adv = getattr(info, 'three_month_average_volume', None) or 0
        if adv < MIN_ADV:
            return None

        # ATR — needs daily bars
        daily = tk.history(period='3mo', interval='1d', auto_adjust=True)
        if len(daily) < 15:
            return None

        atr_pct = calc_atr_pct(daily)
        if atr_pct < MIN_ATR_PCT:
            return None

        # ── 5-minute intraday data ─────────────────────────────────────────
        intra = tk.history(period='1d', interval='5m', auto_adjust=True)
        if len(intra) < 6:
            return None

        current_price = float(intra['Close'].iloc[-1])

        # ── Rule 2: Volume & Momentum ──────────────────────────────────────

        # 2a. Price > VWAP
        vwap = calc_vwap(intra)
        if current_price <= vwap:
            return None

        # 2b. RVOL > 1.5x
        rvol = calc_rvol(intra, float(adv))
        if rvol < MIN_RVOL:
            return None

        # 2c. Price above 9-EMA and 20-EMA on 5-min chart
        closes = intra['Close']
        ema9_series  = ema(closes, 9)
        ema20_series = ema(closes, 20)
        ema9_val     = float(ema9_series.iloc[-1])
        ema20_val    = float(ema20_series.iloc[-1])

        if current_price <= ema9_val or current_price <= ema20_val:
            return None

        # 2d. No climax (parabolic) volume
        if is_climax_volume(intra):
            return None

        # ── Rule 3: Catalyst & Context ─────────────────────────────────────

        # 3a. Relative strength vs SPY/QQQ
        first_price = float(intra['Close'].iloc[0])
        ticker_ret  = (current_price - first_price) / first_price * 100
        if ticker_ret <= spy_ret and ticker_ret <= qqq_ret:
            return None

        # 3b. News sentiment > 75% positive
        sentiment_pct, headline = get_sentiment(ticker)
        if sentiment_pct is not None and sentiment_pct < 75.0:
            return None

        # ── Rule 4: Risk/Reward & Levels ──────────────────────────────────

        # Resistance levels
        prev_day_high = float(daily['High'].iloc[-2]) if len(daily) >= 2 else current_price
        week52_high   = float(daily['High'].max())

        premarket_high = None
        try:
            pm      = tk.history(period='1d', interval='1m', prepost=True, auto_adjust=True)
            pm_est  = pm.copy()
            pm_est.index = pm_est.index.tz_convert(EST)
            pm_only = pm_est[pm_est.index.time < pd.Timestamp('09:30').time()]
            if len(pm_only) > 0:
                premarket_high = float(pm_only['High'].max())
        except Exception:
            pass

        resistance, res_label = find_resistance(
            current_price, prev_day_high, week52_high, premarket_high
        )
        if resistance is None:
            return None

        runway_pct = (resistance - current_price) / current_price * 100
        if runway_pct < MIN_RUNWAY_PCT:
            return None

        risk_pct = (current_price - vwap) / current_price * 100
        if risk_pct <= 0:
            return None

        rr = runway_pct / risk_pct
        if rr < MIN_RR_RATIO:
            return None

        # ── Additional context ─────────────────────────────────────────────
        prev_close = float(daily['Close'].iloc[-2]) if len(daily) >= 2 else current_price
        gap_pct    = (current_price - prev_close) / prev_close * 100

        # Pre-market slot: require gap > 2%
        if slot == 'pre_market' and gap_pct < 2.0:
            return None

        # ── Entry trigger by time slot ─────────────────────────────────────
        pm_level = f"${premarket_high:.2f}" if premarket_high else f"${resistance:.2f}"
        entry_map = {
            'pre_market':  f"Breakout above pre-market high {pm_level}",
            'macro_check': f"Sustained hold above ${current_price * 1.005:.2f} post-data print",
            'true_open':   f"9-EMA hold (${ema9_val:.2f}) + volume expansion on next 5-min bar",
            'midday':      f"Break above ${resistance:.2f} with RVOL maintaining >{MIN_RVOL}x",
            'power_hour':  f"HOD breakout above ${resistance:.2f} in final hour",
        }

        return {
            'ticker':        ticker,
            'price':         current_price,
            'vwap':          vwap,
            'ema9':          ema9_val,
            'ema20':         ema20_val,
            'rvol':          rvol,
            'atr_pct':       atr_pct,
            'gap_pct':       gap_pct,
            'ticker_ret':    ticker_ret,
            'resistance':    resistance,
            'res_label':     res_label,
            'runway_pct':    runway_pct,
            'stop':          vwap,
            'risk_pct':      risk_pct,
            'rr':            rr,
            'sentiment_pct': sentiment_pct,
            'headline':      headline,
            'entry':         entry_map.get(slot, f"Break above ${resistance:.2f}"),
            'mkt_cap_b':     mkt_cap / 1e9,
            'adv_m':         adv / 1e6,
        }

    except Exception as e:
        return None


# ─── HTML GENERATOR ───────────────────────────────────────────────────────────

def _format_card(s: dict, color: str, spy_ret: float) -> str:
    sent    = f"{s['sentiment_pct']:.0f}% Positive" if s['sentiment_pct'] is not None else 'N/A'
    rr_str  = f"1:{s['rr']:.1f}"
    gap_cls = 'gap-up' if s['gap_pct'] >= 0 else 'gap-down'
    gap_sym = '▲' if s['gap_pct'] >= 0 else '▼'
    hl      = s['headline'] or 'Strong institutional momentum — no specific news catalyst identified.'

    return f"""
<div class="card" style="border-left-color:{color};">
  <div class="card-header">
    <span class="ticker" style="color:{color};">{s['ticker']}</span>
    <span class="price">${s['price']:.2f}</span>
    <span class="badge {gap_cls}">{gap_sym} {abs(s['gap_pct']):.2f}%</span>
    <span class="badge neutral">Cap ${s['mkt_cap_b']:.0f}B</span>
  </div>

  <div class="grid-2">
    <div class="metric">
      <div class="lbl">Price / VWAP</div>
      <div class="val">${s['price']:.2f} / ${s['vwap']:.2f}</div>
    </div>
    <div class="metric">
      <div class="lbl">Rel. Volume (RVOL)</div>
      <div class="val hot">{s['rvol']:.2f}x normal</div>
    </div>
    <div class="metric">
      <div class="lbl">9-EMA / 20-EMA (5m)</div>
      <div class="val">${s['ema9']:.2f} / ${s['ema20']:.2f}</div>
    </div>
    <div class="metric">
      <div class="lbl">ATR Volatility</div>
      <div class="val">{s['atr_pct']:.2f}% of price</div>
    </div>
    <div class="metric">
      <div class="lbl">Sentiment Score</div>
      <div class="val">{sent}</div>
    </div>
    <div class="metric">
      <div class="lbl">Strength vs Market</div>
      <div class="val hot">{s['ticker_ret']:+.2f}% vs SPY {spy_ret:+.2f}%</div>
    </div>
  </div>

  <div class="catalyst-box">
    <span class="section-lbl">THE CATALYST</span>
    <p>{hl}</p>
  </div>

  <div class="trade-plan">
    <span class="section-lbl">TRADE PLAN</span>
    <div class="trade-row">
      <span class="tl">Entry Trigger</span>
      <span class="tv">{s['entry']}</span>
    </div>
    <div class="trade-row">
      <span class="tl">Target ({s['res_label']})</span>
      <span class="tv green">${s['resistance']:.2f} &nbsp;(+{s['runway_pct']:.1f}% runway)</span>
    </div>
    <div class="trade-row last">
      <span class="tl">Hard Stop (VWAP)</span>
      <span class="tv red">${s['stop']:.2f} &nbsp;(&minus;{s['risk_pct']:.1f}% risk &rarr; {rr_str} R/R)</span>
    </div>
  </div>
</div>"""


def build_html(setups: list, slot: str, scan_time: str,
               spy_ret: float, qqq_ret: float) -> str:
    slot_info  = TIME_SLOTS[slot]
    setup_count = len(setups)

    cards_html = ''.join(
        _format_card(s, CARD_COLORS[i % len(CARD_COLORS)], spy_ret)
        for i, s in enumerate(setups)
    )

    if not cards_html:
        cards_html = """
<div class="no-setups">
  <div class="no-icon">&#9888;</div>
  <div class="no-msg">No valid setups meet the strict criteria at this time.</div>
  <div class="no-sub">Capital preservation is the priority. Stand aside and wait for the next scan.</div>
</div>"""

    spy_color = '#00ff88' if spy_ret >= 0 else '#ff6b6b'
    qqq_color = '#00ff88' if qqq_ret >= 0 else '#ff6b6b'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>Trading Scanner &mdash; {slot_info['label']}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: #080b10;
      color: #c9d1d9;
      font-family: 'Courier New', 'Consolas', monospace;
      font-size: 14px;
      padding: 24px 16px 48px;
      min-height: 100vh;
    }}

    /* ── Header ─────────────────────────────────────── */
    .header {{
      text-align: center;
      margin-bottom: 36px;
      padding-bottom: 24px;
      border-bottom: 1px solid #1c2030;
    }}
    .header-badge {{
      display: inline-block;
      background: #111827;
      border: 1px solid #2a3042;
      border-radius: 20px;
      padding: 4px 14px;
      font-size: 10px;
      color: #6b7280;
      letter-spacing: 2px;
      text-transform: uppercase;
      margin-bottom: 12px;
    }}
    h1 {{
      font-size: 26px;
      font-weight: bold;
      color: #e6edf3;
      letter-spacing: 1px;
      margin-bottom: 6px;
    }}
    .slot-label {{
      font-size: 14px;
      color: #58a6ff;
      margin-bottom: 4px;
    }}
    .focus-text {{
      font-size: 12px;
      color: #6b7280;
      font-style: italic;
      margin-bottom: 14px;
    }}
    .scan-meta {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 20px;
      font-size: 12px;
      color: #6b7280;
    }}
    .scan-meta .val {{ color: #e6edf3; }}

    /* ── Container ──────────────────────────────────── */
    .container {{ max-width: 860px; margin: 0 auto; }}

    /* ── Cards ──────────────────────────────────────── */
    .card {{
      background: #0d1117;
      border: 1px solid #1c2030;
      border-left: 4px solid #00d4ff;
      border-radius: 8px;
      padding: 22px 20px;
      margin-bottom: 24px;
    }}
    .card-header {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .ticker {{
      font-size: 28px;
      font-weight: bold;
      letter-spacing: 1.5px;
    }}
    .price {{
      font-size: 22px;
      color: #e6edf3;
    }}
    .badge {{
      padding: 3px 10px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: bold;
    }}
    .gap-up    {{ background: #0d2b1a; color: #00ff88; }}
    .gap-down  {{ background: #2b0d0d; color: #ff6b6b; }}
    .neutral   {{ background: #1a1f2e; color: #8b949e; }}

    /* ── Metrics grid ────────────────────────────────── */
    .grid-2 {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-bottom: 16px;
    }}
    .metric {{
      background: #111827;
      border-radius: 6px;
      padding: 10px 12px;
    }}
    .lbl {{
      font-size: 9px;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin-bottom: 5px;
    }}
    .val {{ font-size: 14px; color: #e6edf3; }}
    .val.hot {{ color: #ffcc00; }}

    /* ── Catalyst ────────────────────────────────────── */
    .catalyst-box {{
      background: #111827;
      border-radius: 6px;
      padding: 12px 14px;
      margin-bottom: 14px;
    }}
    .section-lbl {{
      display: block;
      font-size: 9px;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin-bottom: 7px;
    }}
    .catalyst-box p {{
      font-size: 13px;
      color: #c9d1d9;
      line-height: 1.6;
    }}

    /* ── Trade plan ─────────────────────────────────── */
    .trade-plan {{
      background: #080b10;
      border: 1px solid #1c2030;
      border-radius: 6px;
      padding: 14px 16px;
    }}
    .trade-plan .section-lbl {{ margin-bottom: 10px; }}
    .trade-row {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      padding: 8px 0;
      border-bottom: 1px solid #111827;
      gap: 16px;
    }}
    .trade-row.last {{ border-bottom: none; }}
    .tl {{ color: #6b7280; white-space: nowrap; font-size: 12px; }}
    .tv {{ color: #e6edf3; text-align: right; font-size: 12px; }}
    .green {{ color: #00ff88; }}
    .red   {{ color: #ff6b6b; }}

    /* ── No setups ───────────────────────────────────── */
    .no-setups {{
      text-align: center;
      padding: 70px 20px;
      border: 1px dashed #1c2030;
      border-radius: 8px;
    }}
    .no-icon {{ font-size: 44px; margin-bottom: 16px; color: #ffcc00; }}
    .no-msg  {{ font-size: 16px; color: #8b949e; margin-bottom: 8px; }}
    .no-sub  {{ font-size: 13px; color: #4b5563; }}

    /* ── Footer ─────────────────────────────────────── */
    .footer {{
      text-align: center;
      margin-top: 48px;
      padding-top: 20px;
      border-top: 1px solid #1c2030;
      font-size: 11px;
      color: #374151;
      line-height: 1.8;
    }}

    /* ── Mobile ─────────────────────────────────────── */
    @media (max-width: 580px) {{
      .grid-2        {{ grid-template-columns: 1fr; }}
      .trade-row     {{ flex-direction: column; gap: 3px; }}
      .tv            {{ text-align: left; }}
      h1             {{ font-size: 20px; }}
      .ticker        {{ font-size: 22px; }}
    }}
  </style>
</head>
<body>
  <div class="container">

    <div class="header">
      <div class="header-badge">Elite Day Trading Research</div>
      <h1>&#9650; Momentum Scanner</h1>
      <div class="slot-label">{slot_info['label']}</div>
      <div class="focus-text">Focus: {slot_info['focus']}</div>
      <div class="scan-meta">
        <div>Scanned <span class="val">{scan_time}</span></div>
        <div>SPY <span class="val" style="color:{spy_color}">{spy_ret:+.2f}%</span></div>
        <div>QQQ <span class="val" style="color:{qqq_color}">{qqq_ret:+.2f}%</span></div>
        <div>Setups Found <span class="val">{setup_count}</span></div>
      </div>
    </div>

    {cards_html}

    <div class="footer">
      Page auto-refreshes every 5 minutes &nbsp;&bull;&nbsp;
      Powered by yfinance + Alpha Vantage &nbsp;&bull;&nbsp;
      Rules: Cap&gt;$50B &middot; ADV&gt;5M &middot; ATR&ge;2.5% &middot; Price&gt;VWAP
      &middot; RVOL&gt;1.5x &middot; 9/20-EMA &middot; Runway&ge;2% &middot; R/R&ge;1:2<br>
      <strong>For informational &amp; research purposes only. Not financial advice. All trading involves substantial risk of loss.</strong>
    </div>

  </div>
</body>
</html>"""


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    est_now   = datetime.now(EST)
    scan_time = est_now.strftime('%b %d, %Y  %I:%M %p EST')
    slot      = get_slot()
    slot_info = TIME_SLOTS[slot]

    print(f"\n{'=' * 64}")
    print(f"  {slot_info['label']}")
    print(f"  Scan time : {scan_time}")
    print(f"  Focus     : {slot_info['focus']}")
    print(f"{'=' * 64}\n")

    # ── SPY / QQQ baseline returns ─────────────────────────────────────────
    spy_ret, qqq_ret = 0.0, 0.0
    for sym in ['SPY', 'QQQ']:
        try:
            df = yf.Ticker(sym).history(period='1d', interval='5m', auto_adjust=True)
            if len(df) > 1:
                ret = (df['Close'].iloc[-1] - df['Close'].iloc[0]) / df['Close'].iloc[0] * 100
                if sym == 'SPY':
                    spy_ret = float(ret)
                else:
                    qqq_ret = float(ret)
        except Exception as e:
            print(f"  [WARN] Could not fetch {sym}: {e}")

    print(f"  SPY: {spy_ret:+.2f}%   QQQ: {qqq_ret:+.2f}%\n")

    # ── Load universe ──────────────────────────────────────────────────────
    universe = get_universe()
    print(f"  Scanning {len(universe)} tickers...\n")

    # ── Scan each ticker ───────────────────────────────────────────────────
    setups: list = []
    for i, ticker in enumerate(universe):
        result = scan_ticker(ticker, slot, spy_ret, qqq_ret)
        if result:
            setups.append(result)
            print(
                f"  ✓ SETUP: {ticker:<6}  "
                f"Price=${result['price']:.2f}  "
                f"RVOL={result['rvol']:.1f}x  "
                f"Runway={result['runway_pct']:.1f}%  "
                f"R/R=1:{result['rr']:.1f}"
            )

        # Gentle rate limiting — don't hammer yfinance
        if (i + 1) % 25 == 0:
            time.sleep(2)

    # Sort by RVOL (strongest conviction first)
    setups.sort(key=lambda x: x['rvol'], reverse=True)

    print(f"\n{'=' * 64}")
    print(f"  Scan complete — {len(setups)} setup(s) found.")
    print(f"{'=' * 64}\n")

    # ── Write HTML output ──────────────────────────────────────────────────
    html        = build_html(setups, slot, scan_time, spy_ret, qqq_ret)
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"  ✓ index.html written ({len(html):,} bytes) → {output_path}")


if __name__ == '__main__':
    main()
