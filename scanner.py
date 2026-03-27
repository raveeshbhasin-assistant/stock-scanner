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
        daily = tk.history(period="3mo", interval="1d", auto_adjust=True)
        if len(daily) < 15:
            return None

        # Intraday 5-min data
        intra = tk.history(period="1d", interval="5m", auto_adjust=True)
        session_active = len(intra) >= 6
        if not session_active:
            # Market is closed — fall back to most recent trading session
            print(f"  [INFO] {ticker}: no live intraday data, fetching 5d fallback...")
            intra_5d = tk.history(period="5d", interval="5m", auto_adjust=True)
            if len(intra_5d) < 6:
                print(f"  [WARN] {ticker}: 5d fallback also empty")
                return None
            intra_5d.index = intra_5d.index.tz_convert(EST)
            last_date = intra_5d.index[-1].strftime("%Y-%m-%d")
            intra = intra_5d[intra_5d.index.strftime("%Y-%m-%d") == last_date]
            if len(intra) < 6:
                print(f"  [WARN] {ticker}: filtered last-day slice too small ({len(intra)} bars)")
                return None
            print(f"  [INFO] {ticker}: using {last_date} session data ({len(intra)} bars)")

        # Pre/post market 1-min data (use 2d period to capture weekend AH data)
        prepost = None
        try:
            prepost = tk.history(period="1d", interval="1m", prepost=True, auto_adjust=True)
            if prepost is not None and len(prepost) == 0:
                prepost = tk.history(period="2d", interval="1m", prepost=True, auto_adjust=True)
            if prepost is not None and len(prepost) > 0:
                prepost.index = prepost.index.tz_convert(EST)
        except Exception:
            pass

        # Calculate all metrics
        price = float(intra["Close"].iloc[-1])
        vwap = calc_vwap(intra)
        atr_pct = calc_atr_pct(daily)
        rvol = calc_rvol(intra, float(adv))

        closes = intra["Close"]
        ema9_series = ema(closes, 9)
        ema20_series = ema(closes, 20)
        ema9 = float(ema9_series.iloc[-1])
        ema20 = float(ema20_series.iloc[-1])

        climax = is_climax_volume(intra)

        prev_close = float(daily["Close"].iloc[-2]) if len(daily) >= 2 else price
        gap_pct = (price - prev_close) / prev_close * 100

        first_price = float(intra["Close"].iloc[0])
        ticker_ret = (price - first_price) / first_price * 100

        prev_day_high = float(daily["High"].iloc[-2]) if len(daily) >= 2 else price
        prev_day_low = float(daily["Low"].iloc[-2]) if len(daily) >= 2 else price
        week52_high = float(daily["High"].max())
        week52_low = float(daily["Low"].min())

        premarket_high = None
        premarket_low = None
        if prepost is not None and len(prepost) > 0:
            pm_only = prepost[prepost.index.time < pd.Timestamp("09:30").time()]
            if len(pm_only) > 0:
                premarket_high = float(pm_only["High"].max())
                premarket_low = float(pm_only["Low"].min())

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
        }

    except Exception as e:
        print(f"  [ERROR] fetch_monitor({ticker}): {type(e).__name__}: {e}")
        return None


# ─── HTML GENERATOR ───────────────────────────────────────────────────────────

def _format_long_card(s: dict, color: str, spy_ret: float, qqq_ret: float) -> str:
    sent = f"{s['sentiment_pct']:.0f}% Positive" if s['sentiment_pct'] is not None else 'N/A'
    rr_str = f"1:{s['rr']:.1f}"
    gap_cls = 'gap-up' if s['gap_pct'] >= 0 else 'gap-down'
    gap_sym = '▲' if s['gap_pct'] >= 0 else '▼'
    hl = s['headline'] or 'Strong institutional momentum — no specific news catalyst identified.'

    return f"""
<div class="card long-card" style="border-left-color:{color};">
  <div class="card-header">
    <span class="ticker" style="color:{color};">{s['ticker']}</span>
    <span class="price">${s['price']:.2f}</span>
    <span class="badge {gap_cls}">{gap_sym} {abs(s['gap_pct']):.2f}%</span>
  </div>

  <div class="quals-row">
    Cap ${s['mkt_cap_b']:.0f}B | ADV {s['adv_m']:.1f}M | ATR {s['atr_pct']:.1f}% | RVOL {s['rvol']:.1f}x | RS {s['ticker_ret']:+.1f}% vs SPY {spy_ret:+.1f}% | Runway {s['runway_pct']:.1f}% | R/Q 1:{s['rr']:.1f}
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


def _format_short_card(s: dict, color: str, spy_ret: float, qqq_ret: float) -> str:
    sent = f"{s['sentiment_pct']:.0f}% Positive" if s['sentiment_pct'] is not None else 'N/A'
    rr_str = f"1:{s['rr']:.1f}"
    gap_cls = 'gap-up' if s['gap_pct'] >= 0 else 'gap-down'
    gap_sym = '▲' if s['gap_pct'] >= 0 else '▼'
    hl = s['headline'] or 'Institutional selling pressure — no specific news catalyst identified.'

    return f"""
