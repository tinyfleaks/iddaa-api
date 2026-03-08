"""
iddaa_api.py — TrinityBot v3 Production Integration
======================================================
Endpoints:
  GET /health
  GET /odds                     → tüm etkinlikler (ham)
  GET /odds/football            → futbol etkinlikleri (ham)
  GET /odds/football/bot        → bot odds_map (legacy "vs" key, backward-compat)
  GET /odds/football/bot/v2     → bot odds_map v2 ("||" key + metadata + legacy)
  GET /debug/sample             → ilk 2 ham event
"""

from flask import Flask, jsonify
import requests
import logging
import time
import threading
import unicodedata
import copy

# ══════════════════════════════════════════════════════════════
#  SETUP
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

EVENTS_URL = "https://sportsbookv2.iddaa.com/sportsbook/events"
HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.iddaa.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
REQUEST_TIMEOUT = 10    # saniye
CACHE_TTL       = 12    # saniye (iddaa'yı sürekli vurmasın)

# ──────────────────────────────────────────────────────────────
# Market tipi haritası
# ──────────────────────────────────────────────────────────────
MARKET_TYPE_MAP = {
    1:   "1X2",
    60:  "AltUst",   # sov=0.5/1.5/2.5/3.5 tam maç
    101: "AltUst",   # sov=1.5/2.5/3.5 tam maç (iddaa ikisini de kullanıyor)
    89:  "KGVar",    # Karşılıklı gol (Var / Yok)
}

# ══════════════════════════════════════════════════════════════
#  IN-MEMORY CACHE
# ══════════════════════════════════════════════════════════════

_cache: dict = {
    "data":        None,    # parse edilmiş event listesi
    "ts":          0.0,     # son başarılı fetch epoch
    "lock":        threading.Lock(),
}


def _cache_valid() -> bool:
    return (
        _cache["data"] is not None
        and (time.time() - _cache["ts"]) < CACHE_TTL
    )


def _cache_set(data: list) -> None:
    _cache["data"] = data
    _cache["ts"]   = time.time()


def _cache_get() -> list | None:
    return _cache["data"]


# ══════════════════════════════════════════════════════════════
#  YARDIMCI FONKSİYONLAR
# ══════════════════════════════════════════════════════════════

def normalize_outcome_name(name: str) -> str:
    """
    Türkçe karakter farklılıklarını ve yazım varyantlarını normalize et.
    Örnekler:
      "Üst" / "Ust" / "ÜST" / "Over"  → "Üst"
      "Alt"  / "ALT" / "Under"          → "Alt"
      "Var"  / "VAR" / "Yes"            → "Var"
      "Yok"  / "YOK" / "No"             → "Yok"
      "1"/"Ev" → "1",  "0"/"X" → "0",  "2"/"Dep" → "2"
    """
    if not name:
        return name
    n = name.strip()
    # Unicode normalize (é → e gibi)
    n_norm = unicodedata.normalize("NFKC", n).upper()

    # Alt/Üst
    if n_norm in ("ÜST", "UST", "OVER", "OVER 2.5", "O2.5"):
        return "Üst"
    if n_norm in ("ALT", "UNDER", "UNDER 2.5", "U2.5"):
        return "Alt"
    # KG
    if n_norm in ("VAR", "YES", "GG", "BTTS YES"):
        return "Var"
    if n_norm in ("YOK", "NO", "NG", "BTTS NO"):
        return "Yok"
    # 1X2
    if n_norm in ("1", "EV", "HOME"):
        return "1"
    if n_norm in ("0", "X", "DRAW", "BERABERLIK", "BERABERLİK"):
        return "0"
    if n_norm in ("2", "DEP", "AWAY"):
        return "2"
    # Bilinmeyen → orijinali döndür
    return n


def _safe_float(val) -> float | None:
    """Sayısal değeri güvenle float'a çevir (1,85 gibi değerleri de destekler)."""
    try:
        s = str(val).strip().replace(",", ".")
        f = float(s)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


