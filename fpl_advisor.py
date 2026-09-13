"""
Personal FPL transfer advisor.
Pulls your squad from the public FPL API, scores every player on form +
upcoming fixture difficulty + availability, flags weak links in your squad,
and suggests replacements within budget. Writes a static HTML report to
./docs/index.html for GitHub Pages to serve.

No login, no API keys, no paid services. Just your public Team ID.
"""

import os
import json
from datetime import datetime, timezone
import requests

TEAM_ID = os.environ.get("FPL_TEAM_ID", "6150125")
LOOKAHEAD_FIXTURES = 3  # how many upcoming fixtures to average difficulty over
BASE = "https://fantasy.premierleague.com/api"

POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def fetch_json(url):
    r = requests.get(url, headers={"User-Agent": "personal-fpl-advisor"}, timeout=20)
    r.raise_for_status()
    return r.json()


def get_current_event(events):
    current = [e for e in events if e.get("is_current")]
    if current:
        return current[0]["id"]
    finished = [e for e in events if e.get("finished")]
    if finished:
        return finished[-1]["id"]
    return events[0]["id"]


def build_fixture_difficulty(fixtures, teams_by_id, from_event):
    """avg upcoming fixture difficulty per team, plus opponent short names for display"""
    upcoming = [f for f in fixtures if f["event"] and f["event"] >= from_event and not f["finished"]]
    upcoming.sort(key=lambda f: f["event"])
    per_team = {}
    for f in upcoming:
        for side, opp_side, is_home in (("team_h", "team_a", True), ("team_a", "team_h", False)):
            tid = f[side]
            diff = f["team_h_difficulty"] if is_home else f["team_a_difficulty"]
            opp_name = teams_by_id[f[opp_side]]["short_name"]
            per_team.setdefault(tid, []).append(
                {"difficulty": diff, "opponent": opp_name, "home": is_home, "event": f["event"]}
            )
    avg_difficulty = {}
    for tid, games in per_team.items():
        games = games[:LOOKAHEAD_FIXTURES]
        avg_difficulty[tid] = {
            "avg": sum(g["difficulty"] for g in games) / len(games) if games else 3,
            "games": games,
        }
    return avg_difficulty


def score_player(p, fixture_info):
    """Higher is better. Combines form, fixture ease, and availability."""
    if p["status"] != "a":
        return -999  # injured / suspended / unavailable
    form = float(p["form"] or 0)
    fdr = fixture_info.get(p["team"], {}).get("avg", 3)
    fixture_ease = (5 - fdr) * 0.6
    ppg = float(p["points_per_game"] or 0)
    return round(form * 1.2 + fixture_ease + ppg * 0.4, 2)


def main():
    bootstrap = fetch_json(f"{BASE}/bootstrap-static/")
    fixtures = fetch_json(f"{BASE}/fixtures/")
    history = fetch_json(f"{BASE}/entry/{TEAM_ID}/history/")

    players = {p["id"]: p for p in bootstrap["elements"]}
    teams_by_id = {t["id"]: t for t in bootstrap["teams"]}
    events = bootstrap["events"]

    current_event = get_current_event(events)
    next_event = current_event + 1
    picks_data = fetch_json(f"{BASE}/entry/{TEAM_ID}/event/{current_event}/picks/")

    fixture_info = build_fixture_difficulty(fixtures, teams_by_id, next_event)

    bank = picks_data["entry_history"]["bank"] / 10.0
    squad_ids = [pk["element"] for pk in picks_data["picks"]]
    squad = [players[pid] for pid in squad_ids]

    for p in squad:
        p["_score"] = score_player(p, fixture_info)
        fi = fixture_info.get(p["team"], {})
        p["_fixtures"] = ", ".join(
            f"{g['opponent']}{'(H)' if g['home'] else '(A)'}" for g in fi.get("games", [])
        )
        p["_avg_fdr"] = fi.get("avg", 3)

    weak_links = sorted(
        [p for p in squad if p["_score"] < 3 or p["status"] != "a"],
        key=lambda p: p["_score"],
    )[:3]

    suggestions = []
    for weak in weak_links:
        budget = (weak["now_cost"] / 10.0) + bank
        position = weak["element_type"]
        candidates = [
            p for p in players.values()
            if p["element_type"] == position
            and p["id"] not in squad_ids
            and p["now_cost"] / 10.0 <= budget
            and p["status"] == "a"
        ]
        for c in candidates:
            c["_score"] = score_player(c, fixture_info)
        candidates.sort(key=lambda p: p["_score"], reverse=True)
        best = candidates[:1]
        if best:
            replacement = best[0]
            reason_out = (
                f"flagged '{weak['news']}'" if weak["status"] != "a" and weak["news"]
                else f"tough run ({weak['_fixtures']})" if weak["_avg_fdr"] >= 4
                else "poor recent form"
            )
            suggestions.append({
                "out": weak["web_name"],
                "out_reason": reason_out,
                "in": replacement["web_name"],
                "in_price": replacement["now_cost"] / 10.0,
                "in_fixtures": fixture_info.get(replacement["team"], {}).get("games", []),
                "in_form": replacement["form"],
            })

    chips_used = history.get("chips", [])

    render_html(squad, weak_links, suggestions, chips_used, bank, current_event)


def render_html(squad, weak_links, suggestions, chips_used, bank, current_event):
    os.makedirs("docs", exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = "".join(
        f"<tr><td>{p['web_name']}</td><td>{POSITION_NAMES[p['element_type']]}</td>"
        f"<td>{p['form']}</td><td>{p['_avg_fdr']:.1f}</td><td>{p['_fixtures']}</td>"
        f"<td>{p['_score']}</td></tr>"
        for p in sorted(squad, key=lambda x: x["_score"])
    )

    sugg_html = "".join(
        f"<li><b>Sell {s['out']}</b> — {s['out_reason']}. "
        f"<b>Buy {s['in']}</b> (£{s['in_price']}m) — form {s['in_form']}, "
        f"fixtures: {', '.join(g['opponent'] + ('(H)' if g['home'] else '(A)') for g in s['in_fixtures'])}.</li>"
        for s in suggestions
    ) or "<li>No clear upgrades found this week — squad looks fine as is.</li>"

    chips_html = "".join(
        f"<li>{c['name']} — used gameweek {c['event']}</li>" for c in chips_used
    ) or "<li>No chips used yet</li>"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>My FPL Advisor</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 16px; color: #222; }}
table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #ddd; }}
h1 {{ font-size: 22px; }} h2 {{ font-size: 18px; margin-top: 32px; }}
.meta {{ color: #777; font-size: 13px; }}
li {{ margin-bottom: 8px; }}
</style></head><body>
<h1>Your FPL squad — gameweek {current_event}</h1>
<p class="meta">Updated {now}. Bank: £{bank}m.</p>

<h2>Suggested transfers this week</h2>
<ul>{sugg_html}</ul>

<h2>Full squad, sorted worst to best</h2>
<table><tr><th>Player</th><th>Pos</th><th>Form</th><th>Avg FDR (next {LOOKAHEAD_FIXTURES})</th><th>Fixtures</th><th>Score</th></tr>
{rows}</table>

<h2>Chips used</h2>
<ul>{chips_html}</ul>
</body></html>"""

    with open("docs/index.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
