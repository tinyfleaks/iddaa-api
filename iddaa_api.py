"""
iddaa_api.py — İddaa.com Selenium Scraper API
=============================================
Ayrı bir Railway servisi olarak deploy edilir.
Bot bu servisi çağırarak güncel iddaa oranlarını alır.

Kurulum (Railway):
  - Bu dosyayı ayrı bir repoya koy
  - requirements: flask, selenium, webdriver-manager, requests
  - Environment: API_SECRET=sifreniburayadir
  - Start command: python iddaa_api.py
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta
from threading import Lock
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Ayarlar ──────────────────────────────────────────────────
API_SECRET   = os.environ.get('API_SECRET', 'trinity_secret_key')
CACHE_TTL    = int(os.environ.get('CACHE_TTL_MIN', '30'))  # dakika
PORT         = int(os.environ.get('PORT', 5000))

# ── Önbellek ─────────────────────────────────────────────────
_cache: dict = {}          # { 'odds': {...}, 'ts': datetime, 'count': int }
_cache_lock  = Lock()


# ══════════════════════════════════════════════════════════════
#  SELENIUM SCRAPER
# ══════════════════════════════════════════════════════════════

def scrape_iddaa() -> dict:
    """
    Selenium + Chromium ile iddaa.com'u tara, oranları döndür.
    Dönüş formatı: { "HomeTeam||AwayTeam": {1, X, 2, ust, alt, ...} }
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    opts = Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    opts.add_argument('--window-size=1280,900')
    opts.add_argument('--lang=tr-TR')
    opts.add_experimental_option('excludeSwitches', ['enable-automation'])
    opts.add_experimental_option('useAutomationExtension', False)
    opts.add_argument(
        'user-agent=Mozilla/5.0 (X11; Linux x86_64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    )

    # Chromium binary yolunu bul (Nix/Railway öncelikli)
    chrome_paths = [
        '/nix/var/nix/profiles/default/bin/chromium',
        '/nix/var/nix/profiles/default/bin/chromium-browser',
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/bin/google-chrome',
    ]
    for p in chrome_paths:
        if os.path.exists(p):
            opts.binary_location = p
            log.info(f"Chromium bulundu: {p}")
            break

    # Chromedriver yolunu bul
    chromedriver_paths = [
        '/nix/var/nix/profiles/default/bin/chromedriver',
        '/usr/bin/chromedriver',
        '/usr/local/bin/chromedriver',
    ]
    chromedriver_path = None
    for p in chromedriver_paths:
        if os.path.exists(p):
            chromedriver_path = p
            log.info(f"Chromedriver bulundu: {p}")
            break

    if chromedriver_path:
        service = Service(chromedriver_path)
        driver = webdriver.Chrome(service=service, options=opts)
    else:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=opts)

    odds_map = {}
    screenshot_b64 = None

    try:
        # ── 1. Sayfaya git ──────────────────────────────────
        log.info("İddaa.com açılıyor...")
        driver.get('https://www.iddaa.com/program/futbol')
        time.sleep(3)

        # Çerez popup'ını kapat
        for sel in ['#CybotCookiebotDialogBodyButtonAccept',
                    'button[id*="accept"]', '.cookie-accept', '#onetrust-accept-btn-handler']:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                btn.click()
                time.sleep(1)
                break
            except Exception:
                pass

        # ── 2. Oranların yüklenmesini bekle ─────────────────
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'table.program-table, .event-row, [class*="match"], [class*="event"]')
                )
            )
        except Exception:
            log.warning("Tablo yüklenme timeout — devam ediliyor")

        time.sleep(2)

        # ── 3. Ekran görüntüsü al ────────────────────────────
        import base64
        screenshot_b64 = base64.b64encode(driver.get_screenshot_as_png()).decode()
        log.info("Ekran görüntüsü alındı")

        # ── 4. Önce JavaScript API'sini dene ────────────────
        try:
            js_result = driver.execute_script("""
                const keys = Object.keys(window).filter(k =>
                    k.includes('match') || k.includes('event') ||
                    k.includes('odds') || k.includes('program'));
                return keys.slice(0, 20);
            """)
            log.info(f"JS window keys: {js_result[:5]}")
        except Exception:
            pass

        # ── 5. DOM'dan satır satır çek ───────────────────────
        selectors_to_try = [
            'tr.event-row',
            'tr[class*="program"]',
            'div[class*="event-row"]',
            'div[class*="match-row"]',
            '.program-table tbody tr',
            'table tbody tr',
        ]

        rows = []
        for sel in selectors_to_try:
            rows = driver.find_elements(By.CSS_SELECTOR, sel)
            if rows:
                log.info(f"Satır bulundu: {len(rows)} adet ({sel})")
                break

        # ── 6. Satırları parse et ────────────────────────────
        for row in rows:
            try:
                text = row.text.strip()
                if not text or len(text) < 10:
                    continue

                # Takım adları
                home = away = None
                for team_sel in ['td.home-team', 'td[class*="home"]',
                                  'span[class*="home"]', '.home', '[data-home]']:
                    try:
                        home = row.find_element(By.CSS_SELECTOR, team_sel).text.strip()
                        break
                    except Exception:
                        pass

                for team_sel in ['td.away-team', 'td[class*="away"]',
                                  'span[class*="away"]', '.away', '[data-away]']:
                    try:
                        away = row.find_element(By.CSS_SELECTOR, team_sel).text.strip()
                        break
                    except Exception:
                        pass

                # Takım adı bulunamadıysa veri niteliğinden dene
                if not home:
                    home = row.get_attribute('data-home-team') or ''
                if not away:
                    away = row.get_attribute('data-away-team') or ''

                if not home or not away:
                    continue

                # Oranlar
                o1 = o0 = o2 = ust = alt = None
                odds_cells = row.find_elements(By.CSS_SELECTOR,
                    'td[class*="odd"], td[class*="rate"], td[class*="oran"], .odd-value')

                if len(odds_cells) >= 3:
                    try: o1  = float(odds_cells[0].text.strip().replace(',', '.'))
                    except: pass
                    try: o0  = float(odds_cells[1].text.strip().replace(',', '.'))
                    except: pass
                    try: o2  = float(odds_cells[2].text.strip().replace(',', '.'))
                    except: pass
                if len(odds_cells) >= 5:
                    try: ust = float(odds_cells[3].text.strip().replace(',', '.'))
                    except: pass
                    try: alt = float(odds_cells[4].text.strip().replace(',', '.'))
                    except: pass

                if o1 and o1 > 1.0:
                    key = f"{home}||{away}"
                    odds_map[key] = {
                        '1':   o1,
                        '0':   o0,
                        '2':   o2,
                        'ust': ust,
                        'alt': alt,
                        '_bookmaker': 'iddaa',
                        '_source':    'selenium',
                    }

            except Exception as row_err:
                log.debug(f"Satır parse hatası: {row_err}")
                continue

        # ── 7. DOM başarısızsa mobil JSON API'yi dene ────────
        if not odds_map:
            log.info("DOM boş — mobil API deneniyor...")
            driver.get('https://sportprogram.iddaa.com/api/sportprogram/programs?sportId=1&programType=0')
            time.sleep(2)
            try:
                body = driver.find_element(By.TAG_NAME, 'pre').text
                data = json.loads(body)
                events = (data.get('data', {}).get('events', [])
                          or data.get('events', []) or [])
                for ev in events:
                    home = ev.get('homeTeamName', '') or ev.get('home', '')
                    away = ev.get('awayTeamName', '') or ev.get('away', '')
                    if not home or not away:
                        continue
                    o1 = o0 = o2 = None
                    for out in ev.get('outcomes', ev.get('odds', [])):
                        t  = str(out.get('outcomeName', out.get('name', '')))
                        pr = out.get('odd', out.get('price'))
                        if pr:
                            if t == '1': o1 = float(pr)
                            elif t == 'X': o0 = float(pr)
                            elif t == '2': o2 = float(pr)
                    if o1:
                        key = f"{home}||{away}"
                        odds_map[key] = {
                            '1': o1, '0': o0, '2': o2,
                            'ust': None, 'alt': None,
                            '_bookmaker': 'iddaa',
                            '_source': 'iddaa_json_api',
                        }
                log.info(f"Mobil API: {len(odds_map)} maç")
            except Exception as je:
                log.warning(f"Mobil API parse hatası: {je}")

    finally:
        driver.quit()

    log.info(f"Toplam {len(odds_map)} maç oranı toplandı")
    return odds_map, screenshot_b64