def extract_1x2(market: dict) -> dict:
    """
    Bir market dict'inden 1X2 oranlarını çek.
    Döner: {"1": float|None, "0": float|None, "2": float|None}
    """
    result = {"1": None, "0": None, "2": None}
    for o in market.get("o", []):
        key = normalize_outcome_name(str(o.get("n", "")))
        val = _safe_float(o.get("odd"))
        if key in result:
            result[key] = val
    return result


def extract_totals_25(markets_raw: list) -> dict:
    """
    Ham market listesinden Alt/Üst 2.5 hattını güvenle çek.
    Hem st=60 hem st=101 taranır; sov="2.5" veya sov=2.5 kabul edilir.
    Döner: {"Alt": float|None, "Üst": float|None}
    """
    result = {"Alt": None, "Üst": None}
    for m in markets_raw:
        st  = m.get("st")
        sov = m.get("sov")
        if st not in (60, 101):
            continue
        try:
            sov_f = float(sov) if sov is not None else None
        except (TypeError, ValueError):
            continue
        if sov_f != 2.5:
            continue
        for o in m.get("o", []):
            key = normalize_outcome_name(str(o.get("n", "")))
            val = _safe_float(o.get("odd"))
            if key in result:
                result[key] = val
        # İlk bulunan 2.5 hattı yeterli
        if result["Alt"] or result["Üst"]:
            break
    return result


def extract_btts(market: dict) -> dict:
    """
    KG Var/Yok market dict'inden oranları çek.
    Döner: {"Var": float|None, "Yok": float|None}
    """
    result = {"Var": None, "Yok": None}
    for o in market.get("o", []):
        key = normalize_outcome_name(str(o.get("n", "")))
        val = _safe_float(o.get("odd"))
        if key in result:
            result[key] = val
    return result


# ══════════════════════════════════════════════════════════════
#  PARSE & BUILD
# ══════════════════════════════════════════════════════════════

def parse_events(raw_events: list) -> list:
    """
    Ham event listesini temiz iç formata çevirir.
    parse_error sayacı ile kötü marketleri atla, event'i yıkma.
    Sadece maç öncesi marketleri (t=1) işler — canlı market karışmasını önler.
    """
    parsed     = []
    parse_errs = 0

    for ev in raw_events:
        home     = (ev.get("hn") or "").strip()
        away     = (ev.get("an") or "").strip()
        event_id = ev.get("i") or ev.get("event_id")
        sport_id = ev.get("sid")
        league   = (ev.get("lname") or ev.get("league") or ev.get("cn") or "").strip()
        kickoff  = ev.get("d") or ev.get("date") or ev.get("startDate") or ""

        if not home or not away:
            continue

        markets_raw = ev.get("m", [])
        m1x2   = {}
        altust = {}   # sov → {"Alt": x, "Üst": x}
        mbtts  = {}

        for m in markets_raw:
            st = m.get("st")
            t  = m.get("t", 1)

            # Sadece maç öncesi marketleri al (canlı market karışmasını önler)
            if t != 1:
                continue

            try:
                if st == 1:
                    m1x2 = extract_1x2(m)

                elif st in (60, 101):
                    sov = m.get("sov")
                    try:
                        sov_key = str(float(sov)) if sov is not None else None
                    except (TypeError, ValueError):
                        sov_key = None
                    if sov_key:
                        line = {}
                        for o in m.get("o", []):
                            k = normalize_outcome_name(str(o.get("n", "")))
                            v = _safe_float(o.get("odd"))
                            if k in ("Alt", "Üst"):
                                line[k] = v
                        if line:
                            altust[sov_key] = line

                elif st == 89:
                    mbtts = extract_btts(m)

            except Exception as e:
                parse_errs += 1
                logger.debug(f"Market parse hatası ({home} vs {away}, st={st}): {e}")

        parsed.append({
            "event_id":  event_id,
            "sport_id":  sport_id,
            "home":      home,
            "away":      away,
            "league":    league,
            "kickoff":   kickoff,
            "markets": {
                "1X2":    m1x2,
                "AltUst": altust,
                "KGVar":  mbtts,
            },
        })

    if parse_errs:
        logger.warning(f"  ⚠️  Parse hataları: {parse_errs} market atlandı")

    return parsed


