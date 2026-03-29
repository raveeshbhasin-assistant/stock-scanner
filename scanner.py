#!/usr/bin/env python3
"""
Elite Day Trading Research Scanner
===================================
Scans S&P 500 / Nasdaq 100 at 5 scheduled times daily.
Applies a momentum, liquidity, and catalyst framework.
Outputs index.html for GitHub Pages hosting.

Rules:
  LONG:
    1. Universe & Liquidity  — Market Cap >$20B, ADV >2M, ATR >=1.5%
    2. Volume & Momentum     — Price>VWAP, RVOL>1.2x, above 9/20-EMA, no climax
    3. Context               — Outperforming SPY or QQQ
    4. Risk/Reward & Levels  — Runway >=1%, R/R >= 1:1.5

  SHORT:
    1. Universe & Liquidity  — Market Cap >$20B, ADV >2M, ATR >=1.5%
    2. Volume & Momentum     — Price<VWAP, RVOL>1.2x, below 9/20-EMA, no climax
    3. Context               — Underperforming BOTH SPY and QQQ
    4. Risk/Reward & Levels  — Downside >=1%, R/R >= 1:1.5

Data:  yfinance (free, ~2-5 min delay)
News:  Alpha Vantage News Headlines API
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
MIN_MARKET_CAP = 20_000_000_000   # $20B
MIN_ADV        = 2_000_000        # 2M shares/day
MIN_ATR_PCT    = 1.5              # ATR as % of price

# Rule 2 thresholds
MIN_RVOL       = 1.2              # 1.2x normal volume

# Rule 4 thresholds
MIN_RUNWAY_PCT = 1.0              # 1% to nearest resistance/support
MIN_RR_RATIO   = 1.5              # 1:1.5 risk/reward minimum

# Monitor tickers
MONITOR_TICKERS = ['GOOGL', 'NVDA']

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

LONG_CARD_COLORS = ['#00d4ff', '#00ff88', '#ffcc00', '#ff6b35', '#c77dff']
SHORT_CARD_COLORS = ['#ff4444', '#ff6b6b', '#ff8c42', '#ff3fa4', '#c44dff']
MONITOR_CARD_COLOR = '#8b5cf6'

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

    clean = [t for t in sorted(tickers) if isinstance(t, str) and 1 <= len(t) <= 6]
    return clean


# ─── TIME SLOT DETECTION ──────────────────────────────────────────────────────

def get_slot() -> str:
    """Return the name of the most recently triggered time slot."""
    now   = datetime.now(EST)
    total = now.hour * 60 + now.minute

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

    if last_ts.tzinfo is not None:
        last_ts_est = last_ts.tz_convert(EST)
    else:
        last_ts_est = last_ts

    open_min = 9 * 60 + 30
    now_min  = last_ts_est.hour * 60 + last_ts_est.minute

    if now_min < open_min:
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


def find_support(price: float,
                 prev_day_low: float,
                 week52_low: float,
                 premarket_low: float | None) -> tuple:
    """Return (nearest_support_below_price, label) or (None, None)."""
    candidates = {
        'Previous Day Low': prev_day_low,
        '52-Week Low':      week52_low,
    }
    if premarket_low and premarket_low < price * 0.999:
        candidates['Pre-Market Low'] = premarket_low

    below = {k: v for k, v in candidates.items() if v < price * 0.999}
    if not below:
        return None, None

    label = max(below, key=below.get)
    return below[label], label


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
        pct = round((avg + 1) / 2 * 100, 1)
        _sentiment_cache[ticker] = (pct, headline)
        return pct, headline

    except Exception as e:
        _sentiment_cache[ticker] = (None, '')
        return None, ''


# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_raw(ticker: str) -> dict | None:
    """
    Fetch all data needed for long/short analysis.
    Returns a dict with all fields or None on any failure.
    """
    try:
        tk = yf.Ticker(ticker)
        info = tk.fast_info

        # Fast pre-filters
        mkt_cap = getattr(info, 'market_cap', None) or 0
        adv = getattr(info, 'three_month_average_volume', None) or 0

        # Daily data
        daily = tk.history(period='3mo', interval='1d', auto_adjust=True)
        if len(daily) < 15:
            return None

        # Intraday 5-min data
        intra = tk.history(period='1d', interval='5m', auto_adjust=True)
        session_active = len(intra) >= 6
        if not session_active:
            # Market is closed — fall back to most recent trading session
            print(f"  [INFO] {ticker}: no live intraday data, fetching 5d fallback...")
            intra_5d = tk.history(period='5d', interval='5m', auto_adjust=True)
            if len(intra_5d) < 6:
                print(f"  [WARN] {ticker}: 5d fallback also empty")
                return None
            intra_5d.index = intra_5d.index.tz_convert(EST)
            last_date = intra_5d.index[-1].strftime('%Y-%m-%d')
            intra = intra_5d[intra_5d.index.strftime('%Y-%m-%d') == last_date]
            if len(intra) < 2:
                print(f"  [WARN] {ticker}: filtered last-day slice too small ({len(intra)} bars)")
                return None
            print(f"  [INFO] {ticker}: using {last_date} session data ({len(intra)} bars)")

        # Pre/post market 1-min data (use 2d period to capture weekend AH data)
        prepost = None
        try:
            prepost = tk.history(period='1d', interval='1m', prepost=True, auto_adjust=True)
            if prepost is not None and len(prepost) == 0:
                prepost = tk.history(period='2d', interval='1m', prepost=True, auto_adjust=True)
            if prepost is not None and len(prepost) > 0:
                prepost.index = prepost.index.tz_convert(EST)
        except Exception:
            pass

        # Calculate all metrics
        price = float(intra['Close'].iloc[-1])
        vwap = calc_vwap(intra)
        atr_pct = calc_atr_pct(daily)
        rvol = calc_rvol(intra, float(adv))

        closes = intra['Close']
        ema9_series = ema(closes, 9)
        ema20_series = ema(closes, 20)
        ema9 = float(ema9_series.iloc[-1])
        ema20 = float(ema20_series.iloc[-1])

        climax = is_climax_volume(intra)

        prev_close = float(daily['Close'].iloc[-2]) if len(daily) >= 2 else price
        gap_pct = (price - prev_close) / prev_close * 100

        first_price = float(intra['Close'].iloc[0])
        ticker_ret = (price - first_price) / first_price * 100

        prev_day_high = float(daily['High'].iloc[-2]) if len(daily) >= 2 else price
        prev_day_low = float(daily['Low'].iloc[-2]) if len(daily) >= 2 else price
        week52_high = float(daily['High'].max())
        week52_low = float(daily['Low'].min())

        premarket_high = None
        premarket_low = None
        if prepost is not None and len(prepost) > 0:
            pm_only = prepost[prepost.index.time < pd.Timestamp('09:30').time()]
            if len(pm_only) > 0:
                premarket_high = float(pm_only['High'].max())
                premarket_low = float(pm_only['Low'].min())

        resistance, res_label = find_resistance(price, prev_day_high, week52_high, premarket_high)
        support, sup_label = find_support(price, prev_day_low, week52_low, premarket_low)

        # Determine market session status and after-hours / pre-market price
        ah_price = None
        market_status = 'LIVE' if session_active else 'PREV CLOSE'
        if prepost is not None and len(prepost) > 0:
            last_ts = prepost.index[-1]
            h, m = last_ts.hour, last_ts.minute
            if h >= 16:
                ah_price = float(prepost['Close'].iloc[-1])
                market_status = 'AFTER HRS'
            elif h < 9 or (h == 9 and m < 30):
                ah_price = float(prepost['Close'].iloc[-1])
                market_status = 'PRE-MKT'

        # ── Chart data for inline SVG sparklines ──────────────────────────
        try:
            ci = intra.index
            if hasattr(ci, 'tz_convert'):
                ci = ci.tz_convert(EST)
            chart_times  = [t.strftime('%H:%M') for t in ci]
            chart_closes = [round(float(v), 2) for v in closes.tolist()]
            tp_s         = (intra['High'] + intra['Low'] + intra['Close']) / 3
            vwap_run     = (tp_s * intra['Volume']).cumsum() / intra['Volume'].cumsum()
            chart_vwap   = [round(float(v), 2) for v in vwap_run.tolist()]
            chart_ema9   = [round(float(v), 2) for v in ema9_series.tolist()]
            chart_ema20  = [round(float(v), 2) for v in ema20_series.tolist()]
            chart_volume = [int(v) for v in intra['Volume'].tolist()]
            chart_data   = {
                'times': chart_times, 'closes': chart_closes,
                'vwap': chart_vwap, 'ema9': chart_ema9,
                'ema20': chart_ema20, 'volume': chart_volume,
            }
        except Exception:
            chart_data = {}

        return {
            'ticker': ticker,
            'price': price,
            'vwap': vwap,
            'ema9': ema9,
            'ema20': ema20,
            'rvol': rvol,
            'atr_pct': atr_pct,
            'climax': climax,
            'gap_pct': gap_pct,
            'ticker_ret': ticker_ret,
            'resistance': resistance,
            'res_label': res_label,
            'support': support,
            'sup_label': sup_label,
            'mkt_cap': mkt_cap,
            'mkt_cap_b': mkt_cap / 1e9,
            'adv': adv,
            'adv_m': adv / 1e6,
            'prev_day_high': prev_day_high,
            'prev_day_low': prev_day_low,
            'week52_high': week52_high,
            'week52_low': week52_low,
            'session_active': session_active,
            'ah_price': ah_price,
            'market_status': market_status,
            'chart_data': chart_data,
        }

    except Exception as e:
        print(f"  [ERROR] fetch_raw({ticker}): {type(e).__name__}: {e}")
        return None


# ─── LONG SCANNER ─────────────────────────────────────────────────────────────

def scan_long(ticker: str, slot: str, spy_ret: float, qqq_ret: float) -> dict | None:
    """
    Scan for LONG setups.
    Returns a setup dict on pass, None on any failure.
    """
    try:
        tk = yf.Ticker(ticker)
        info = tk.fast_info

        # Rule 1: Universe & Liquidity (pre-filter)
        mkt_cap = getattr(info, 'market_cap', None) or 0
        if mkt_cap < MIN_MARKET_CAP:
            return None

        adv = getattr(info, 'three_month_average_volume', None) or 0
        if adv < MIN_ADV:
            return None

        # Fetch all data
        data = fetch_raw(ticker)
        if data is None:
            return None

        price = data['price']
        vwap = data['vwap']
        ema9 = data['ema9']
        ema20 = data['ema20']
        rvol = data['rvol']
        atr_pct = data['atr_pct']
        climax = data['climax']
        gap_pct = data['gap_pct']
        ticker_ret = data['ticker_ret']
        resistance = data['resistance']
        res_label = data['res_label']

        # Rule 1 continued
        if atr_pct < MIN_ATR_PCT:
            return None

        # Rule 2: Volume & Momentum
        # 2a. Price > VWAP
        if price <= vwap:
            return None

        # 2b. RVOL > 1.2x
        if rvol < MIN_RVOL:
            return None

        # 2c. Price above 9-EMA and 20-EMA
        if price <= ema9 or price <= ema20:
            return None

        # 2d. No climax volume
        if climax:
            return None

        # Rule 3: Context — Relative Strength
        if ticker_ret <= spy_ret and ticker_ret <= qqq_ret:
            return None

        # Get news
        sentiment_pct, headline = get_sentiment(ticker)

        # Rule 4: Risk/Reward & Levels
        if resistance is None:
            return None

        long_runway = (resistance - price) / price * 100
        if long_runway < MIN_RUNWAY_PCT:
            return None

        risk_pct = (price - vwap) / price * 100
        if risk_pct <= 0:
            return None

        rr = long_runway / risk_pct
        if rr < MIN_RR_RATIO:
            return None

        # Pre-market slot: require gap > 1%
        if slot == 'pre_market' and gap_pct < 1.0:
            return None

        # Entry trigger by time slot
        pm_level = f"${data['prev_day_high']:.2f}" if data.get('prev_day_high') else f"${resistance:.2f}"
        entry_map = {
            'pre_market':  f"Breakout above pre-market high {pm_level}",
            'macro_check': f"Sustained hold above ${price * 1.005:.2f} post-data print",
            'true_open':   f"9-EMA hold (${ema9:.2f}) + volume expansion on next 5-min bar",
            'midday':      f"Break above ${resistance:.2f} with RVOL maintaining >{MIN_RVOL}x",
            'power_hour':  f"HOD breakout above ${resistance:.2f} in final hour",
        }

        return {
            'ticker': ticker,
            'price': price,
            'vwap': vwap,
            'ema9': ema9,
            'ema20': ema20,
            'rvol': rvol,
            'atr_pct': atr_pct,
            'gap_pct': gap_pct,
            'ticker_ret': ticker_ret,
            'resistance': resistance,
            'res_label': res_label,
            'mkt_cap_b': data['mkt_cap_b'],
            'adv_m': data['adv_m'],
            'runway_pct': long_runway,
            'stop': vwap,
            'risk_pct': risk_pct,
            'rr': rr,
            'sentiment_pct': sentiment_pct,
            'headline': headline,
            'entry': entry_map.get(slot, f"Break above ${resistance:.2f}"),
            'setup_type': 'long',
            'chart_data': data.get('chart_data', {}),
        }

    except Exception as e:
        return None


# ─── SHORT SCANNER ────────────────────────────────────────────────────────────

def scan_short(ticker: str, slot: str, spy_ret: float, qqq_ret: float) -> dict | None:
    """
    Scan for SHORT setups.
    Returns a setup dict on pass, None on any failure.
    """
    try:
        tk = yf.Ticker(ticker)
        info = tk.fast_info

        # Rule 1: Universe & Liquidity (pre-filter)
        mkt_cap = getattr(info, 'market_cap', None) or 0
        if mkt_cap < MIN_MARKET_CAP:
            return None

        adv = getattr(info, 'three_month_average_volume', None) or 0
        if adv < MIN_ADV:
            return None

        # Fetch all data
        data = fetch_raw(ticker)
        if data is None:
            return None

        price = data['price']
        vwap = data['vwap']
        ema9 = data['ema9']
        ema20 = data['ema20']
        rvol = data['rvol']
        atr_pct = data['atr_pct']
        climax = data['climax']
        gap_pct = data['gap_pct']
        ticker_ret = data['ticker_ret']
        support = data['support']
        sup_label = data['sup_label']

        # Rule 1 continued
        if atr_pct < MIN_ATR_PCT:
            return None

        # Rule 2: Volume & Momentum (inverted for shorts)
        # 2a. Price < VWAP
        if price >= vwap:
            return None

        # 2b. RVOL > 1.2x
        if rvol < MIN_RVOL:
            return None

        # 2c. Price below 9-EMA and 20-EMA
        if price >= ema9 or price >= ema20:
            return None

        # 2d. No climax volume
        if climax:
            return None

        # Rule 3: Context — Relative Strength (underperform BOTH SPY and QQQ)
        if ticker_ret > spy_ret or ticker_ret > qqq_ret:
            return None

        # Get news
        sentiment_pct, headline = get_sentiment(ticker)

        # Rule 4: Risk/Reward & Levels (inverted for shorts)
        if support is None:
            return None

        short_downside = (price - support) / price * 100
        if short_downside < MIN_RUNWAY_PCT:
            return None

        risk_pct = (vwap - price) / price * 100
        if risk_pct <= 0:
            return None

        rr = short_downside / risk_pct
        if rr < MIN_RR_RATIO:
            return None

        # Pre-market slot: require gap < -1%
        if slot == 'pre_market' and gap_pct > -1.0:
            return None

        # Entry trigger by time slot
        entry_map = {
            'pre_market':  f"Breakdown below pre-market low ${data['prev_day_low']:.2f}",
            'macro_check': f"Breakdown below ${price * 0.995:.2f} post-data print",
            'true_open':   f"9-EMA breakdown (${ema9:.2f}) + volume expansion on next 5-min bar",
            'midday':      f"Break below ${support:.2f} with RVOL maintaining >{MIN_RVOL}x",
            'power_hour':  f"LOD breakdown below ${support:.2f} in final hour",
        }

        return {
            'ticker': ticker,
            'price': price,
            'vwap': vwap,
            'ema9': ema9,
            'ema20': ema20,
            'rvol': rvol,
            'atr_pct': atr_pct,
            'gap_pct': gap_pct,
            'ticker_ret': ticker_ret,
            'support': support,
            'sup_label': sup_label,
            'mkt_cap_b': data['mkt_cap_b'],
            'adv_m': data['adv_m'],
            'downside_pct': short_downside,
            'stop': vwap,
            'risk_pct': risk_pct,
            'rr': rr,
            'sentiment_pct': sentiment_pct,
            'headline': headline,
            'entry': entry_map.get(slot, f"Break below ${support:.2f}"),
            'setup_type': 'short',
            'chart_data': data.get('chart_data', {}),
        }

    except Exception as e:
        return None


# ─── MONITOR FUNCTION ─────────────────────────────────────────────────────────

def fetch_monitor(ticker: str, spy_ret: float, qqq_ret: float) -> dict | None:
    """
    Fetch monitor data for a ticker (GOOGL, NVDA).
    Returns a dict with ticker info and qualification checks.
    """
    try:
        tk = yf.Ticker(ticker)
        info = tk.fast_info

        mkt_cap = getattr(info, 'market_cap', None) or 0
        adv = getattr(info, 'three_month_average_volume', None) or 0

        data = fetch_raw(ticker)
        if data is None:
            return None

        price = data['price']
        vwap = data['vwap']
        ema9 = data['ema9']
        ema20 = data['ema20']
        rvol = data['rvol']
        atr_pct = data['atr_pct']
        ticker_ret = data['ticker_ret']
        gap_pct = data['gap_pct']
        resistance = data['resistance']
        support = data['support']
        mkt_cap_b = data['mkt_cap_b']
        adv_m = data['adv_m']

        sentiment_pct, headline = get_sentiment(ticker)

        # Build long qualification checks
        long_checks = [
            ('Cap', f'${mkt_cap_b:.0f}B', '≥$20B', mkt_cap >= MIN_MARKET_CAP),
            ('ADV', f'{adv_m:.1f}M', '≥2M', adv >= MIN_ADV),
            ('ATR', f'{atr_pct:.1f}%', '≥1.5%', atr_pct >= MIN_ATR_PCT),
            ('P>VWAP', f'${price:.2f}>${vwap:.2f}', '', price > vwap),
            ('RVOL', f'{rvol:.1f}x', '≥1.2x', rvol >= MIN_RVOL),
            ('EMAs↑', f'9E:{ema9:.0f} 20E:{ema20:.0f}', '', price > ema9 and price > ema20),
            ('RS↑', f'{ticker_ret:+.1f}%', '>SPY/QQQ', ticker_ret > spy_ret or ticker_ret > qqq_ret),
        ]

        if resistance:
            long_runway = (resistance - price) / price * 100
            risk_pct = (price - vwap) / price * 100
            rr = long_runway / risk_pct if risk_pct > 0 else 0
            long_checks.extend([
                ('Runway', f'{long_runway:.1f}%', '≥1%', long_runway >= MIN_RUNWAY_PCT),
                ('R/R', f'1:{rr:.1f}', '≥1:1.5', rr >= MIN_RR_RATIO),
            ])

        # Build short qualification checks
        short_checks = [
            ('Cap', f'${mkt_cap_b:.0f}B', '≥$20B', mkt_cap >= MIN_MARKET_CAP),
            ('ADV', f'{adv_m:.1f}M', '≥2M', adv >= MIN_ADV),
            ('ATR', f'{atr_pct:.1f}%', '≥1.5%', atr_pct >= MIN_ATR_PCT),
            ('P<VWAP', f'${price:.2f}<{vwap:.2f}', '', price < vwap),
            ('RVOL', f'{rvol:.1f}x', '≥1.2x', rvol >= MIN_RVOL),
            ('EMAs↓', f'9E:{ema9:.0f} 20E:{ema20:.0f}', '', price < ema9 and price < ema20),
            ('RS↓', f'{ticker_ret:+.1f}%', '<SPY&QQQ', ticker_ret < spy_ret and ticker_ret < qqq_ret),
        ]

        if support:
            short_downside = (price - support) / price * 100
            risk_pct = (vwap - price) / price * 100
            rr = short_downside / risk_pct if risk_pct > 0 else 0
            short_checks.extend([
                ('Downside', f'{short_downside:.1f}%', '≥1%', short_downside >= MIN_RUNWAY_PCT),
                ('R/R', f'1:{rr:.1f}', '≥1:1.5', rr >= MIN_RR_RATIO),
            ])

        return {
            'ticker': ticker,
            'price': price,
            'gap_pct': gap_pct,
            'ticker_ret': ticker_ret,
            'sentiment_pct': sentiment_pct,
            'headline': headline,
            'long_checks': long_checks,
            'short_checks': short_checks,
            'session_active': data.get('session_active', True),
            'ah_price': data.get('ah_price'),
            'market_status': data.get('market_status', 'LIVE'),
            'chart_data': data.get('chart_data', {}),
        }

    except Exception as e:
        print(f"  [ERROR] fetch_monitor({ticker}): {type(e).__name__}: {e}")
        return None


# ─── HTML GENERATOR ───────────────────────────────────────────────────────────

_svg_chart_id = 0


def _render_chart_svg(chart_data: dict, accent_color: str, ticker: str) -> str:
    """
    Render a compact inline SVG sparkline: price + VWAP + EMA9 + EMA20 + volume bars.
    Returns empty string if data is insufficient.
    """
    global _svg_chart_id
    _svg_chart_id += 1
    cid = f"cg_{ticker}_{_svg_chart_id}"

    closes  = chart_data.get('closes', [])
    vwap_s  = chart_data.get('vwap', [])
    ema9_s  = chart_data.get('ema9', [])
    ema20_s = chart_data.get('ema20', [])
    volumes = chart_data.get('volume', [])

    n = len(closes)
    if n < 3:
        return ''

    W, H, VOL_H = 800, 180, 32
    PRICE_H = H - VOL_H - 4

    all_p = [v for series in [closes, vwap_s, ema9_s, ema20_s]
             for v in series if isinstance(v, (int, float)) and v > 0]
    if not all_p:
        return ''
    p_min, p_max = min(all_p), max(all_p)
    p_range = max(p_max - p_min, p_max * 0.005)
    p_min -= p_range * 0.06
    p_max += p_range * 0.06
    p_range = p_max - p_min

    def sy(price):
        return PRICE_H * (1 - (price - p_min) / p_range)

    def sx(i):
        return (i / (n - 1)) * W if n > 1 else W / 2

    def polyline(series, stroke, sw, dash='', op=1.0):
        pts = ' '.join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(series)
                       if isinstance(v, (int, float)) and v > 0)
        if pts.count(' ') < 1:
            return ''
        da = f' stroke-dasharray="{dash}"' if dash else ''
        return (f'<polyline points="{pts}" fill="none" stroke="{stroke}" '
                f'stroke-width="{sw}"{da} opacity="{op}" '
                f'stroke-linejoin="round" stroke-linecap="round"/>')

    # Price area fill
    valid_closes = [(i, v) for i, v in enumerate(closes)
                    if isinstance(v, (int, float)) and v > 0]
    fill_path = ''
    if len(valid_closes) >= 2:
        pts_str = ' '.join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in valid_closes)
        x0, x1 = sx(valid_closes[0][0]), sx(valid_closes[-1][0])
        fill_path = (f'<path d="M {pts_str.split()[0].replace(",", " ")} '
                     f'L {" L ".join(pts_str.split()[1:])} '
                     f'L {x1:.1f},{PRICE_H} L {x0:.1f},{PRICE_H} Z" '
                     f'fill="url(#{cid})" opacity="0.25"/>')

    # Volume bars
    max_vol = max((v for v in volumes if isinstance(v, (int, float))), default=1) or 1
    bw = max(0.8, W / n - 0.6)
    vol_bars = ''.join(
        f'<rect x="{sx(i) - bw/2:.1f}" y="{H - (v/max_vol)*VOL_H:.1f}" '
        f'width="{bw:.1f}" height="{(v/max_vol)*VOL_H:.1f}" '
        f'fill="rgba(255,255,255,0.10)" rx="1"/>'
        for i, v in enumerate(volumes) if isinstance(v, (int, float)) and max_vol > 0
    )

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'preserveAspectRatio="none" style="width:100%;height:100px;display:block;">'
        f'<defs><linearGradient id="{cid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{accent_color}" stop-opacity="0.4"/>'
        f'<stop offset="100%" stop-color="{accent_color}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'{vol_bars}{fill_path}'
        f'{polyline(ema20_s, "rgba(100,116,139,0.65)", 1.2, dash="4,3")}'
        f'{polyline(ema9_s,  "rgba(96,165,250,0.80)", 1.6)}'
        f'{polyline(vwap_s,  "rgba(251,191,36,0.90)", 2.0, dash="5,3")}'
        f'{polyline(closes,  accent_color, 2.6)}'
        f'</svg>'
    )


def _ticker_logo(ticker: str, color: str) -> str:
    ini = ticker[:2]
    return (
        f'<div class="logo-wrap">'
        f'<img src="https://financialmodelingprep.com/image-stock/{ticker}.png" '
        f'class="logo-img" alt="{ticker}" '
        f'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\';">'
        f'<div class="logo-fallback" style="background:{color}1a;color:{color};">{ini}</div>'
        f'</div>'
    )


def _format_long_card(s: dict, color: str, spy_ret: float, qqq_ret: float) -> str:
    sent     = f"{s['sentiment_pct']:.0f}% Positive" if s['sentiment_pct'] is not None else 'N/A'
    sent_col = ('#00e68a' if (s['sentiment_pct'] or 0) >= 60
                else ('#ffb020' if (s['sentiment_pct'] or 0) >= 40 else '#ff5166')
                if s['sentiment_pct'] is not None else 'rgba(232,236,244,0.28)')
    rr_str   = f"1:{s['rr']:.1f}"
    gap_cls  = 'badge-up' if s['gap_pct'] >= 0 else 'badge-dn'
    gap_sym  = '▲' if s['gap_pct'] >= 0 else '▼'
    hl       = s['headline'] or 'Strong institutional momentum — no specific news catalyst identified.'
    chart    = _render_chart_svg(s.get('chart_data', {}), color, s['ticker'])
    logo     = _ticker_logo(s['ticker'], color)
    chart_block = (
        f'<div class="chart-box">{chart}'
        f'<div class="chart-legend">'
        f'<span class="cleg" style="color:{color};">── Price</span>'
        f'<span class="cleg cleg-vwap">⋯ VWAP</span>'
        f'<span class="cleg cleg-ema9">── EMA9</span>'
        f'<span class="cleg cleg-ema20">── EMA20</span>'
        f'</div></div>'
    ) if chart else ''

    return f"""
