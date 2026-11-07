#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Монитор сигналов MOEX (TQBR) -> уведомления в Telegram и/или MAX.

Сигналы (из ТЗ заказчика):
  1) свеча +/- N% по тикеру        — LASTCHANGEPRCNT к предыдущему закрытию
  2) изменение статуса торгов      — TRADINGSTATUS (T=торги, иначе приостановка)
  3) расширение планок             — колонки LIMITUP/LIMITDOWN, если их отдаёт ISS
  4) приостановка торгов по акции  — переход TRADINGSTATUS != 'T'

Размер плеча (маржинальные ставки) биржа по ISS не отдаёт — это данные брокера;
точка расширения: extra_feed.py, подключить API брокера (Alor/T-Invest/Finam).

Режимы:
  python signal_watch.py             # боевой опрос ISS (нужен интернет до moex.com)
  python signal_watch.py --demo      # на demo_snapshot.json (реальный снимок ISS)
  python signal_watch.py --demo --shock   # + синтетический сигнал для проверки алертов

Отправка: TG_TOKEN+TG_CHAT и/или MAX_TOKEN+MAX_CHAT; без токенов — печать в консоль.
Состояние state.json — повторных алертов по тому же сигналу нет.
"""
import json, os, sys, time, urllib.request, urllib.parse
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = 'https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json'
COLS = 'SECID,LAST,LASTCHANGEPRCNT,TRADINGSTATUS,UPDATETIME,SYSTIME'
HERE = Path(__file__).parent
STATE = HERE / 'state.json'
WATCH_FILE = HERE / 'watchlist.txt'
POLL_SECONDS = int(os.environ.get('POLL_SECONDS', '60'))
THRESHOLD_PCT = float(os.environ.get('THRESHOLD_PCT', '2.0'))

STATUS_RU = {
    'T': 'торгуется', 'S': 'торги приостановлены', 'N': 'недоступна для торгов',
    'R': 'торги приостановлены (режим)', 'P': 'пауза', 'F': 'закрыта',
}


def read_watchlist():
    if not WATCH_FILE.exists():
        return ['SBER', 'GAZR', 'LKOH', 'YDEX', 'MGNT', 'ROSN', 'VTBR', 'OZON', 'NVTK', 'TATN']
    return [l.split('#')[0].strip().upper() for l in WATCH_FILE.read_text(encoding='utf-8').splitlines() if l.split('#')[0].strip()]


def load_state():
    return json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}


def save_state(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding='utf-8')


def fetch_iss():
    q = urllib.parse.urlencode({
        'iss.meta': 'off', 'iss.only': 'securities,marketdata',
        'securities.columns': 'SECID,SHORTNAME',
        'marketdata.columns': COLS})
    with urllib.request.urlopen(f'{BASE}?{q}', timeout=20) as r:
        d = json.load(r)
    names = {r[0]: r[1] for r in d['securities']['data']}
    cols = d['marketdata']['columns']
    return [{**dict(zip(cols, row)), 'SHORTNAME': names.get(row[0], '')}
            for row in d['marketdata']['data']]


def notify(text):
    sent = False
    tok, chat = os.environ.get('TG_TOKEN'), os.environ.get('TG_CHAT')
    if tok and chat:
        try:
            urllib.request.urlopen(
                f'https://api.telegram.org/bot{tok}/sendMessage',
                data=urllib.parse.urlencode({'chat_id': chat, 'text': text}).encode(), timeout=15)
            sent = True
        except Exception as e:
            print(f'[tg-err] {e}')
    mtok, mchat = os.environ.get('MAX_TOKEN'), os.environ.get('MAX_CHAT')
    if mtok and mchat:
        try:
            urllib.request.urlopen(
                f'https://botapi.max.ru/messages?access_token={mtok}',
                data=json.dumps({'chat_id': int(mchat), 'text': text}).encode(), timeout=15)
            sent = True
        except Exception as e:
            print(f'[max-err] {e}')
    print(f'[{"sent" if sent else "dry"}] {text}')


def detect(rows, state, watch):
    alerts = []
    for r in rows:
        sec = r.get('SECID')
        if watch and sec not in watch:
            continue
        name = r.get('SHORTNAME') or sec
        chg = r.get('LASTCHANGEPRCNT')
        if isinstance(chg, (int, float)) and abs(chg) >= THRESHOLD_PCT:
            key = f'pct:{sec}:{round(chg, 2)}'
            if key not in state:
                alerts.append((key, f'📈 {sec} {name}: свеча {chg:+.2f}% — цена {r.get("LAST")}'))
        st = r.get('TRADINGSTATUS')
        prev = state.get(f'status:{sec}')
        if st != prev:
            if st and st != 'T' and prev is not None:
                alerts.append((f'halt:{sec}:{st}:{r.get("UPDATETIME")}',
                               f'🚨 {sec} {name}: {STATUS_RU.get(st, "статус " + st)} '
                               f'(было: {STATUS_RU.get(prev, prev)})'))
            state[f'status:{sec}'] = st
        for lim in ('LIMITUP', 'LIMITDOWN'):
            v = r.get(lim)
            if v is not None and state.get(f'{lim}:{sec}') != v and f'{lim}:{sec}' in state:
                alerts.append((f'{lim}:{sec}:{v}', f'📏 {sec} {name}: {lim} изменился → {v}'))
            if v is not None:
                state[f'{lim}:{sec}'] = v
    return alerts


def main():
    demo = '--demo' in sys.argv
    if demo:
        snap = json.loads((HERE / 'demo_snapshot.json').read_text(encoding='utf-8'))
        rows = snap['rows']
        print(f'[demo] снимок ISS: {snap.get("captured")}, {len(rows)} тикеров')
        if '--shock' in sys.argv:
            rows = [dict(r) for r in rows] + [
                {'SECID': 'GAZR', 'SHORTNAME': 'Газпром', 'LAST': 182.5,
                 'LASTCHANGEPRCNT': 3.4, 'TRADINGSTATUS': 'T', 'UPDATETIME': 'demo'},
                {'SECID': 'SBER', 'SHORTNAME': 'Сбербанк', 'LAST': 293.92,
                 'LASTCHANGEPRCNT': 0.03, 'TRADINGSTATUS': 'S', 'UPDATETIME': 'demo'},
            ]
            print('[demo] добавлены тестовые сигналы: GAZR +3.4%, SBER -> приостановка')
    else:
        rows = fetch_iss()
    watch = read_watchlist()
    state = load_state()
    state.setdefault('_boot', True)
    if demo or '_init' in state:
        # первый прогон только запоминает статусы, чтобы не было фальшивых «halt»
        pass
    alerts = detect(rows, state, watch)
    if '_init' not in state:
        state['_init'] = True
        alerts = [a for a in alerts if not a[0].startswith('halt:')] if not demo else alerts
    for key, text in alerts:
        notify(text)
        state[key] = True
    save_state(state)
    print(f'[{time.strftime("%H:%M:%S")}] проверено {len(rows)} строк, алертов: {len(alerts)}')
    if not demo:
        while True:
            time.sleep(POLL_SECONDS)
            try:
                rows = fetch_iss()
                alerts = detect(rows, load_state() | state, watch)
                for key, text in alerts:
                    notify(text)
                    state[key] = True
                save_state(state)
            except Exception as e:
                print(f'[poll-err] {e}')


if __name__ == '__main__':
    main()