def build_odds_map_v2(events: list) -> tuple[dict, dict]:
    """
    İki format birden üretir:
      odds_map        → "Home||Away" key (yeni standart)
      odds_map_legacy → "Home vs Away" key (backward-compat)

    Her maç kaydına metadata da eklenir:
      event_id, kickoff, last_update_epoch, league_name, _source, _bookmaker

    1X2'de 1 veya 2 yoksa maç atlanır.
    """
    odds_map        = {}
    odds_map_legacy = {}
    skipped         = 0
    now_epoch       = int(time.time())

    for ev in events:
        home = ev.get("home", "")
        away = ev.get("away", "")
        if not home or not away:
            continue

        markets = ev.get("markets", {})
        h2h     = markets.get("1X2", {})
        o1      = h2h.get("1")
        o0      = h2h.get("0")
        o2      = h2h.get("2")

        # 1X2'de 1 veya 2 yoksa atla
        if not o1 or not o2:
            skipped += 1
            continue

        # 2.5 hattını al
        altust  = markets.get("AltUst", {})
        line_25 = altust.get("2.5", {})
        alt_val = line_25.get("Alt")
        ust_val = line_25.get("Üst")

        # KG Var/Yok
        kg      = markets.get("KGVar", {})
        kg_var  = kg.get("Var")
        kg_yok  = kg.get("Yok")

        record = {
            # Oranlar
            "1":          o1,
            "0":          o0,
            "2":          o2,
            "alt":        alt_val,
            "ust":        ust_val,
            "kg_var":     kg_var,
            "kg_yok":     kg_yok,
            # Metadata
            "event_id":         ev.get("event_id"),
            "kickoff":          ev.get("kickoff", ""),
            "last_update_epoch": now_epoch,
            "league_name":      ev.get("league", ""),
            "_source":          "📡 iddaa",
            "_bookmaker":       "iddaa",
            "_lig":             ev.get("league", ""),
        }

        pipe_key   = f"{home}||{away}"
        legacy_key = f"{home} vs {away}"

        odds_map[pipe_key]         = record
        # Legacy map ayrı referans olsun (deepcopy — bağımsız mutasyon)
        odds_map_legacy[legacy_key] = copy.deepcopy(record)

    logger.info(
        f"  odds_map: {len(odds_map)} maç eklendi, {skipped} atlandı (1X2 eksik)"
    )
    return odds_map, odds_map_legacy


# ══════════════════════════════════════════════════════════════
#  FETCH (cache'li)
# ══════════════════════════════════════════════════════════════