<div class="card" style="--cc:{color};">
  <div class="card-top">
    {logo}
    <div class="card-id">
      <div class="card-ticker">{s['ticker']}</div>
      <div class="card-dir long-dir">▲ LONG</div>
    </div>
    <div class="card-price-grp">
      <div class="card-price">${s['price']:.2f}</div>
      <span class="gbadge {gap_cls}">{gap_sym} {abs(s['gap_pct']):.2f}%</span>
    </div>
  </div>

  <div class="tp-box">
    <div class="tp-eyebrow">▎ Trade Plan</div>
    <div class="tp-row">
      <span class="tp-icon ti-entry">➜</span>
      <span class="tp-key">ENTER</span>
      <span class="tp-val">{s['entry']}</span>
    </div>
    <div class="tp-row">
      <span class="tp-icon ti-target">◎</span>
      <span class="tp-key">TARGET</span>
      <span class="tp-val tp-g">${s['resistance']:.2f} <span class="tp-muted">(+{s['runway_pct']:.1f}% · {s['res_label']})</span></span>
    </div>
    <div class="tp-row tp-last">
      <span class="tp-icon ti-stop">✕</span>
      <span class="tp-key">STOP</span>
      <span class="tp-val tp-r">${s['stop']:.2f} VWAP <span class="tp-muted">(&minus;{s['risk_pct']:.1f}% risk · {rr_str} R/R)</span></span>
    </div>
  </div>

  <div class="pills">
    <div class="pill"><span class="pl">RVOL</span><span class="pv pv-hi">{s['rvol']:.1f}×</span></div>
    <div class="pill"><span class="pl">RUNWAY</span><span class="pv">{s['runway_pct']:.1f}%</span></div>
    <div class="pill"><span class="pl">R/R</span><span class="pv">{rr_str}</span></div>
    <div class="pill"><span class="pl">RS vs SPY</span><span class="pv tp-g">{s['ticker_ret']:+.1f}%</span></div>
    <div class="pill"><span class="pl">VWAP</span><span class="pv">${s['vwap']:.2f}</span></div>
    <div class="pill"><span class="pl">EMA9</span><span class="pv">${s['ema9']:.2f}</span></div>
    <div class="pill"><span class="pl">EMA20</span><span class="pv">${s['ema20']:.2f}</span></div>
    <div class="pill"><span class="pl">ATR</span><span class="pv">{s['atr_pct']:.1f}%</span></div>
    <div class="pill"><span class="pl">CAP</span><span class="pv">${s['mkt_cap_b']:.0f}B</span></div>
    <div class="pill"><span class="pl">SENTIMENT</span><span class="pv" style="color:{sent_col};">{sent}</span></div>
  </div>

  {chart_block}

  <div class="cat-box">
    <div class="sec-lbl">Catalyst</div>
    <div class="cat-text">{hl}</div>
  </div>
