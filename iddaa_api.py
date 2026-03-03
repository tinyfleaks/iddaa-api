"""
iddaa_api.py — İddaa.com Playwright Scraper API
Railway'de ayrı servis olarak çalışır.
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from threading import Lock
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

API_SECRET = os.environ.get('API_SECRET', 'trinity_secret_key')
CACHE_TTL  = int(os.environ.get('CACHE_TTL_MIN', '30'))
PORT       = int(os.environ.get('PORT', 8080))

_cache: dict = {}
_cache_lock  = Lock()


def scrape_iddaa() -> tuple:
    """Playwright ile iddaa.com'dan oranları çek."""
    from playwright.sync_api import sync_playwright

    odds_map = {}
    screenshot_b64 = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-blink-features=AutomationControlled',
            ]
        )
        context = browser.new_context(
            viewport={'width': 1280, 'height': 900},
            locale='tr-TR',
            user_agent=(
                'Mozilla/5.0 (X11; Linux x86_64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/122.0.0.0 Safari/537.36'
            )
        )
        page = context.new_page()

        try:
            # 1. Önce JSON API'yi dene
            log.info("İddaa mobil API deneniyor...")
            try:
                api_resp = page.goto(
                    'https://sportprogram.iddaa.com/api/sportprogram/programs?sportId=1&programType=0',
                    timeout=15000, wait_until='domcontentloaded')
                if api_resp and api_resp.status == 200:
                    body = page.content()
                    import re
                    match = re.search(r'<pre[^>]*>(.*?)</pre>', body, re.DOTALL)
                    data = json.loads(match.group(1) if match else body)
                    events = (data.get('data', {}).get('events', [])
                              or data.get('events', []) or [])
                    log.info(f"JSON API: {len(events)} etkinlik")
                    for ev in events:
                        home = ev.get('homeTeamName', '') or ev.get('home', '')
                        away = ev.get('awayTeamName', '') or ev.get('away', '')
                        if not home or not away:
                            continue
                        o1 = o0 = o2 = None
                        for out in ev.get('outcomes', ev.get('odds', [])):
                            name  = str(out.get('outcomeName', out.get('name', '')))
                            price = out.get('odd', out.get('price'))
                            if not price:
                                continue
                            price = float(price)
                            if name == '1':   o1 = price
                            elif name == 'X': o0 = price
                            elif name == '2': o2 = price
                        if o1 and o1 > 1.0:
                            odds_map[f"{home}||{away}"] = {
                                '1': o1, '0': o0, '2': o2,
                                'ust': None, 'alt': None,
                                '_bookmaker': 'iddaa',
                                '_source': 'iddaa_json_api',
                            }
            except Exception as api_err:
                log.warning(f"JSON API hatası: {api_err}")

            # 2. JSON başarısızsa HTML sayfayı tara
            if not odds_map:
                log.info("HTML sayfa taranıyor...")
                page.goto('https://www.iddaa.com/program/futbol',
                          timeout=20000, wait_until='networkidle')
                time.sleep(2)

                for sel in ['#CybotCookiebotDialogBodyButtonAccept',
                            'button:has-text("Kabul")', 'button:has-text("Tamam")']:
                    try:
                        page.click(sel, timeout=2000)
                        break
                    except Exception:
                        pass

                import base64
                screenshot_b64 = base64.b64encode(
                    page.screenshot(full_page=False)).decode()

                for sel in ['tr.event-row', 'tr[class*="program"]',
                            '.program-table tbody tr', 'table tbody tr']:
                    rows = page.query_selector_all(sel)
                    if rows:
                        log.info(f"{len(rows)} satır bulundu ({sel})")
                        for row in rows:
                            try:
                                home = away = None
                                for ts in ['td.home-team', 'td[class*="home"]', '.home']:
                                    el = row.query_selector(ts)
                                    if el:
                                        home = el.inner_text().strip()
                                        break
                                for ts in ['td.away-team', 'td[class*="away"]', '.away']:
                                    el = row.query_selector(ts)
                                    if el:
                                        away = el.inner_text().strip()
                                        break
                                if not home or not away:
                                    continue
                                cells = row.query_selector_all('td[class*="odd"], .odd-value')
                                o1 = o0 = o2 = None
                                if len(cells) >= 3:
                                    try: o1 = float(cells[0].inner_text().strip().replace(',', '.'))
                                    except: pass
                                    try: o0 = float(cells[1].inner_text().strip().replace(',', '.'))
                                    except: pass
                                    try: o2 = float(cells[2].inner_text().strip().replace(',', '.'))
                                    except: pass
                                if o1 and o1 > 1.0:
                                    odds_map[f"{home}||{away}"] = {
                                        '1': o1, '0': o0, '2': o2,
                                        'ust': None, 'alt': None,
                                        '_bookmaker': 'iddaa',
                                        '_source': 'playwright',
                                    }
                            except Exception:
                                continue
                        break
        finally:
            browser.close()

    log.info(f"Toplam {len(odds_map)} maç oranı toplandı")
    return odds_map, screenshot_b64


def get_cached_odds(force: bool = False) -> dict:
    with _cache_lock:
        now = datetime.now()
        if (not force and _cache.get('odds') and _cache.get('ts')
                and now - _cache['ts'] < timedelta(minutes=CACHE_TTL)):
            return _cache
        try:
            odds, screenshot = scrape_iddaa()
            _cache.update({'odds': odds, 'screenshot': screenshot,
                           'ts': now, 'count': len(odds), 'error': None})
        except Exception as e:
            log.error(f"Scrape hatası: {e}")
            _cache['error'] = str(e)
            if not _cache.get('odds'):
                _cache['odds'] = {}
        return _cache


def check_auth():
    return (request.headers.get('X-API-Key')
            or request.args.get('key')) == API_SECRET


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


@app.route('/screenshot')
def get_screenshot():
    if not check_auth():
        return jsonify({'error': 'Yetkisiz erişim'}), 401
    cache = get_cached_odds()
    if not cache.get('screenshot'):
        return jsonify({'error': 'Henüz ekran görüntüsü yok'}), 404
    return jsonify({'screenshot_b64': cache['screenshot'],
                    'taken_at': cache['ts'].isoformat() if cache.get('ts') else None})


@app.route('/refresh', methods=['POST'])
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