# ══════════════════════════════════════════════════════════════
#  ÖNBELLEK YÖNETİMİ
# ══════════════════════════════════════════════════════════════

def get_cached_odds(force: bool = False) -> dict:
    """Önbellekte varsa döndür, yoksa veya süresi geçtiyse yenile."""
    with _cache_lock:
        now = datetime.now()
        if (not force
                and _cache.get('odds')
                and _cache.get('ts')
                and now - _cache['ts'] < timedelta(minutes=CACHE_TTL)):
            age = int((now - _cache['ts']).total_seconds() / 60)
            log.info(f"Önbellekten döndürüldü ({age} dk önce çekildi)")
            return _cache

        log.info("Yeni veri çekiliyor...")
        try:
            odds, screenshot = scrape_iddaa()
            _cache['odds']       = odds
            _cache['screenshot'] = screenshot
            _cache['ts']         = now
            _cache['count']      = len(odds)
            _cache['error']      = None
        except Exception as e:
            log.error(f"Scrape hatası: {e}")
            _cache['error'] = str(e)
            if not _cache.get('odds'):
                _cache['odds'] = {}

        return _cache


# ══════════════════════════════════════════════════════════════
#  AUTH MIDDLEWARE
# ══════════════════════════════════════════════════════════════

def check_auth():
    """API_SECRET kontrolü (header veya query param)."""
    secret = (request.headers.get('X-API-Key')
              or request.args.get('key')
              or request.json.get('key') if request.is_json else None)
    return secret == API_SECRET