</div>"""


def _format_short_card(s: dict, color: str, spy_ret: float, qqq_ret: float) -> str:
    sent     = f"{s['sentiment_pct']:.0f}% Positive" if s['sentiment_pct'] is not None else 'N/A'
    sent_col = ('#00e68a' if (s['sentiment_pct'] or 0) >= 60
                else ('#ffb020' if (s['sentiment_pct'] or 0) >= 40 else '#ff5166')
                if s['sentiment_pct'] is not None else 'rgba(232,236,244,0.28)')
    rr_str   = f"1:{s['rr']:.1f}"
    gap_cls  = 'badge-up' if s['gap_pct'] >= 0 else 'badge-dn'
    gap_sym  = '▲' if s['gap_pct'] >= 0 else '▼'
    hl       = s['headline'] or 'Institutional selling pressure — no specific news catalyst identified.'
    chart    = _render_chart_svg(s.get('chart_data', {}), color, s['ticker'])
    logo     = _ticker_logo(s['ticker'], color)
    chart_block = (
        f'<div class="chart-box">{chart}'
        f'<div class="chart-legend">'
        f'<span class="cleg" style="color:{color};">── Price</span>'
        f'<span class="cleg cleg-vwap">⋯ VWAP</span>'
        f'<span class="cleg cleg-ema9">── EMA9</span>'
        f'<span class="cleg cleg-ema20">── EMA20</span>'
        f'</div></div>'
    ) if chart else ''

    return f"""
