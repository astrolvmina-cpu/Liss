import math
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
import yfinance as yf

st.set_page_config(page_title='LISS')
st.title('LISS Gold Coach')
st.caption('Demo-account tool. Not financial advice. Not a guarantee of profit.')

# ---- Locked safe numbers ----
ACCOUNT = 2000.0
RISK_PCT = 0.5
RR = 1.5
SPREAD = 0.30
MAX_SPREAD = 0.50
MAX_LOTS = 0.01
MAX_CHASE = 0.5
MAX_DATA_AGE_MIN = 25
MODEL = 'claude-sonnet-4-5'
LOG_FILE = 'liss_signals.csv'

with st.expander('Daily check (tap to open)', expanded=True):
    traded_today = st.checkbox('I already took a trade today')
    loss_today = st.number_input('Loss so far today (% of balance)', value=0.0, step=0.1, min_value=0.0)


@st.cache_data(ttl=60)
def load_data():
    d = yf.download('GC=F', period='5d', interval='5m', progress=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d.dropna()
    d.columns = [c.lower() for c in d.columns]
    d.index = d.index.tz_convert('UTC')
    return d


@st.cache_data(ttl=900)
def load_news():
    items = []
    try:
        for n in (yf.Ticker('GC=F').news or [])[:6]:
            c = n.get('content', n)
            title = c.get('title')
            if title:
                items.append(title)
    except Exception:
        pass
    return items


def high_impact_warning(now_utc):
    # Rule-based warning for common big events. Check an economic calendar for exact dates.
    notes = []
    wd, day, hr = now_utc.weekday(), now_utc.day, now_utc.hour
    if wd == 4 and day <= 7:
        notes.append('First Friday of the month: US jobs report is usually released at 13:30 UTC. Do not trade around it.')
    if wd in (1, 2) and 12 <= hr <= 15:
        notes.append('US data releases often land around 12:30 to 14:00 UTC on weekdays. Check the calendar.')
    if wd == 2:
        notes.append('Wednesdays can have Fed or FOMC news. Check the calendar before trading.')
    return notes


def get_signal(data, now_utc):
    last = data.index[-1]
    price = float(data['close'].iloc[-1])
    ema = float(data['close'].ewm(span=240, adjust=False).mean().iloc[-1])
    out = {'time': last, 'price': price, 'ema': ema, 'signal': 'WAIT', 'reason': '',
           'entry': None, 'sl': None, 'tp': None, 'lots': None,
           'sl_dist': None, 'tp_dist': None, 'hi': None, 'lo': None}

    def veto(signal, reason):
        out['signal'] = signal
        out['reason'] = reason
        return out

    if now_utc.weekday() >= 5:
        return veto('NO TRADE', 'Weekend, market closed')
    age_min = (now_utc - last.to_pydatetime()).total_seconds() / 60
    if age_min > MAX_DATA_AGE_MIN:
        return veto('NO TRADE', 'Price data is %.0f minutes old, market may be closed' % age_min)
    if SPREAD > MAX_SPREAD:
        return veto('NO TRADE', 'Spread too wide')
    if loss_today >= 2.0:
        return veto('NO TRADE', 'Daily loss limit reached. Stop for today')
    if traded_today:
        return veto('NO TRADE', 'One trade per day: you already traded today')

    day = data[data.index.normalize() == last.normalize()]
    rng = day.between_time('08:00', '08:25')
    if len(rng) < 5:
        return veto('WAIT', 'London opening range (08:00 to 08:30 UTC) not formed yet')
    hi = float(rng['high'].max())
    lo = float(rng['low'].min())
    out['hi'], out['lo'] = hi, lo
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

    stop_dist = abs(entry - sl)
    risk_money = ACCOUNT * RISK_PCT / 100
    lots = math.floor(risk_money / (stop_dist * 100) * 100) / 100
    if lots < 0.01:
        lots = 0.01
    lots = min(lots, MAX_LOTS)
    risk_usd = lots * stop_dist * 100
    if risk_usd > risk_money * 2:
        return veto('NO TRADE', 'Stop is too wide even for 0.01 lots (would risk $%.2f)' % risk_usd)

    out.update(signal=side, reason=why, entry=entry, sl=sl, tp=tp, lots=lots,
               sl_dist=stop_dist, tp_dist=abs(tp - entry))
    return out


def market_context(s, news, warnings):
    lines = [
        'Instrument: gold futures (GC=F), 5-minute candles, proxy for XAUUSD.',
        'Last closed candle (UTC): %s' % s['time'],
        'Price: %.2f. Trend EMA (about 20 hours): %.2f.' % (s['price'], s['ema']),
        'Rule-based signal: %s. Reason: %s' % (s['signal'], s['reason']),
    ]
    if s['hi'] is not None:
        lines.append('London opening range: low %.2f, high %.2f.' % (s['lo'], s['hi']))
    if s['entry'] is not None:
        lines.append('Trade plan: lots %.2f, stop distance $%.2f, target distance $%.2f.'
                     % (s['lots'], s['sl_dist'], s['tp_dist']))
    lines.append('Calendar warnings: ' + ('; '.join(warnings) if warnings else 'none'))
    lines.append('Recent headlines: ' + (' | '.join(news) if news else 'none available'))
    return '\n'.join(lines)


SYSTEM = (
    'You are LISS Coach, a calm, honest gold trading coach for a beginner using a DEMO account. '
    'You explain the market in plain, short sentences. You use ONLY the market data provided. '
    'You never promise profit and never predict with certainty. Say bullish, bearish or neutral '
    'and give the reason in simple words. The rule-based signal and risk limits are final: you may '
    'explain them but never override them, never suggest bigger lots, never suggest removing a stop loss. '
    'If the signal is WAIT or NO TRADE, say so clearly and explain why. Mention news risk when relevant. '
    'Keep answers under 120 words.'
)


def ask_ai(messages, context):
    key = st.secrets.get('ANTHROPIC_API_KEY', None)
    if not key:
        return 'AI key is missing. Add ANTHROPIC_API_KEY in the app Secrets settings.'
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=MODEL, max_tokens=400,
            system=SYSTEM + '\n\nLIVE DATA:\n' + context,
            messages=messages)
        return resp.content[0].text
    except Exception as e:
        return 'The AI could not answer right now (%s). The rule-based signal above still works.' % type(e).__name__


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
            data = data.iloc[:-1]
        s = get_signal(data, now_utc)
        news = load_news()
        warnings = high_impact_warning(now_utc)
        st.session_state['sig'] = s
        st.session_state['ctx'] = market_context(s, news, warnings)
        st.session_state['news'] = news
        st.session_state['warn'] = warnings
        st.session_state['read'] = ask_ai(
            [{'role': 'user', 'content': 'Give me the market read now: bullish, bearish or neutral, why, and what to do.'}],
            st.session_state['ctx'])
        cols = ['time', 'signal', 'price', 'entry', 'sl', 'tp', 'lots', 'reason']
        row = pd.DataFrame([{k: s[k] for k in cols}])
        row.to_csv(LOG_FILE, mode='a', header=not os.path.exists(LOG_FILE), index=False)

