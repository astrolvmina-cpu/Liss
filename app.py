import math
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title='LISS')
st.title('LISS Gold Signal')
st.caption('Demo-account signal tool. Not financial advice. Not a guarantee of profit.')

# ---- Settings you can change ----
with st.expander('Settings and daily check', expanded=True):
    ACCOUNT = st.number_input('Demo balance ($)', value=2000.0, step=100.0)
    RISK_PCT = st.number_input('Risk per trade (%)', value=0.5, step=0.1, min_value=0.1, max_value=1.0)
    RR = st.number_input('Reward to risk', value=1.5, step=0.1, min_value=1.0)
    SPREAD = st.number_input('Current Exness spread ($)', value=0.30, step=0.05, min_value=0.0)
    traded_today = st.checkbox('I already took a trade today')
    loss_today = st.number_input('Loss so far today (% of balance)', value=0.0, step=0.1, min_value=0.0)

# ---- Fixed risk rules ----
MAX_SPREAD = 0.50        # skip if spread is wider ($)
MAX_LOTS = 0.05          # hard cap on lot size
DAILY_LOSS_LIMIT = 2.0   # stop for the day at this loss (%)
MAX_CHASE = 0.5          # no entry if price already ran this fraction of the range past the breakout
MAX_DATA_AGE_MIN = 25    # older data means market closed or feed stale
LOG_FILE = 'liss_signals.csv'


@st.cache_data(ttl=60)
def load_data():
    d = yf.download('GC=F', period='5d', interval='5m', progress=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d.dropna()
    d.columns = [c.lower() for c in d.columns]
    d.index = d.index.tz_convert('UTC')
    return d


def get_signal(data, now_utc):
    last = data.index[-1]
    price = float(data['close'].iloc[-1])
    out = {'time': last, 'price': price, 'signal': 'WAIT', 'reason': '',
           'entry': None, 'sl': None, 'tp': None, 'lots': None,
           'sl_dist': None, 'tp_dist': None}

    def veto(signal, reason):
        out['signal'] = signal
        out['reason'] = reason
        return out

    # ---- Risk manager first: it can veto everything ----
    if now_utc.weekday() >= 5:
        return veto('NO TRADE', 'Weekend, market closed')
    age_min = (now_utc - last.to_pydatetime()).total_seconds() / 60
    if age_min > MAX_DATA_AGE_MIN:
        return veto('NO TRADE', 'Price data is %.0f minutes old, market may be closed' % age_min)
    if SPREAD > MAX_SPREAD:
        return veto('NO TRADE', 'Spread %.2f is too wide (limit %.2f)' % (SPREAD, MAX_SPREAD))
    if loss_today >= DAILY_LOSS_LIMIT:
        return veto('NO TRADE', 'Daily loss limit reached. Stop for today')
    if traded_today:
        return veto('NO TRADE', 'One trade per day: you already traded today')

    # ---- Strategy: London opening range breakout ----
    ema = float(data['close'].ewm(span=240, adjust=False).mean().iloc[-1])
    day = data[data.index.normalize() == last.normalize()]
    rng = day.between_time('08:00', '08:25')
    if len(rng) < 5:
        return veto('WAIT', 'London opening range (08:00 to 08:30 UTC) not formed yet')
    hi = float(rng['high'].max())
    lo = float(rng['low'].min())
    size = hi - lo
    if size < 1.0 or size > 15.0:
        return veto('NO TRADE', 'Opening range %.2f is outside safe limits (1 to 15)' % size)
    hhmm = last.strftime('%H:%M')
    if hhmm < '08:30' or hhmm > '16:00':
        return veto('WAIT', 'Outside trading window (08:30 to 16:00 UTC)')

    if price > hi and price > ema:
        if price - hi > MAX_CHASE * size:
            return veto('WAIT', 'Breakout already ran %.2f past the range. Do not chase' % (price - hi))
        side, entry, sl = 'BUY', price, lo
        tp = entry + RR * (entry - sl)
        why = 'Broke above London range high %.2f, above trend %.2f' % (hi, ema)
    elif price < lo and price < ema:
        if lo - price > MAX_CHASE * size:
            return veto('WAIT', 'Breakout already ran %.2f past the range. Do not chase' % (lo - price))
        side, entry, sl = 'SELL', price, hi
        tp = entry - RR * (sl - entry)
        why = 'Broke below London range low %.2f, below trend %.2f' % (lo, ema)
    else:
        return veto('WAIT', 'Price inside range (%.2f to %.2f) or against the trend' % (lo, hi))

    # ---- Position size (1.00 lot = 100 oz, so $1 move = $100 per lot) ----
    stop_dist = abs(entry - sl)
    risk_money = ACCOUNT * RISK_PCT / 100
    lots = math.floor(risk_money / (stop_dist * 100) * 100) / 100
    if lots < 0.01:
        lots = 0.01
        if lots * stop_dist * 100 > risk_money * 2:
            return veto('NO TRADE', 'Stop is too wide even for 0.01 lots')
    lots = min(lots, MAX_LOTS)

    out.update(signal=side, reason=why, entry=entry, sl=sl, tp=tp, lots=lots,
               sl_dist=stop_dist, tp_dist=abs(tp - entry))
    return out


if st.button('Get Signal', type='primary'):
    try:
        raw = load_data()
    except Exception:
        raw = pd.DataFrame()

    if raw.empty:
        st.error('Could not load price data. Wait a minute and tap again.')
    else:
        now_utc = datetime.now(timezone.utc)
        data = raw
        if data.index[-1] + pd.Timedelta(minutes=5) > now_utc:
            data = data.iloc[:-1]  # ignore the candle that is still forming
        s = get_signal(data, now_utc)

        st.header(s['signal'])
        st.write('Reason:', s['reason'])
        st.write('Last closed candle (UTC):', str(s['time']))
        st.write('Futures price: %.2f' % s['price'])

        if s['entry'] is not None:
            risk_usd = s['lots'] * s['sl_dist'] * 100
            st.subheader('Trade plan')
            st.write('Lots: %.2f (risking about $%.2f)' % (s['lots'], risk_usd))
            st.write('Stop loss: $%.2f away from your entry' % s['sl_dist'])
            st.write('Take profit: $%.2f away from your entry' % s['tp_dist'])
            st.write('Futures levels for reference: entry %.2f, stop %.2f, target %.2f'
                     % (s['entry'], s['sl'], s['tp']))
            st.write('Invalid if price closes back inside the London opening range.')
            st.warning('Gold futures price differs from Exness XAUUSD. In MT5, enter at your own '
                       'market price and set stop and target using the distances above. '
                       'Skip major news times.')

        cols = ['time', 'signal', 'price', 'entry', 'sl', 'tp', 'lots', 'reason']
        row = pd.DataFrame([{k: s[k] for k in cols}])
        row.to_csv(LOG_FILE, mode='a', header=not os.path.exists(LOG_FILE), index=False)

st.divider()
st.subheader('Signal log')
try:
    st.dataframe(pd.read_csv(LOG_FILE).tail(20))
except Exception:
    st.write('No signals yet.')