<div class="card" style="--cc:{color};">
  <div class="card-top">
    {logo}
    <div class="card-id">
      <div class="card-ticker">{s['ticker']}</div>
      <div class="card-dir short-dir">▼ SHORT</div>
    </div>
    <div class="card-price-grp">
      <div class="card-price">${s['price']:.2f}</div>
      <span class="gbadge {gap_cls}">{gap_sym} {abs(s['gap_pct']):.2f}%</span>
    </div>
  </div>

  <div class="tp-box">
    <div class="tp-eyebrow">▎ Trade Plan</div>
    <div class="tp-row">
      <span class="tp-icon ti-entry">➜</span>
      <span class="tp-key">ENTER</span>
      <span class="tp-val">{s['entry']}</span>
    </div>
    <div class="tp-row">
      <span class="tp-icon ti-target">◎</span>
      <span class="tp-key">TARGET</span>
      <span class="tp-val tp-g">${s['support']:.2f} <span class="tp-muted">(&minus;{s['downside_pct']:.1f}% · {s['sup_label']})</span></span>
    </div>
    <div class="tp-row tp-last">
      <span class="tp-icon ti-stop">✕</span>
      <span class="tp-key">STOP</span>
      <span class="tp-val tp-r">${s['stop']:.2f} VWAP <span class="tp-muted">(+{s['risk_pct']:.1f}% risk · {rr_str} R/R)</span></span>
    </div>
  </div>

  <div class="pills">
    <div class="pill"><span class="pl">RVOL</span><span class="pv pv-hi">{s['rvol']:.1f}×</span></div>
    <div class="pill"><span class="pl">DOWNSIDE</span><span class="pv">{s['downside_pct']:.1f}%</span></div>
    <div class="pill"><span class="pl">R/R</span><span class="pv">{rr_str}</span></div>
    <div class="pill"><span class="pl">RS vs SPY</span><span class="pv tp-r">{s['ticker_ret']:+.1f}%</span></div>
    <div class="pill"><span class="pl">VWAP</span><span class="pv">${s['vwap']:.2f}</span></div>
    <div class="pill"><span class="pl">EMA9</span><span class="pv">${s['ema9']:.2f}</span></div>
    <div class="pill"><span class="pl">EMA20</span><span class="pv">${s['ema20']:.2f}</span></div>
    <div class="pill"><span class="pl">ATR</span><span class="pv">{s['atr_pct']:.1f}%</span></div>
    <div class="pill"><span class="pl">CAP</span><span class="pv">${s['mkt_cap_b']:.0f}B</span></div>
    <div class="pill"><span class="pl">SENTIMENT</span><span class="pv" style="color:{sent_col};">{sent}</span></div>
  </div>

  {chart_block}

  <div class="cat-box">
    <div class="sec-lbl">Catalyst</div>
    <div class="cat-text">{hl}</div>
  </div>