if 'sig' in st.session_state:
    s = st.session_state['sig']
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
        st.write('Invalid if price closes back inside the London opening range.')
        st.warning('Futures price differs from Exness XAUUSD. Enter at your own market price and apply the distances above.')
    st.subheader('Coach read')
    st.write(st.session_state['read'])
    st.subheader('News check')
    for w in st.session_state['warn']:
        st.warning(w)
    if not st.session_state['warn']:
        st.write('No scheduled high-impact warning from the built-in rules. Still check an economic calendar.')
    for h in st.session_state['news']:
        st.write('- ' + h)

    st.divider()
    st.subheader('Ask the coach')
    if 'chat' not in st.session_state:
        st.session_state['chat'] = []
    for m in st.session_state['chat']:
        with st.chat_message(m['role']):
            st.write(m['content'])
    q = st.chat_input('Ask about gold, the signal, risk, anything')
    if q:
        st.session_state['chat'].append({'role': 'user', 'content': q})
        answer = ask_ai(st.session_state['chat'][-10:], st.session_state['ctx'])
        st.session_state['chat'].append({'role': 'assistant', 'content': answer})
        st.rerun()

st.divider()
st.subheader('Signal log')
try:
    st.dataframe(pd.read_csv(LOG_FILE).tail(20))
except Exception:
    st.write('No signals yet.')
