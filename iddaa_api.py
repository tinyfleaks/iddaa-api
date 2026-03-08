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
MARKET_TYPE_MAP = {
    1:   "1X2",
    60:  "Alt/Üst",
    101: "Alt/Üst (HT)",
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
        for m in ev.get("m", []):
            st = m.get("st")
            label = MARKET_TYPE_MAP.get(st, f"market_{st}")
            odds = {o["n"]: o["odd"] for o in m.get("o", [])}
            markets[label] = odds
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
        h2h = markets.get("1X2", {})
        totals = markets.get("Alt/Üst", {})

        o1 = h2h.get("1") or h2h.get("MS 1")
        o0 = h2h.get("X") or h2h.get("MS X")
        o2 = h2h.get("2") or h2h.get("MS 2")

        if not (o1 and o2):
            continue

        odds_map[key] = {
            "1":      float(o1),
            "0":      float(o0) if o0 else None,
            "2":      float(o2),
            "alt":    float(totals.get("Alt", totals.get("2.5 Alt", 0))) or None,
            "ust":    float(totals.get("Üst", totals.get("2.5 Üst", 0))) or None,
            "kg_var": None,
            "kg_yok": None,
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
    """Tüm canlı/yaklaşan etkinliklerin oranlarını döner."""
    try:
        resp = requests.get(
            EVENTS_URL,
            headers=HEADERS,
            params={"st": 1, "type": 0, "version": 0},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("isSuccess"):
            return jsonify({"error": "iddaa API başarısız döndü"}), 502
        events = data.get("data", {}).get("events", [])
        parsed = parse_events(events)
        logger.info(f"Fetched {len(parsed)} events from iddaa")
        return jsonify({"count": len(parsed), "events": parsed})
    except requests.exceptions.Timeout:
        logger.error("iddaa API timeout")
        return jsonify({"error": "iddaa API timeout"}), 504
    except requests.exceptions.RequestException as e:
        logger.error(f"iddaa API request error: {e}")
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/odds/football", methods=["GET"])
def get_football_odds():
    """Sadece futbol (sid=1) etkinliklerini döner."""
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
    Bot'un doğrudan kullandığı endpoint.
    odds_map formatında döner: { "Home vs Away": {"1":x, "0":x, "2":x, ...} }
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
    """İlk 2 maçın ham verisini döner - outcome key'lerini görmek için."""
    try:
        resp = requests.get(
            EVENTS_URL,
            headers=HEADERS,
            params={"st": 1, "type": 0, "version": 0},
            timeout=10,
        )
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