</div>"""


def _format_monitor_card(m: dict, spy_ret: float, qqq_ret: float) -> str:
    color          = MONITOR_CARD_COLOR
    gap_cls        = 'badge-up' if m['gap_pct'] >= 0 else 'badge-dn'
    gap_sym        = '▲' if m['gap_pct'] >= 0 else '▼'
    hl             = m['headline'] or 'Monitoring price action and volatility.'
    sent           = f"{m['sentiment_pct']:.0f}% Positive" if m['sentiment_pct'] is not None else 'N/A'
    market_status  = m.get('market_status', 'LIVE')
    ah_price       = m.get('ah_price')
    session_active = m.get('session_active', True)

    status_map = {
        'LIVE':       ('#00e68a', 'rgba(0,230,138,0.15)'),
        'AFTER HRS':  ('#ffb020', 'rgba(255,176,32,0.15)'),
        'PRE-MKT':    ('#4d9fff', 'rgba(77,159,255,0.15)'),
        'PREV CLOSE': ('rgba(232,236,244,0.35)', 'rgba(255,255,255,0.06)'),
    }
    st_fg, st_bg = status_map.get(market_status, ('rgba(232,236,244,0.35)', 'rgba(255,255,255,0.06)'))
    status_badge = (
        f'<span class="status-badge" style="background:{st_bg};color:{st_fg};border:1px solid {st_fg};">'
        f'{market_status}</span>'
    )

    if ah_price is not None:
        display_price = ah_price
        price_label   = 'Extended Hrs Price'
        session_label = 'Extended Hrs'
    elif not session_active:
        display_price = m['price']
        price_label   = 'Last Session Close'
        session_label = 'Last Session'
    else:
        display_price = m['price']
        price_label   = 'Current Price'
        session_label = 'Today'

    ah_pill = ''
    if ah_price is not None:
        ah_chg  = (ah_price - m['price']) / m['price'] * 100
        ah_col  = '#00e68a' if ah_chg >= 0 else '#ff5166'
        ah_sign = '+' if ah_chg >= 0 else ''
        ah_pill = (f'<div class="pill"><span class="pl">PREV CLOSE</span>'
                   f'<span class="pv">${m["price"]:.2f}</span></div>'
                   f'<div class="pill"><span class="pl">EXT HRS CHG</span>'
                   f'<span class="pv" style="color:{ah_col};">{ah_sign}{ah_chg:.2f}%</span></div>')

    long_badges = ''.join(
        f'<span class="cbadge {"cbp" if passes else "cbf"}">{"✓" if passes else "✗"} {label} {actual}</span>'
        for label, actual, threshold, passes in m['long_checks']
    )
    short_badges = ''.join(
        f'<span class="cbadge {"cbp" if passes else "cbf"}">{"✓" if passes else "✗"} {label} {actual}</span>'
        for label, actual, threshold, passes in m['short_checks']
    )

    chart    = _render_chart_svg(m.get('chart_data', {}), color, m['ticker'])
    logo     = _ticker_logo(m['ticker'], color)
    chart_block = (
        f'<div class="chart-box">{chart}'
        f'<div class="chart-legend">'
        f'<span class="cleg" style="color:{color};">── Price</span>'
        f'<span class="cleg cleg-vwap">⋯ VWAP</span>'
        f'<span class="cleg cleg-ema9">── EMA9</span>'
        f'<span class="cleg cleg-ema20">── EMA20</span>'
        f'</div></div>'
    ) if chart else ''

    return f"""
<div class="card" style="--cc:{color};">
  <div class="card-top">
    {logo}
    <div class="card-id">
      <div class="card-ticker">{m['ticker']}</div>
      <div class="card-dir monitor-dir">◉ MONITOR</div>
    </div>
    <div class="card-price-grp">
      <div class="card-price">${display_price:.2f}</div>
      <div style="display:flex;gap:6px;align-items:center;margin-top:4px;flex-wrap:wrap;">
        <span class="gbadge {gap_cls}">{gap_sym} {abs(m['gap_pct']):.2f}%</span>
        {status_badge}
      </div>
    </div>
  </div>

  <div class="pills">
    <div class="pill"><span class="pl">{price_label.upper()}</span><span class="pv">${display_price:.2f}</span></div>
    <div class="pill"><span class="pl">{session_label.upper()} RETURN</span><span class="pv {'tp-g' if m['ticker_ret'] >= 0 else 'tp-r'}">{m['ticker_ret']:+.2f}%</span></div>
    {ah_pill}
    <div class="pill"><span class="pl">SPY</span><span class="pv {'tp-g' if spy_ret >= 0 else 'tp-r'}">{spy_ret:+.2f}%</span></div>
    <div class="pill"><span class="pl">QQQ</span><span class="pv {'tp-g' if qqq_ret >= 0 else 'tp-r'}">{qqq_ret:+.2f}%</span></div>
    <div class="pill"><span class="pl">SENTIMENT</span><span class="pv">{sent}</span></div>
  </div>

  {chart_block}

  <div class="cat-box">
    <div class="sec-lbl">Headline</div>
    <div class="cat-text">{hl}</div>
  </div>

  <div class="checks-section">
    <div class="sec-lbl">Long Qualification ({session_label} data)</div>
    <div class="checks-row">{long_badges}</div>
  </div>

  <div class="checks-section">
    <div class="sec-lbl">Short Qualification ({session_label} data)</div>
    <div class="checks-row">{short_badges}</div>
  </div>
