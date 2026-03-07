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
    # st (subtype) -> okunabilir isim
    1:   "1X2",
    60:  "Alt/Üst",
    101: "Alt/Üst (HT)",
    # gerekirse genişlet
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
        football = [e for e in events if e.get("sid") == 1]
        parsed = parse_events(football)

        logger.info(f"Fetched {len(parsed)} football events from iddaa")
        return jsonify({"count": len(parsed), "events": parsed})

    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
