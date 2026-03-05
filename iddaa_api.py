"""
iddaa_api.py — İddaa.com HTTP Scraper API
Tarayıcı gerektirmez, direkt HTTP istekleri kullanır.
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from threading import Lock
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

IDDAA_KEY  = os.environ.get('IDDAA_KEY', 'trinity2024')
CACHE_TTL  = int(os.environ.get('CACHE_TTL_MIN', '30'))
PORT       = int(os.environ.get('PORT', 8080))

_cache: dict = {}
_cache_lock  = Lock()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
                  'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'tr-TR,tr;q=0.9,en;q=0.8',
    'Referer': 'https://www.iddaa.com/',
    'Origin': 'https://www.iddaa.com',
}

ENDPOINTS = [
    'https://sportprogram.iddaa.com/api/sportprogram/programs?sportId=1&programType=0',
    'https://sportprogram.iddaa.com/api/sportprogram/programs?sportId=1&programType=1',
    'https://sportsbettingapi.iddaa.com/api/sports/1/events?date=today',
    'https://www.iddaa.com/api/sportprogram/programs?sportId=1',
]


def parse_events(events: list) -> dict:
    odds_map = {}
    today = datetime.now().date()

    for ev in events:
        # Tarih kontrolü
        for date_field in ['eventDate', 'date', 'matchDate', 'startTime']:
            date_val = ev.get(date_field, '')
            if date_val:
                try:
                    if 'T' in str(date_val):
                        match_date = datetime.fromisoformat(
                            str(date_val).replace('Z', '+00:00')).date()
                    else:
                        match_date = datetime.strptime(
                            str(date_val)[:10], '%Y-%m-%d').date()
                    if match_date != today:
                        break
                except Exception:
                    pass

        home = (ev.get('homeTeamName') or ev.get('home') or
                ev.get('homeName') or ev.get('homeTeam') or '')
        away = (ev.get('awayTeamName') or ev.get('away') or
                ev.get('awayName') or ev.get('awayTeam') or '')
        if not home or not away:
            continue

        o1 = o0 = o2 = ust = alt = None
        outcomes = (ev.get('outcomes') or ev.get('odds') or
                    ev.get('markets') or [])

        for out in outcomes:
            name  = str(out.get('outcomeName') or out.get('name') or
                        out.get('type') or '')
            price = out.get('odd') or out.get('price') or out.get('value')
            if not price:
                continue
            try:
                price = float(price)
            except Exception:
                continue
            if price <= 1.0:
                continue

            if name in ('1', 'MS 1', 'home'):       o1  = price
            elif name in ('X', '0', 'MS X', 'draw'): o0  = price
            elif name in ('2', 'MS 2', 'away'):      o2  = price
            elif name in ('Üst 2.5', 'Over', 'ÜST'): ust = price
            elif name in ('Alt 2.5', 'Under', 'ALT'): alt = price

        if o1 and o1 > 1.0:
            odds_map[f"{home}||{away}"] = {
                '1': o1, '0': o0, '2': o2,
                'ust': ust, 'alt': alt,
                '_bookmaker': 'iddaa',
                '_source': 'iddaa_api',
            }

    return odds_map


def scrape_iddaa() -> dict:
    odds_map = {}

    for url in ENDPOINTS:
        try:
            log.info(f"Deneniyor: {url}")
            r = requests.get(url, headers=HEADERS, timeout=15)
            log.info(f"  → Status: {r.status_code}")

            if r.status_code != 200:
                continue

            data = r.json()
            log.info(f"  → JSON keys: {list(data.keys())[:5]}")

            # Farklı yapıları dene
            events = []
            for key in ['data', 'result', 'response', 'body']:
                sub = data.get(key)
                if isinstance(sub, dict):
                    events = (sub.get('events') or sub.get('matches') or
                              sub.get('programs') or [])
                    if events:
                        break
                elif isinstance(sub, list):
                    events = sub
                    break

            if not events:
                # Direkt liste mi?
                if isinstance(data, list):
                    events = data
                else:
                    # Her key'i tara
                    for k, v in data.items():
                        if isinstance(v, list) and len(v) > 0:
                            events = v
                            break

            log.info(f"  → {len(events)} etkinlik bulundu")
            if events:
                odds_map = parse_events(events)
                if odds_map:
                    log.info(f"  ✅ {len(odds_map)} maç oranı alındı")
                    break

        except Exception as e:
            log.warning(f"  ⚠️ Hata ({url}): {e}")
            continue

    if not odds_map:
        log.warning("Tüm endpointler başarısız, ham veriyi logla")
        # Son endpoint'ten ham veriyi logla
        try:
            r = requests.get(ENDPOINTS[0], headers=HEADERS, timeout=15)
            log.info(f"Ham veri (ilk 500 karakter): {r.text[:500]}")
        except Exception:
            pass

    return odds_map


def get_cached_odds(force: bool = False) -> dict:
    with _cache_lock:
        now = datetime.now()
        if (not force and _cache.get('odds') and _cache.get('ts')
                and now - _cache['ts'] < timedelta(minutes=CACHE_TTL)):
            return _cache
        try:
            odds = scrape_iddaa()
            _cache.update({'odds': odds, 'ts': now,
                           'count': len(odds), 'error': None})
        except Exception as e:
            log.error(f"Scrape hatası: {e}")
            _cache['error'] = str(e)
            if not _cache.get('odds'):
                _cache['odds'] = {}
        return _cache


def check_auth():
    return (request.headers.get('X-API-Key')
            or request.args.get('key')) == IDDAA_KEY


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'cached': bool(_cache.get('odds')),
                    'count': _cache.get('count', 0),
                    'time': datetime.now().isoformat()})


@app.route('/odds')
def get_odds():
    if not check_auth():
        return jsonify({'error': 'Yetkisiz erişim'}), 401
    force = request.args.get('force', '0') == '1'
    cache = get_cached_odds(force=force)
    return jsonify({'odds': cache.get('odds', {}), 'count': cache.get('count', 0),
                    'cached_at': cache['ts'].isoformat() if cache.get('ts') else None,
                    'error': cache.get('error')})


@app.route('/refresh', methods=['POST', 'GET'])
def force_refresh():
    if not check_auth():
        return jsonify({'error': 'Yetkisiz erişim'}), 401
    cache = get_cached_odds(force=True)
    return jsonify({'status': 'refreshed', 'count': cache.get('count', 0),
                    'error': cache.get('error')})


if __name__ == '__main__':
    log.info(f"İddaa API başlıyor — port {PORT}")
    import threading
    threading.Thread(target=get_cached_odds, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