</div>"""


def build_html(longs: list, shorts: list, monitors: list, slot: str, scan_time: str,
               spy_ret: float, qqq_ret: float) -> str:
    slot_info   = TIME_SLOTS[slot]
    long_count  = len(longs)
    short_count = len(shorts)

    long_cards = ''.join(
        _format_long_card(s, LONG_CARD_COLORS[i % len(LONG_CARD_COLORS)], spy_ret, qqq_ret)
        for i, s in enumerate(longs)
    )
    if not long_cards:
        long_cards = """
<div class="no-setups">
  <div class="no-icon">&#9650;</div>
  <div class="no-msg">No long setups pass the filter right now.</div>
  <div class="no-sub">Capital preservation is a valid position.</div>
</div>"""

    short_cards = ''.join(
        _format_short_card(s, SHORT_CARD_COLORS[i % len(SHORT_CARD_COLORS)], spy_ret, qqq_ret)
        for i, s in enumerate(shorts)
    )
    if not short_cards:
        short_cards = """
<div class="no-setups">
  <div class="no-icon">&#9660;</div>
  <div class="no-msg">No short setups pass the filter right now.</div>
  <div class="no-sub">Capital preservation is a valid position.</div>
</div>"""

    monitor_cards = ''.join(
        _format_monitor_card(m, spy_ret, qqq_ret)
        for m in monitors if m is not None
    )

    spy_col = '#00e68a' if spy_ret >= 0 else '#ff5166'
    qqq_col = '#00e68a' if qqq_ret >= 0 else '#ff5166'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>Momentum Scanner &mdash; {slot_info['label']}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    /* ── Reset ──────────────────────────────── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:      #080b12;
      --s1:      #0f1420;
      --s2:      #161d2e;
      --s3:      #1e2740;
      --border:  rgba(255,255,255,0.06);
      --border-accent: rgba(255,255,255,0.10);
      --t1:      #e8ecf4;
      --t2:      rgba(232,236,244,0.55);
      --t3:      rgba(232,236,244,0.28);
      --green:   #00e68a;
      --red:     #ff5166;
      --blue:    #4d9fff;
      --amber:   #ffb020;
      --purple:  #9d7aff;
      --r:       12px;
      --r-sm:    8px;
    }}
    body {{
      background: var(--bg);
      color: var(--t1);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 14px;
      line-height: 1.5;
      padding: 0 16px 100px;
      -webkit-font-smoothing: antialiased;
    }}

    /* ── Layout ─────────────────────────────── */
    .wrap {{ max-width: 1000px; margin: 0 auto; }}

    /* ── Page Header ────────────────────────── */
    .pg-header {{
      text-align: center;
      padding: 60px 0 44px;
      border-bottom: 1px solid var(--border-accent);
      margin-bottom: 56px;
    }}
    .pg-eyebrow {{
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 3px;
      text-transform: uppercase;
      color: var(--t3);
      margin-bottom: 16px;
    }}
    .pg-title {{
      font-size: 42px;
      font-weight: 700;
      letter-spacing: -1.2px;
      color: var(--t1);
      margin-bottom: 14px;
      background: linear-gradient(135deg, var(--t1) 0%, rgba(232,236,244,0.8) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }}
    .pg-slot {{
      font-size: 16px;
      font-weight: 600;
      color: var(--blue);
      margin-bottom: 6px;
    }}
    .pg-focus {{
      font-size: 13px;
      color: var(--t2);
      font-style: italic;
      margin-bottom: 32px;
    }}
    .pg-meta {{
      display: flex;
      justify-content: center;
      flex-wrap: wrap;
      gap: 32px;
    }}
    .meta-item {{ font-size: 12px; color: var(--t2); }}
    .meta-val  {{ font-weight: 700; color: var(--t1); }}

    /* ── Section Heads ──────────────────────── */
    .sec-head {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 64px;
      margin-bottom: 28px;
      padding-bottom: 16px;
      border-bottom: 2px solid var(--border);
    }}
    .sec-head-title {{
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.4px;
    }}
    .sec-head-count {{
      background: var(--s3);
      border: 1px solid var(--border-accent);
      border-radius: 20px;
      padding: 4px 14px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.5px;
      color: var(--t2);
    }}
    .sec-head-line {{ flex: 1; height: 1px; background: var(--border); }}
    .sec-head.long    .sec-head-title {{ color: var(--green);  }}
    .sec-head.short   .sec-head-title {{ color: var(--red);    }}
    .sec-head.monitor .sec-head-title {{ color: var(--purple); }}

    /* ── Card Grid ──────────────────────────── */
    .card-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(480px, 1fr));
      gap: 24px;
      margin-bottom: 12px;
    }}

    /* ── Cards ──────────────────────────────── */
    .card {{
      background: var(--s1);
      border: 1px solid var(--border);
      border-left: 4px solid var(--cc, var(--blue));
      border-radius: var(--r);
      padding: 24px;
      transition: all 0.2s ease;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
    }}
    .card:hover {{
      border-color: var(--border-accent);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    }}

    /* ── Card top row ───────────────────────── */
    .card-top {{
      display: flex;
      align-items: center;
      gap: 14px;
      margin-bottom: 20px;
    }}
    .logo-wrap {{ width: 48px; height: 48px; flex-shrink: 0; position: relative; }}
    .logo-img  {{
      width: 48px; height: 48px;
      border-radius: 10px;
      object-fit: contain;
      display: block;
      background: rgba(255,255,255,0.03);
    }}
    .logo-fallback {{
      width: 48px; height: 48px;
      border-radius: 10px;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.5px;
      display: flex;
    }}
    .card-id {{ flex: 1; min-width: 0; }}
    .card-ticker {{
      font-size: 28px;
      font-weight: 700;
      color: var(--cc, var(--t1));
      letter-spacing: -0.6px;
      line-height: 1;
    }}
    .card-dir {{
      font-size: 9px;
      font-weight: 800;
      letter-spacing: 2px;
      text-transform: uppercase;
      margin-top: 4px;
    }}
    .long-dir    {{ color: var(--green);  }}
    .short-dir   {{ color: var(--red);    }}
    .monitor-dir {{ color: var(--purple); }}
    .card-price-grp {{ text-align: right; flex-shrink: 0; }}
    .card-price {{
      font-size: 28px;
      font-weight: 700;
      color: var(--t1);
      letter-spacing: -0.6px;
      line-height: 1;
    }}
    .gbadge {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 10px;
      font-weight: 700;
      margin-top: 6px;
    }}
    .badge-up {{ background: rgba(0,230,138,0.15); color: var(--green); }}
    .badge-dn {{ background: rgba(255,81,102,0.15);  color: var(--red);  }}

    /* ── Trade Plan Box ─────────────────────── */
    .tp-box {{
      background: linear-gradient(135deg, rgba(0,230,138,0.08) 0%, rgba(77,159,255,0.04) 100%);
      border: 1px solid var(--border);
      border-left: 3px solid var(--cc, var(--blue));
      border-radius: var(--r-sm);
      padding: 18px 20px;
      margin-bottom: 18px;
    }}
    .tp-eyebrow {{
      font-size: 9px;
      font-weight: 800;
      letter-spacing: 2.5px;
      text-transform: uppercase;
      color: var(--t2);
      margin-bottom: 14px;
    }}
    .tp-row {{
      display: flex;
      align-items: baseline;
      gap: 10px;
      padding: 10px 0;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
    }}
    .tp-row.tp-last {{ border-bottom: none; padding-bottom: 0; }}
    .tp-icon  {{ width: 16px; text-align: center; flex-shrink: 0; font-size: 14px; }}
    .ti-entry  {{ color: var(--blue);  }}
    .ti-target {{ color: var(--green); }}
    .ti-stop   {{ color: var(--red);   }}
    .tp-key {{
      font-size: 9px;
      font-weight: 800;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      color: var(--t3);
      width: 52px;
      flex-shrink: 0;
    }}
    .tp-val   {{ flex: 1; color: var(--t1); font-weight: 600; font-size: 13px; }}
    .tp-g     {{ color: var(--green); }}
    .tp-r     {{ color: var(--red);   }}
    .tp-muted {{ font-size: 11px; font-weight: 400; color: var(--t3); }}

    /* ── Metric Pills ───────────────────────── */
    .pills {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 8px;
      margin-bottom: 18px;
    }}
    .pill {{
      background: var(--s2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .pl {{
      font-size: 7px;
      font-weight: 800;
      letter-spacing: 1.2px;
      text-transform: uppercase;
      color: var(--t3);
    }}
    .pv {{ font-size: 13px; font-weight: 700; color: var(--t1); }}
    .pv-hi {{ color: var(--amber); }}

    /* ── Chart ──────────────────────────────── */
    .chart-box {{
      background: var(--s2);
      border: 1px solid var(--border);
      border-radius: var(--r-sm);
      padding: 14px;
      margin-bottom: 18px;
      overflow: hidden;
    }}
    .chart-legend {{
      display: flex;
      gap: 18px;
      margin-top: 8px;
      padding-left: 2px;
      flex-wrap: wrap;
    }}
    .cleg       {{ font-size: 10px; font-weight: 500; color: var(--t2); }}
    .cleg-vwap  {{ color: rgba(255,176,32,0.9); }}
    .cleg-ema9  {{ color: rgba(77,159,255,0.85); }}
    .cleg-ema20 {{ color: rgba(100,116,139,0.6); }}

    /* ── Catalyst ───────────────────────────── */
    .cat-box {{
      background: var(--s2);
      border: 1px solid var(--border);
      border-radius: var(--r-sm);
      padding: 14px 16px;
    }}
    .sec-lbl {{
      font-size: 8px;
      font-weight: 800;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: var(--t3);
      margin-bottom: 8px;
    }}
    .cat-text {{
      font-size: 13px;
      color: var(--t2);
      line-height: 1.6;
    }}

    /* ── Status Badge ───────────────────────── */
    .status-badge {{
      border-radius: 6px;
      padding: 4px 11px;
      font-size: 9px;
      font-weight: 800;
      letter-spacing: 1px;
      text-transform: uppercase;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.6; }}
    }}

    /* ── Checks (Monitor) ───────────────────── */
    .checks-section {{
      background: var(--s2);
      border: 1px solid var(--border);
      border-radius: var(--r-sm);
      padding: 14px 16px;
      margin-top: 12px;
    }}
    .checks-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }}
    .cbadge {{
      padding: 5px 11px;
      border-radius: 6px;
      font-size: 9px;
      font-weight: 700;
      letter-spacing: 0.2px;
    }}
    .cbp {{ background: rgba(0,230,138,0.15); color: var(--green); border: 1px solid rgba(0,230,138,0.25); }}
    .cbf {{ background: rgba(255,81,102,0.12);  color: var(--red);   border: 1px solid rgba(255,81,102,0.20);  }}

    /* ── No Setups ──────────────────────────── */
    .no-setups {{
      text-align: center;
      padding: 64px 24px;
      border: 1px dashed var(--border);
      border-radius: var(--r);
    }}
    .no-icon {{ font-size: 36px; color: var(--amber); opacity: 0.4; margin-bottom: 14px; }}
    .no-msg  {{ font-size: 16px; font-weight: 700; color: var(--t2); margin-bottom: 6px; }}
    .no-sub  {{ font-size: 13px; color: var(--t3); }}

    /* ── How We Filter Section ──────────────── */
    .rules-wrap {{
      margin-top: 88px;
      padding-top: 60px;
      border-top: 1px solid var(--border-accent);
    }}
    .rules-title {{
      font-size: 28px;
      font-weight: 700;
      color: var(--t1);
      letter-spacing: -0.6px;
      margin-bottom: 10px;
    }}
    .rules-sub {{
      font-size: 14px;
      color: var(--t2);
      margin-bottom: 42px;
      max-width: 600px;
      line-height: 1.6;
    }}
    .rules-cols {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 22px;
      margin-bottom: 28px;
    }}
    .rule-card {{
      background: var(--s1);
      border: 1px solid var(--border);
      border-radius: var(--r-sm);
      padding: 24px;
    }}
    .rule-card-title {{
      font-size: 10px;
      font-weight: 800;
      letter-spacing: 2px;
      text-transform: uppercase;
      margin-bottom: 20px;
    }}
    .rule-card.long-rc  .rule-card-title {{ color: var(--green);  }}
    .rule-card.short-rc .rule-card-title {{ color: var(--red);    }}
    .rule-card.both-rc  .rule-card-title {{ color: var(--blue);   }}
    .rule-item {{ margin-bottom: 16px; }}
    .rule-item:last-child {{ margin-bottom: 0; }}
    .rule-plain {{
      font-size: 14px;
      font-weight: 500;
      color: var(--t1);
      margin-bottom: 4px;
      line-height: 1.5;
    }}
    .rule-tech {{
      font-size: 11px;
      color: var(--t3);
      font-family: 'SF Mono', 'Consolas', 'Menlo', monospace;
      line-height: 1.5;
    }}

    /* ── Footer ─────────────────────────────── */
    .footer {{
      text-align: center;
      margin-top: 56px;
      padding-top: 32px;
      border-top: 1px solid var(--border-accent);
      font-size: 11px;
      color: var(--t3);
      line-height: 2;
    }}

    /* ── Mobile ─────────────────────────────── */
    @media (max-width: 768px) {{
      body {{ padding: 0 12px 80px; }}
      .wrap {{ max-width: 100%; }}
      .card {{ padding: 20px; }}
      .pg-title {{ font-size: 32px; }}
      .card-ticker {{ font-size: 24px; }}
      .card-price  {{ font-size: 24px; }}
      .rules-cols  {{ grid-template-columns: 1fr; }}
      .card-grid {{ grid-template-columns: 1fr; }}
      .pills {{ grid-template-columns: repeat(2, 1fr); }}
      .sec-head {{ margin-top: 48px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">

    <!-- ── Page Header ─────────────────────────────── -->
    <div class="pg-header">
      <div class="pg-eyebrow">Elite Day Trading Research</div>
      <h1 class="pg-title">Momentum Scanner</h1>
      <div class="pg-slot">{slot_info['label']}</div>
      <div class="pg-focus">Focus: {slot_info['focus']}</div>
      <div class="pg-meta">
        <div class="meta-item">Scanned &nbsp;<span class="meta-val">{scan_time}</span></div>
        <div class="meta-item">SPY &nbsp;<span class="meta-val" style="color:{spy_col};">{spy_ret:+.2f}%</span></div>
        <div class="meta-item">QQQ &nbsp;<span class="meta-val" style="color:{qqq_col};">{qqq_ret:+.2f}%</span></div>
        <div class="meta-item">Longs &nbsp;<span class="meta-val">{long_count}</span></div>
        <div class="meta-item">Shorts &nbsp;<span class="meta-val">{short_count}</span></div>
      </div>
    </div>

    <!-- ── Market Monitor (top) ─────────────────────────── -->
    <div class="sec-head monitor">
      <div class="sec-head-title">◉ Market Monitor</div>
      <div class="sec-head-count">GOOGL &amp; NVDA</div>
      <div class="sec-head-line"></div>
    </div>
    <div class="card-grid">
      {monitor_cards}
    </div>

    <!-- ── Long Setups ─────────────────────────────── -->
    <div class="sec-head long">
      <div class="sec-head-title">▲ Long Setups</div>
      <div class="sec-head-count">{long_count} found</div>
      <div class="sec-head-line"></div>
    </div>
    <div class="card-grid">
      {long_cards}
    </div>

    <!-- ── Short Setups ────────────────────────────── -->
    <div class="sec-head short">
      <div class="sec-head-title">▼ Short Setups</div>
      <div class="sec-head-count">{short_count} found</div>
      <div class="sec-head-line"></div>
    </div>
    <div class="card-grid">
      {short_cards}
    </div>

    <!-- ── How We Filter ───────────────────────────── -->
    <div class="rules-wrap">
      <div class="rules-title">How the Scanner Filters</div>
      <div class="rules-sub">Every setup shown here has passed all four checks below. Here's what each check means in plain language — and the exact technical parameters behind it.</div>

      <div class="rules-cols">
        <!-- Shared -->
        <div class="rule-card both-rc" style="grid-column:1/-1;">
          <div class="rule-card-title">Step 1 — Large, Liquid, and Movable (applies to both Long &amp; Short)</div>
          <div class="rule-item">
            <div class="rule-plain">Only well-known, large companies — household names with deep trading markets.</div>
            <div class="rule-tech">Market Cap &gt; $20,000,000,000</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Trades at least 2 million shares on an average day — liquid enough to get in and out cleanly.</div>
            <div class="rule-tech">Average Daily Volume (3-month) &gt; 2,000,000 shares</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Moves enough each day to make trading it worthwhile — we skip stocks that barely budge.</div>
            <div class="rule-tech">ATR(14) as % of price &ge; 1.5%</div>
          </div>
        </div>

        <!-- Long -->
        <div class="rule-card long-rc">
          <div class="rule-card-title">▲ Long — Steps 2, 3 &amp; 4</div>
          <div class="rule-item">
            <div class="rule-plain">The stock is trading above its volume-weighted average price for today — buyers are in control.</div>
            <div class="rule-tech">Price &gt; VWAP (intraday, 5-min)</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Unusually high trading activity right now — more than 1.2&times; its expected volume for this time of day.</div>
            <div class="rule-tech">RVOL &gt; 1.2&times; (today's volume vs expected by time-of-day)</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Price is rising and holding above its short-term trend lines — momentum is intact.</div>
            <div class="rule-tech">Price &gt; EMA(9) and Price &gt; EMA(20) on 5-min bars · No climax-volume spike</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Outpacing the broader market — it's leading, not just riding the tide.</div>
            <div class="rule-tech">Ticker intraday return &gt; SPY return OR &gt; QQQ return</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">There's at least 1% of clear air between the current price and the nearest overhead resistance — room to run.</div>
            <div class="rule-tech">Runway to resistance &ge; 1.0% · Risk/Reward &ge; 1:1.5 (stop = VWAP)</div>
          </div>
        </div>

        <!-- Short -->
        <div class="rule-card short-rc">
          <div class="rule-card-title">▼ Short — Steps 2, 3 &amp; 4</div>
          <div class="rule-item">
            <div class="rule-plain">The stock is trading below its volume-weighted average price — sellers are in control.</div>
            <div class="rule-tech">Price &lt; VWAP (intraday, 5-min)</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Unusually high activity confirms the move is real — not just a quiet drift lower.</div>
            <div class="rule-tech">RVOL &gt; 1.2&times; (today's volume vs expected by time-of-day)</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Price is breaking down and sitting below its short-term trend lines — downward momentum is confirmed.</div>
            <div class="rule-tech">Price &lt; EMA(9) and Price &lt; EMA(20) on 5-min bars · No climax-volume spike</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">Lagging both major indices — it's weaker than the market, not just a sector rotation.</div>
            <div class="rule-tech">Ticker return &lt; SPY return AND &lt; QQQ return</div>
          </div>
          <div class="rule-item">
            <div class="rule-plain">At least 1% of clear air between price and the next support level below — room to fall.</div>
            <div class="rule-tech">Downside to support &ge; 1.0% · Risk/Reward &ge; 1:1.5 (stop = VWAP)</div>
          </div>
        </div>
      </div>

      <div class="rule-card both-rc">
        <div class="rule-card-title">Trade Plan Logic</div>
        <div class="rule-item">
          <div class="rule-plain">Entry: wait for the 9-EMA to hold (or break for shorts) with a volume expansion bar confirming the move.</div>
          <div class="rule-tech">Entry trigger = 9-EMA hold/break + RVOL surge on next 5-min close</div>
        </div>
        <div class="rule-item">
          <div class="rule-plain">Target: the nearest technically significant price level above (longs) or below (shorts).</div>
          <div class="rule-tech">Target = nearest resistance (long) or support (short): Prev Day High/Low, 52-Wk High/Low, Pre-Mkt High/Low</div>
        </div>
        <div class="rule-item">
          <div class="rule-plain">Stop: if price crosses back through the VWAP, the trade thesis is invalidated — exit immediately.</div>
          <div class="rule-tech">Hard stop = VWAP cross (long: below VWAP · short: above VWAP)</div>
        </div>
      </div>
    </div>

    <!-- ── Footer ──────────────────────────────────── -->
    <div class="footer">
      Page auto-refreshes every 5 minutes &nbsp;&middot;&nbsp;
      Data: yfinance (free, ~2–5 min delay) + Alpha Vantage news<br>
      <strong>For informational and research purposes only. Not financial advice. All trading involves substantial risk of loss.</strong>
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

    # ── Fetch monitor data for GOOGL and NVDA (before main scan to avoid rate limits) ──
    monitors = []
    for ticker in MONITOR_TICKERS:
        print(f"  Fetching monitor data for {ticker}...")
        monitor_data = fetch_monitor(ticker, spy_ret, qqq_ret)
        if monitor_data:
            monitors.append(monitor_data)
    time.sleep(2)  # brief pause before bulk scan

    # ── Load universe ──────────────────────────────────────────────────────
    universe = get_universe()
    print(f"  Scanning {len(universe)} tickers...\n")

    # ── Scan each ticker for LONG and SHORT ────────────────────────────────
    longs: list = []
    shorts: list = []

    for i, ticker in enumerate(universe):
        long_result = scan_long(ticker, slot, spy_ret, qqq_ret)
        if long_result:
            longs.append(long_result)
            print(
                f"  ✓ LONG:  {ticker:<6}  "
                f"Price=${long_result['price']:.2f}  "
                f"RVOL={long_result['rvol']:.1f}x  "
                f"Runway={long_result['runway_pct']:.1f}%  "
                f"R/R=1:{long_result['rr']:.1f}"
            )

        short_result = scan_short(ticker, slot, spy_ret, qqq_ret)
        if short_result:
            shorts.append(short_result)
            print(
                f"  ✓ SHORT: {ticker:<6}  "
                f"Price=${short_result['price']:.2f}  "
                f"RVOL={short_result['rvol']:.1f}x  "
                f"Downside={short_result['downside_pct']:.1f}%  "
                f"R/R=1:{short_result['rr']:.1f}"
            )

        # Gentle rate limiting
        if (i + 1) % 25 == 0:
            time.sleep(2)

    # Sort by RVOL (strongest conviction first)
    longs.sort(key=lambda x: x['rvol'], reverse=True)
    shorts.sort(key=lambda x: x['rvol'], reverse=True)

    print(f"\n{'=' * 64}")
    print(f"  Scan complete — {len(longs)} long setup(s), {len(shorts)} short setup(s) found.")
    print(f"{'=' * 64}\n")

    # ── Write HTML output ──────────────────────────────────────────────────
    html        = build_html(longs, shorts, monitors, slot, scan_time, spy_ret, qqq_ret)
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"\n  ✓ index.html written ({len(html):,} bytes) → {output_path}\n")


if __name__ == '__main__':
    main()