<div class="card short-card" style="border-left-color:{color};">
  <div class="card-header">
    <span class="ticker" style="color:{color};">{s['ticker']}</span>
    <span class="price">${s['price']:.2f}</span>
    <span class="badge {gap_cls}">{gap_sym} {abs(s['gap_pct']):.2f}%</span>
  </div>

  <div class="quals-row">
    Cap ${s['mkt_cap_b']:.0f}B | ADV {s['adv_m']:.1f}M | ATR {s['atr_pct']:.1f}% | RVOL {s['rvol']:.1f}x | RS {s['ticker_ret']:+.1f}% vs SPY {spy_ret:+.1f}% | Downside {s['downside_pct']:.1f}% | R/R 1:{s['rr']:.1f}
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
      <div class="lbl">Weakness vs Market</div>
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
      <span class="tl">Target ({s['sup_label']})</span>
      <span class="tv green">${s['support']:.2f} &nbsp;(&minus;{s['downside_pct']:.1f}% downside)</span>
    </div>
    <div class="trade-row last">
      <span class="tl">Hard Stop (VWAP)</span>
      <span class="tv red">${s['stop']:.2f} &nbsp;(+{s['risk_pct']:.1f}% risk &rarr; {rr_str} R/R)</span>
    </div>
  </div>
</div>"""


def _format_monitor_card(m: dict, spy_ret: float, qqq_ret: float) -> str:
    gap_cls = 'gap-up' if m['gap_pct'] >= 0 else 'gap-down'
    gap_sym = '▲' if m['gap_pct'] >= 0 else '▼'
    hl = m['headline'] or 'Monitoring price action and volatility.'
    sent = f"{m['sentiment_pct']:.0f}% Positive" if m['sentiment_pct'] is not None else 'N/A'

    market_status = m.get('market_status', 'LIVE')
    ah_price = m.get('ah_price')
    session_active = m.get('session_active', True)

    # Market status badge styling
    status_colors = {
        'LIVE':       ('#00ff88', '#003318'),
        'AFTER HRS':  ('#f4a261', '#2d1a08'),
        'PRE-MKT':    ('#58a6ff', '#0b1e35'),
        'PREV CLOSE': ('#8b949e', '#161b22'),
    }
    status_fg, status_bg = status_colors.get(market_status, ('#8b949e', '#161b22'))
    status_badge = (
        f'<span style="background:{status_bg};color:{status_fg};border:1px solid {status_fg}44;'
        f'border-radius:4px;padding:2px 8px;font-size:10px;letter-spacing:1px;">'
        f'{market_status}</span>'
    )

    # Price display — show AH/PM price when available, last-session close otherwise
    if ah_price is not None:
        display_price = ah_price
        price_label = 'Extended Hrs Price'
        session_label = 'Extended Hrs'
    elif not session_active:
        display_price = m['price']
        price_label = 'Last Session Close'
        session_label = 'Last Session'
    else:
        display_price = m['price']
        price_label = 'Current Price'
        session_label = 'Today'

    ah_row = ''
    if ah_price is not None:
        ah_chg = (ah_price - m['price']) / m['price'] * 100
        ah_cls = 'hot' if ah_chg >= 0 else ''
        ah_sign = '+' if ah_chg >= 0 else ''
        ah_row = f"""
    <div class="metric">
      <div class="lbl">Last Session Close</div>
      <div class="val">${m['price']:.2f}</div>
    </div>
    <div class="metric">
      <div class="lbl">Extended Hrs Change</div>
      <div class="val {ah_cls}">{ah_sign}{ah_chg:.2f}%</div>
    </div>"""

    long_badges = ''
    for label, actual, threshold, passes in m['long_checks']:
        badge_class = 'check-pass' if passes else 'check-fail'
        long_badges += f'<span class="check-badge {badge_class}">{"✓" if passes else "✗"} {label} {actual}</span>'

    short_badges = ''
    for label, actual, threshold, passes in m['short_checks']:
        badge_class = 'check-pass' if passes else 'check-fail'
        short_badges += f'<span class="check-badge {badge_class}">{"✓" if passes else "✗"} {label} {actual}</span>'

    return f"""
