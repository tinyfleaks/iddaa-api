from flask import Flask, jsonify
import requests
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVENTS_URL = "https://sportsbookv2.iddaa.com/sportsbook/events"
HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.iddaa.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# st → market tipi
# st=60 ve st=101 ikisi de tam maç Alt/Üst, sov field'ı hangi hat olduğunu söylüyor
# st=100 → Handikaplı 1X2
# st=1   → Normal 1X2 (t=1: maç öncesi, t=2: canlı)
MARKET_TYPE_MAP = {
    1:   "1X2",
    60:  "AltUst",   # sov=0.5/1.5/2.5/3.5 tam maç
    101: "AltUst",   # sov=1.5/2.5/3.5 tam maç (iddaa bunu da kullanıyor)
    89:  "KGVar",    # Karşılıklı gol
}


def parse_events(raw_events: list) -> list:
    """Ham event listesini temiz formata çevirir."""
    parsed = []
    for ev in raw_events:
        home = ev.get("hn", "")
        away = ev.get("an", "")
        event_id = ev.get("i")
        sport_id = ev.get("sid")

        markets = {}
        # Alt/Üst için sov bazlı ayrım yap
        altust = {}  # sov → {"Alt": x, "Üst": y}

        for m in ev.get("m", []):
            st = m.get("st")
            t = m.get("t", 1)
            sov = m.get("sov")

            # Sadece maç öncesi veya her ikisi de kabul
            if st == 1 and t == 1:
                # Ana 1X2 (maç öncesi)
                odds = {o["n"]: o["odd"] for o in m.get("o", [])}
                markets["1X2"] = odds

            elif st in (60, 101) and sov:
                # Alt/Üst — sov ile ayır
                odds = {o["n"]: o["odd"] for o in m.get("o", [])}
                altust[str(sov)] = odds

            elif st == 89:
                # Karşılıklı gol
                odds = {o["n"]: o["odd"] for o in m.get("o", [])}
                markets["KGVar"] = odds

        markets["AltUst"] = altust  # {"0.5": {...}, "1.5": {...}, "2.5": {...}}

        parsed.append({
            "event_id": event_id,
            "sport_id": sport_id,
            "home": home,
            "away": away,
            "markets": markets,
        })
    return parsed


def build_odds_map(events: list) -> dict:
    """
    Bot'un beklediği odds_map formatına çevirir:
    { "Home vs Away": {"1": x, "0": x, "2": x, "alt": x, "ust": x, ...} }
    """
    odds_map = {}
    for ev in events:
        home = ev.get("home", "")
        away = ev.get("away", "")
        if not home or not away:
            continue

        key = f"{home} vs {away}"
        markets = ev.get("markets", {})

        # 1X2
        h2h = markets.get("1X2", {})
        o1 = h2h.get("1")
        o0 = h2h.get("0")
        o2 = h2h.get("2")

        if not (o1 and o2):
            continue

        # Alt/Üst 2.5 hattı
        altust = markets.get("AltUst", {})
        line_25 = altust.get("2.5", {})
        alt_val = line_25.get("Alt")
        ust_val = line_25.get("Üst")

        # Karşılıklı gol
        kg = markets.get("KGVar", {})
        kg_var = kg.get("Var")
        kg_yok = kg.get("Yok")

        odds_map[key] = {
            "1":           float(o1),
            "0":           float(o0) if o0 else None,
            "2":           float(o2),
            "alt":         float(alt_val) if alt_val else None,
            "ust":         float(ust_val) if ust_val else None,
            "kg_var":      float(kg_var) if kg_var else None,
            "kg_yok":      float(kg_yok) if kg_yok else None,
            "_source":     "📡 iddaa",
            "_bookmaker":  "iddaa",
        }

    return odds_map


def _fetch_football_events():
    resp = requests.get(
        EVENTS_URL,
        headers=HEADERS,
        params={"st": 1, "type": 0, "version": 0},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("isSuccess"):
        raise ValueError("iddaa API başarısız döndü")
    events = data.get("data", {}).get("events", [])
    football = [e for e in events if e.get("sid") == 1]
    return parse_events(football)


@app.route("/odds", methods=["GET"])
def get_odds():
    """Tüm etkinliklerin oranlarını döner (ham format)."""
    try:
        resp = requests.get(EVENTS_URL, headers=HEADERS,
                            params={"st": 1, "type": 0, "version": 0}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("isSuccess"):
            return jsonify({"error": "iddaa API başarısız döndü"}), 502
        events = data.get("data", {}).get("events", [])
        parsed = parse_events(events)
        logger.info(f"Fetched {len(parsed)} events from iddaa")
        return jsonify({"count": len(parsed), "events": parsed})
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/odds/football", methods=["GET"])
def get_football_odds():
    """Sadece futbol etkinliklerini döner (ham format)."""
    try:
        parsed = _fetch_football_events()
        logger.info(f"Fetched {len(parsed)} football events from iddaa")
        return jsonify({"count": len(parsed), "events": parsed})
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/odds/football/bot", methods=["GET"])
def get_football_odds_bot():
    """
    Bot'un kullandığı endpoint — odds_map formatı.
    { "Home vs Away": {"1":x, "0":x, "2":x, "alt":x, "ust":x, ...} }
    """
    try:
        parsed = _fetch_football_events()
        odds_map = build_odds_map(parsed)
        logger.info(f"Returning odds_map with {len(odds_map)} matches")
        return jsonify({"count": len(odds_map), "odds_map": odds_map})
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/debug/sample", methods=["GET"])
def debug_sample():
    """İlk 2 maçın ham verisini döner."""
    try:
        resp = requests.get(EVENTS_URL, headers=HEADERS,
                            params={"st": 1, "type": 0, "version": 0}, timeout=10)
        data = resp.json()
        events = data.get("data", {}).get("events", [])
        football = [e for e in events if e.get("sid") == 1][:2]
        return jsonify({"raw_sample": football})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