def _fetch_football_events(force: bool = False) -> list:
    """
    iddaa API'den futbol eventlerini çek.
    Cache geçerliyse ve force=False ise cache'i döndür.
    Hata olursa son başarılı cache varsa onu döndür, logla.
    """
    with _cache["lock"]:
        if not force and _cache_valid():
            logger.debug("Cache hit — iddaa'ya istek yapılmadı")
            return _cache_get()

    try:
        logger.info("📡 iddaa API'ye istek yapılıyor...")
        resp = requests.get(
            EVENTS_URL,
            headers=HEADERS,
            params={"st": 1, "type": 0, "version": 0},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        # HTML dönerse (bot koruması vb.) erken hata ver
        ct = resp.headers.get("Content-Type", "")
        if "html" in ct.lower():
            raise ValueError(f"HTML yanıt geldi (bot engeli?): Content-Type={ct}")

        data = resp.json()
        if not data.get("isSuccess"):
            raise ValueError("iddaa API isSuccess=False döndü")

        all_events  = data.get("data", {}).get("events", [])
        football    = [e for e in all_events if e.get("sid") == 1]
        parsed      = parse_events(football)

        logger.info(
            f"  Toplam event: {len(all_events)} | "
            f"Futbol: {len(football)} | "
            f"Parse: {len(parsed)}"
        )

        with _cache["lock"]:
            _cache_set(parsed)
        return parsed

    except Exception as e:
        logger.error(f"❌ iddaa fetch hatası: {e}")
        cached = _cache_get()
        if cached is not None:
            age = int(time.time() - _cache["ts"])
            logger.warning(f"⚠️  Eski cache kullanılıyor ({age}s önce alındı)")
            return cached
        raise   # cache de yoksa yeniden fırlat


# ══════════════════════════════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    cache_age = int(time.time() - _cache["ts"]) if _cache["ts"] else None
    return jsonify({
        "status":        "ok",
        "cache_age_sec": cache_age,
        "cache_valid":   _cache_valid(),
    })


@app.route("/odds", methods=["GET"])
def get_odds():
    """Tüm etkinliklerin oranları (ham format, sport filtresi yok)."""
    try:
        resp = requests.get(
            EVENTS_URL, headers=HEADERS,
            params={"st": 1, "type": 0, "version": 0},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data   = resp.json()
        if not data.get("isSuccess"):
            return jsonify({"error": "iddaa API başarısız döndü"}), 502
        events = data.get("data", {}).get("events", [])
        parsed = parse_events(events)
        logger.info(f"/odds → {len(parsed)} event")
        return jsonify({"count": len(parsed), "events": parsed})
    except Exception as e:
        logger.error(f"/odds hata: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/odds/football", methods=["GET"])
def get_football_odds():
    """Sadece futbol etkinlikleri (ham format)."""
    try:
        parsed = _fetch_football_events()
        return jsonify({"count": len(parsed), "events": parsed})
    except Exception as e:
        logger.error(f"/odds/football hata: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/odds/football/bot", methods=["GET"])
def get_football_odds_bot():
    """
    Bot'un kullandığı eski endpoint — BACKWARD COMPATIBLE.
    Artık içeride v2 builder kullanıyor ama yanıt "vs" key ile geliyor.

    Response:
      { "count": N, "odds_map": {"Home vs Away": {...}} }
    """
    try:
        parsed = _fetch_football_events()
        _, odds_map_legacy = build_odds_map_v2(parsed)
        logger.info(f"/odds/football/bot → {len(odds_map_legacy)} maç")
        return jsonify({
            "count":    len(odds_map_legacy),
            "odds_map": odds_map_legacy,   # "vs" key — eski bot uyumu
        })
    except Exception as e:
        logger.error(f"/odds/football/bot hata: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/odds/football/bot/v2", methods=["GET"])
def get_football_odds_bot_v2():
    """
    Bot-optimized v2 endpoint.

    Response:
    {
      "count":           N,
      "generated_at":    epoch,
      "odds_map":        { "Home||Away": { ...oranlar + metadata } },
      "odds_map_legacy": { "Home vs Away": { ...aynı kayıt } }
    }

    Metadata her maç kaydında:
      event_id, kickoff, last_update_epoch, league_name,
      _source="📡 iddaa", _bookmaker="iddaa", _lig
    """
    try:
        parsed = _fetch_football_events()
        odds_map, odds_map_legacy = build_odds_map_v2(parsed)
        return jsonify({
            "count":           len(odds_map),
            "generated_at":    int(time.time()),
            "odds_map":        odds_map,
            "odds_map_legacy": odds_map_legacy,
        })
    except Exception as e:
        logger.error(f"/odds/football/bot/v2 hata: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/debug/sample", methods=["GET"])
def debug_sample():
    """İlk 2 ham futbol event'i döner (geliştirme / debug için)."""
    try:
        resp = requests.get(
            EVENTS_URL, headers=HEADERS,
            params={"st": 1, "type": 0, "version": 0},
            timeout=REQUEST_TIMEOUT,
        )
        data     = resp.json()
        events   = data.get("data", {}).get("events", [])
        football = [e for e in events if e.get("sid") == 1][:2]
        return jsonify({"raw_sample": football, "total_football": len(
            [e for e in events if e.get("sid") == 1]
        )})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 iddaa_api başlıyor — port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