<div class="card monitor-card" style="border-left-color:{MONITOR_CARD_COLOR};">
  <div class="card-header">
    <span class="ticker" style="color:{MONITOR_CARD_COLOR};">{m['ticker']}</span>
    <span class="price">${display_price:.2f}</span>
    <span class="badge {gap_cls}">{gap_sym} {abs(m['gap_pct']):.2f}%</span>
    <span class="badge neutral">{m['ticker_ret']:+.2f}% {session_label}</span>
    {status_badge}
  </div>

  <div class="grid-2">
    <div class="metric">
      <div class="lbl">{price_label}</div>
      <div class="val">${display_price:.2f}</div>
    </div>
    <div class="metric">
      <div class="lbl">{session_label} Return</div>
      <div class="val hot">{m['ticker_ret']:+.2f}%</div>
    </div>{ah_row}
    <div class="metric">
      <div class="lbl">Sentiment</div>
      <div class="val">{sent}</div>
    </div>
    <div class="metric">
      <div class="lbl">Market Context</div>
      <div class="val">SPY {spy_ret:+.2f}% | QQQ {qqq_ret:+.2f}%</div>
    </div>
  </div>

  <div class="catalyst-box">
    <span class="section-lbl">HEADLINE</span>
    <p>{hl}</p>
  </div>

  <div class="checks-section">
    <div class="checks-label">LONG Qualification ({session_label} data)</div>
    <div class="checks-row">{long_badges}</div>
  </div>

  <div class="checks-section">
    <div class="checks-label">SHORT Qualification ({session_label} data)</div>
    <div class="checks-row">{short_badges}</div>
  </div>
</div>"""


def build_html(longs: list, shorts: list, monitors: list, slot: str, scan_time: str,
               spy_ret: float, qqq_ret: float) -> str:
    slot_info = TIME_SLOTS[slot]
    long_count = len(longs)
    short_count = len(shorts)

    long_cards = ''.join(
        _format_long_card(s, LONG_CARD_COLORS[i % len(LONG_CARD_COLORS)], spy_ret, qqq_ret)
        for i, s in enumerate(longs)
    )
    if not long_cards:
        long_cards = """
<div class="no-setups">
  <div class="no-icon">&#9888;</div>
  <div class="no-msg">No long setups meet the criteria.</div>
  <div class="no-sub">Capital preservation is the priority.</div>
</div>"""

    short_cards = ''.join(
        _format_short_card(s, SHORT_CARD_COLORS[i % len(SHORT_CARD_COLORS)], spy_ret, qqq_ret)
        for i, s in enumerate(shorts)
    )
    if not short_cards:
        short_cards = """
<div class="no-setups">
  <div class="no-icon">&#9888;</div>
  <div class="no-msg">No short setups meet the criteria.</div>
  <div class="no-sub">Capital preservation is the priority.</div>
