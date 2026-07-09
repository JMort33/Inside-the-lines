"""
Pulls live MLB/soccer odds and scores and writes them into content.json.
Runs automatically via the GitHub Action in .github/workflows/update-content.yml.

Data sources:
- Odds: The Odds API (https://the-odds-api.com) — needs a free API key
- Live scores: ESPN's public scoreboard endpoints — no key required

Note: public betting % isn't available from any free API, so that field
is left blank here. If you find a data source for it later, add it in
build_market_rows() below.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
CONTENT_PATH = "content.json"

ODDS_SPORTS = [
    ("baseball_mlb", "MLB"),
    ("soccer_epl", "EPL"),
]

SCORE_ENDPOINTS = {
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "EPL": "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
}


def get_mlb_target_date():
    # Mirrors the client-side logic: the MLB slate doesn't flip at midnight,
    # it flips at 9:30am Eastern, since most games are still being played
    # right after midnight.
    now_et = datetime.now(ZoneInfo("America/New_York"))
    before_cutover = (now_et.hour < 9) or (now_et.hour == 9 and now_et.minute < 30)
    target = now_et - timedelta(days=1) if before_cutover else now_et
    return target.strftime("%Y%m%d"), target.strftime("%Y-%m-%d")


def fetch_odds(sport_key):
    if not ODDS_API_KEY:
        print("No ODDS_API_KEY set — skipping odds fetch.")
        return []
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h,totals",
        "oddsFormat": "american",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Odds fetch failed for {sport_key}: {e}")
        return []


def build_market_rows(existing_market):
    # Once we've captured a line for a matchup, we freeze it — this fetches
    # fresh odds only for matchups we haven't seen yet (i.e. the opening
    # pregame line), and reuses whatever was already saved for anything
    # already in the file, so the number doesn't drift once a game starts.
    existing_by_matchup = {row.get("matchup"): row for row in (existing_market or [])}

    rows = []
    for sport_key, label in ODDS_SPORTS:
        games = fetch_odds(sport_key)
        for game in games[:5]:
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            matchup = f"{away} @ {home}"

            if matchup in existing_by_matchup:
                # Already have a line locked in for this game — keep it as-is.
                prior = existing_by_matchup[matchup]
                rows.append({
                    "matchup": matchup,
                    "moneyline": prior.get("moneyline", ""),
                    "total": prior.get("total", ""),
                })
                continue

            bookmakers = game.get("bookmakers", [])
            if not bookmakers:
                continue
            book = bookmakers[0]
            moneyline = ""
            total = ""
            for market in book.get("markets", []):
                if market.get("key") == "h2h":
                    outcomes = market.get("outcomes", [])
                    home_price = next((o.get("price") for o in outcomes if o.get("name") == home), None)
                    if home_price is not None:
                        moneyline = f"{home[:3].upper()} {home_price:+d}"
                if market.get("key") == "totals":
                    outcomes = market.get("outcomes", [])
                    point = outcomes[0].get("point") if outcomes else None
                    if point is not None:
                        total = f"O/U {point}"

            rows.append({
                "matchup": matchup,
                "moneyline": moneyline,
                "total": total,
            })
    return rows


def fetch_scores(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Score fetch failed for {url}: {e}")
        return {}


def build_live_scores():
    scores = []
    mlb_date_compact, _ = get_mlb_target_date()
    for league, url in SCORE_ENDPOINTS.items():
        if league == "MLB":
            url = f"{url}?dates={mlb_date_compact}"
        data = fetch_scores(url)
        for event in data.get("events", [])[:8]:
            try:
                comp = event["competitions"][0]
                competitors = comp["competitors"]
                home = next(c for c in competitors if c["homeAway"] == "home")
                away = next(c for c in competitors if c["homeAway"] == "away")
                status = comp["status"]["type"]["shortDetail"]
                scores.append({
                    "league": league,
                    "away": away["team"].get("abbreviation", away["team"].get("shortDisplayName", "")),
                    "home": home["team"].get("abbreviation", home["team"].get("shortDisplayName", "")),
                    "awayScore": away.get("score", ""),
                    "homeScore": home.get("score", ""),
                    "status": status,
                })
            except (KeyError, IndexError, StopIteration):
                continue
    return scores


def archive_final_scores(content, live_scores):
    # ESPN's scoreboard only ever shows the current slate — once it flips to
    # the next day, finished games vanish from that feed for good. This keeps
    # a permanent record so nothing gets lost, just moved into history.
    archive = content.setdefault("scoreArchive", {})
    _, mlb_date_iso = get_mlb_target_date()
    day_bucket = archive.setdefault(mlb_date_iso, [])
    already_recorded = {(g.get("league"), g.get("away"), g.get("home")) for g in day_bucket}

    for game in live_scores:
        if "final" not in game.get("status", "").lower():
            continue
        key = (game.get("league"), game.get("away"), game.get("home"))
        if key in already_recorded:
            continue
        day_bucket.append(game)
        already_recorded.add(key)

    # Keep the last 30 days on hand; older than that just isn't shown anymore.
    cutoff_keys = sorted(archive.keys())
    if len(cutoff_keys) > 30:
        for old_key in cutoff_keys[:-30]:
            del archive[old_key]


TRANSFER_FEED_URL = "https://feeds.bbci.co.uk/sport/football/transfers/rss.xml"

# Rough keyword heuristic to flag a headline as a confirmed move vs a rumor —
# not perfect, but a reasonable first pass. BBC's own wording is the source
# of truth; this just adds a quick visual tag.
CONFIRMED_WORDS = ["signs", "signed", "completes", "completed", "official", "joins", "confirmed", "seals", "agrees deal"]
RUMOR_WORDS = ["linked", "interested", "target", "reportedly", "could", "eyeing", "monitoring", "keen on", "want"]


def fetch_transfer_buzz():
    try:
        r = requests.get(TRANSFER_FEED_URL, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"Transfer feed fetch failed: {e}")
        return None

    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item")[:10]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            lower = title.lower()
            confirmed = any(w in lower for w in CONFIRMED_WORDS) and not any(w in lower for w in RUMOR_WORDS)
            items.append({"title": title, "link": link, "confirmed": confirmed})
        return items
    except Exception as e:
        print(f"Transfer feed parse failed: {e}")
        return None


def main():
    with open(CONTENT_PATH, "r") as f:
        content = json.load(f)

    content["market"] = build_market_rows(content.get("market", [])) or content.get("market", [])
    content["liveScores"] = build_live_scores()
    archive_final_scores(content, content["liveScores"])

    transfer_buzz = fetch_transfer_buzz()
    if transfer_buzz is not None:
        content["transferBuzz"] = transfer_buzz

    content["lastUpdated"] = datetime.now(timezone.utc).isoformat()

    with open(CONTENT_PATH, "w") as f:
        json.dump(content, f, indent=2)

    print(f"Updated {len(content['market'])} market rows, {len(content['liveScores'])} live scores, "
          f"{len(content.get('transferBuzz', []))} transfer items.")


if __name__ == "__main__":
    main()