# ══════════════════════════════════════════════════════════════
#  API ENDPOINTLERİ
# ══════════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    """Sağlık kontrolü — auth gerektirmez."""
    return jsonify({
        'status':  'ok',
        'service': 'iddaa-scraper-api',
        'time':    datetime.now().isoformat(),
        'cached':  bool(_cache.get('odds')),
        'count':   _cache.get('count', 0),
    })


@app.route('/odds', methods=['GET'])
def get_odds():
    """
    Tüm maçların oranlarını döndür.
    GET /odds?key=API_SECRET&force=1
    """
    if not check_auth():
        return jsonify({'error': 'Yetkisiz erişim'}), 401

    force = request.args.get('force', '0') == '1'
    cache = get_cached_odds(force=force)

    if cache.get('error') and not cache.get('odds'):
        return jsonify({'error': cache['error'], 'odds': {}}), 500

    return jsonify({
        'odds':       cache.get('odds', {}),
        'count':      cache.get('count', 0),
        'cached_at':  cache['ts'].isoformat() if cache.get('ts') else None,
        'cache_ttl':  CACHE_TTL,
        'error':      cache.get('error'),
    })


@app.route('/odds/<path:match_key>', methods=['GET'])
def get_match_odds(match_key):
    """
    Belirli maçın oranını döndür.
    GET /odds/Galatasaray||Fenerbahce?key=API_SECRET
    """
    if not check_auth():
        return jsonify({'error': 'Yetkisiz erişim'}), 401

    cache = get_cached_odds()
    odds  = cache.get('odds', {})

    # Direkt eşleşme
    if match_key in odds:
        return jsonify({'match': match_key, 'odds': odds[match_key]})

    # Kısmi eşleşme (büyük/küçük harf + kısmi isim)
    key_lower = match_key.lower()
    for k, v in odds.items():
        if key_lower in k.lower() or k.lower() in key_lower:
            return jsonify({'match': k, 'odds': v})

    return jsonify({'match': match_key, 'odds': None, 'error': 'Maç bulunamadı'}), 404


@app.route('/screenshot', methods=['GET'])
def get_screenshot():
    """
    Son çekilen ekran görüntüsünü base64 döndür.
    GET /screenshot?key=API_SECRET
    """
    if not check_auth():
        return jsonify({'error': 'Yetkisiz erişim'}), 401

    cache = get_cached_odds()
    screenshot = cache.get('screenshot')

    if not screenshot:
        return jsonify({'error': 'Henüz ekran görüntüsü yok'}), 404

    return jsonify({
        'screenshot_b64': screenshot,
        'taken_at': cache['ts'].isoformat() if cache.get('ts') else None,
    })


@app.route('/refresh', methods=['POST'])
def force_refresh():
    """
    Önbelleği zorla yenile.
    POST /refresh  (body: {"key": "API_SECRET"})
    """
    if not check_auth():
        return jsonify({'error': 'Yetkisiz erişim'}), 401

    cache = get_cached_odds(force=True)
    return jsonify({
        'status':  'refreshed',
        'count':   cache.get('count', 0),
        'error':   cache.get('error'),
    })


# ══════════════════════════════════════════════════════════════
#  BAŞLATMA
# ══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    log.info(f"İddaa API başlıyor — port {PORT}, cache {CACHE_TTL} dk")
    # İlk yüklemeyi arka planda başlat
    import threading
    threading.Thread(target=get_cached_odds, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False)
  