</div>"""

    monitor_cards = ''.join(
        _format_monitor_card(m, spy_ret, qqq_ret)
        for m in monitors
        if m is not None
    )

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

    /* ── Section Headers ────────────────────────────── */
    .section-header {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-top: 42px;
      margin-bottom: 20px;
      padding-bottom: 12px;
      border-bottom: 2px solid #1c2030;
      font-size: 14px;
      font-weight: bold;
      color: #e6edf3;
      letter-spacing: 1px;
    }}
    .section-header.long {{
      color: #00ff88;
      border-bottom-color: #00ff88;
    }}
    .section-header.short {{
      color: #ff6b6b;
      border-bottom-color: #ff6b6b;
    }}
    .section-header.monitor {{
      color: #8b5cf6;
      border-bottom-color: #8b5cf6;
    }}
    .section-header .count {{
      background: rgba(255, 255, 255, 0.1);
      padding: 3px 10px;
      border-radius: 4px;
      font-size: 12px;
    }}
    .section-header .rules {{
      margin-left: auto;
      font-size: 11px;
      color: #8b949e;
      font-weight: normal;
      letter-spacing: 0.5px;
    }}

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
      margin-bottom: 16px;
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

    /* ── Quals Row ───────────────────────────────────── */
    .quals-row {{
      background: #111827;
      border-radius: 6px;
      padding: 10px 12px;
      margin-bottom: 16px;
      font-size: 11px;
      color: #8b949e;
      line-height: 1.5;
      overflow-x: auto;
    }}

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

    /* ── Checks section (monitors) ────────────────────── */
    .checks-section {{
      background: #111827;
      border-radius: 6px;
      padding: 12px 14px;
      margin-bottom: 12px;
    }}
    .checks-label {{
      font-size: 9px;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 1px;
      margin-bottom: 8px;
    }}
    .checks-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .check-badge {{
      display: inline-block;
      padding: 4px 8px;
      border-radius: 3px;
      font-size: 10px;
      font-weight: bold;
    }}
    .check-pass {{
      background: #0d2b1a;
      color: #00ff88;
    }}
    .check-fail {{
      background: #2b0d0d;
      color: #ff6b6b;
    }}

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
      .quals-row     {{ font-size: 10px; }}
      .section-header {{
        flex-direction: column;
        align-items: flex-start;
      }}
      .section-header .rules {{
        margin-left: 0;
        margin-top: 8px;
      }}
    }}
  </style>
</head>
<body>
  <div class="container">

    <div class="header">
      <div class="header-badge">Elite Day Trading Research</div>
      <h1>&#9650; ▼ Momentum Scanner</h1>
      <div class="slot-label">{slot_info['label']}</div>
      <div class="focus-text">Focus: {slot_info['focus']}</div>
      <div class="scan-meta">
        <div>Scanned <span class="val">{scan_time}</span></div>
        <div>SPY <span class="val" style="color:{spy_color}">{spy_ret:+.2f}%</span></div>
        <div>QQQ <span class="val" style="color:{qqq_color}">{qqq_ret:+.2f}%</span></div>
        <div>Long <span class="val">{long_count}</span></div>
        <div>Short <span class="val">{short_count}</span></div>
      </div>
    </div>

    <!-- LONG SETUPS SECTION -->
    <div class="section-header long">
      <span>▲ LONG SETUPS ({long_count} found)</span>
      <span class="rules">Price&gt;VWAP · RVOL&gt;1.2x · Above 9/20-EMA · RS outperforms SPY or QQQ · Runway≥1% · R/R≥1:1.5</span>
    </div>
    {long_cards}

    <!-- SHORT SETUPS SECTION -->
    <div class="section-header short">
      <span>▼ SHORT SETUPS ({short_count} found)</span>
      <span class="rules">Price&lt;VWAP · RVOL&gt;1.2x · Below 9/20-EMA · RS underperforms SPY and QQQ · Downside≥1% · R/R≥1:1.5</span>
    </div>
    {short_cards}

    <!-- MONITOR SECTION -->
    <div class="section-header monitor">
      <span>◉ MARKET MONITOR — GOOGL & NVDA</span>
    </div>
    {monitor_cards}

    <div class="footer">
      Page auto-refreshes every 5 minutes &nbsp;&bull;&nbsp;
      Powered by yfinance + Alpha Vantage &nbsp;&bull;&nbsp;
      Rules: Cap&gt;$20B &middot; ADV&gt;2M &middot; ATR&ge;1.5% &middot; RVOL&gt;1.2x &middot; Runway&ge;1% &middot; R/R&ge;1:1.5<br>
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

    # ── Write HTML output ──────────────────────────────────────────────────────
    html        = build_html(longs, shorts, monitors, slot, scan_time, spy_ret, qqq_ret)
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"\n  ✓ index.html written ({len(html):,} bytes) → {output_path}\n")


if __name__ == '__main__':
    main()
