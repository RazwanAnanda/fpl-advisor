"""
Personal FPL dashboard & transfer advisor.
Pulls your squad, season stats, and league-wide player data from the public
FPL API. Scores players on form + fixtures + availability, suggests
transfers, a captain pick, a chip-timing window, and differentials to watch.
Writes a static HTML dashboard to ./docs/index.html for GitHub Pages.

No login, no API keys, no paid services. Just your public Team ID.
"""

import os
from datetime import datetime, timezone
import requests

TEAM_ID = os.environ.get("FPL_TEAM_ID", "6150125")
SCORE_LOOKAHEAD = 3     # fixtures used for near-term scoring
DISPLAY_LOOKAHEAD = 5   # fixtures shown on the ticker
FUTURE_WINDOW = 6       # gameweeks scanned for chip-timing insight
DIFFERENTIAL_MAX_OWNED = 10.0  # % ownership below which a player counts as a differential

BASE = "https://fantasy.premierleague.com/api"
POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
FDR_COLOR = {1: "#0a7d3c", 2: "#4caf50", 3: "#e0a800", 4: "#e2622a", 5: "#c0392b"}


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


def build_fixture_map(fixtures, teams_by_id, from_event, count):
    """Per team: ordered list of upcoming fixtures (event, opponent, difficulty, home)."""
    upcoming = [f for f in fixtures if f["event"] and f["event"] >= from_event and not f["finished"]]
    upcoming.sort(key=lambda f: f["event"])
    per_team = {}
    for f in upcoming:
        for side, opp_side, is_home in (("team_h", "team_a", True), ("team_a", "team_h", False)):
            tid = f[side]
            per_team.setdefault(tid, [])
            if len(per_team[tid]) >= count:
                continue
            diff = f["team_h_difficulty"] if is_home else f["team_a_difficulty"]
            per_team[tid].append({
                "event": f["event"],
                "opponent": teams_by_id[f[opp_side]]["short_name"],
                "difficulty": diff,
                "home": is_home,
            })
    return per_team


def score_player(p, fixture_map, n_fixtures):
    if p["status"] != "a":
        return -999
    games = fixture_map.get(p["team"], [])[:n_fixtures]
    avg_fdr = sum(g["difficulty"] for g in games) / len(games) if games else 3
    form = float(p["form"] or 0)
    ppg = float(p["points_per_game"] or 0)
    return round(form * 1.2 + (5 - avg_fdr) * 0.6 + ppg * 0.4, 2)


def fixture_ticker_html(games):
    chips = ""
    for g in games:
        color = FDR_COLOR.get(g["difficulty"], "#999")
        venue = "H" if g["home"] else "A"
        chips += (f'<span class="fdr-chip" style="background:{color}">'
                  f'{g["opponent"]} {venue}</span>')
    return chips or '<span class="fdr-chip" style="background:#555">no fixture</span>'


def main():
    bootstrap = fetch_json(f"{BASE}/bootstrap-static/")
    fixtures = fetch_json(f"{BASE}/fixtures/")
    history = fetch_json(f"{BASE}/entry/{TEAM_ID}/history/")
    entry = fetch_json(f"{BASE}/entry/{TEAM_ID}/")

    players = {p["id"]: p for p in bootstrap["elements"]}
    teams_by_id = {t["id"]: t for t in bootstrap["teams"]}
    events = bootstrap["events"]

    current_event = get_current_event(events)
    next_event = current_event + 1
    picks_data = fetch_json(f"{BASE}/entry/{TEAM_ID}/event/{current_event}/picks/")

    fmap_score = build_fixture_map(fixtures, teams_by_id, next_event, SCORE_LOOKAHEAD)
    fmap_display = build_fixture_map(fixtures, teams_by_id, next_event, DISPLAY_LOOKAHEAD)
    fmap_future = build_fixture_map(fixtures, teams_by_id, next_event, FUTURE_WINDOW)

    bank = picks_data["entry_history"]["bank"] / 10.0
    squad_ids = [pk["element"] for pk in picks_data["picks"]]
    starters_ids = [pk["element"] for pk in picks_data["picks"] if pk["position"] <= 11]
    squad = [players[pid] for pid in squad_ids]

    for p in squad:
        p["_score"] = score_player(p, fmap_score, SCORE_LOOKAHEAD)
        p["_ticker"] = fixture_ticker_html(fmap_display.get(p["team"], []))

    # --- captain / vice-captain: weight the very next fixture only ---
    def next_game_score(p):
        games = fmap_score.get(p["team"], [])
        next_diff = games[0]["difficulty"] if games else 3
        if p["status"] != "a":
            return -999
        return float(p["form"] or 0) * 1.2 + (5 - next_diff) * 0.8 + float(p["points_per_game"] or 0) * 0.4

    starters = [players[pid] for pid in starters_ids]
    starters_ranked = sorted(starters, key=next_game_score, reverse=True)
    captain, vice = starters_ranked[0], starters_ranked[1]

    # --- weak links & transfer suggestions ---
    weak_links = sorted([p for p in squad if p["_score"] < 3 or p["status"] != "a"],
                         key=lambda p: p["_score"])[:3]
    suggestions = []
    for weak in weak_links:
        budget = (weak["now_cost"] / 10.0) + bank
        candidates = [p for p in players.values()
                      if p["element_type"] == weak["element_type"]
                      and p["id"] not in squad_ids
                      and p["now_cost"] / 10.0 <= budget
                      and p["status"] == "a"]
        for c in candidates:
            c["_score"] = score_player(c, fmap_score, SCORE_LOOKAHEAD)
        candidates.sort(key=lambda p: p["_score"], reverse=True)
        if candidates:
            best = candidates[0]
            reason = (f"flagged: {weak['news']}" if weak["status"] != "a" and weak["news"]
                       else "tough fixture run" if weak["_score"] < 1
                       else "poor recent form")
            suggestions.append({
                "out": weak["web_name"], "out_reason": reason,
                "in": best["web_name"], "in_price": best["now_cost"] / 10.0,
                "in_ticker": fixture_ticker_html(fmap_display.get(best["team"], [])),
                "in_form": best["form"],
            })

    # --- chip timing: average squad fixture difficulty per upcoming gameweek ---
    gw_range = list(range(next_event, next_event + FUTURE_WINDOW))
    gw_avg = {}
    for gw in gw_range:
        diffs, blanks = [], 0
        for p in squad:
            game = next((g for g in fmap_future.get(p["team"], []) if g["event"] == gw), None)
            if game:
                diffs.append(game["difficulty"])
            else:
                blanks += 1
        gw_avg[gw] = {"avg": (sum(diffs) / len(diffs)) if diffs else 5, "blanks": blanks}

    best_gw = min(gw_avg, key=lambda g: (gw_avg[g]["blanks"], gw_avg[g]["avg"]))
    worst_gw = max(gw_avg, key=lambda g: (gw_avg[g]["avg"], gw_avg[g]["blanks"]))

    # --- differentials: low ownership, strong underlying score, any position ---
    all_scored = [p for p in players.values() if p["status"] == "a"]
    for p in all_scored:
        p["_score"] = score_player(p, fmap_score, SCORE_LOOKAHEAD)
    differentials = sorted(
        [p for p in all_scored if float(p["selected_by_percent"]) <= DIFFERENTIAL_MAX_OWNED],
        key=lambda p: p["_score"], reverse=True,
    )[:5]

    chips_used = history.get("chips", [])

    render_html(entry, squad, weak_links, suggestions, chips_used, bank,
                current_event, captain, vice, gw_avg, best_gw, worst_gw, differentials)


def render_html(entry, squad, weak_links, suggestions, chips_used, bank, current_event,
                 captain, vice, gw_avg, best_gw, worst_gw, differentials):
    os.makedirs("docs", exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = "".join(
        f'<tr><td>{p["web_name"]}</td><td>{POSITION_NAMES[p["element_type"]]}</td>'
        f'<td>£{p["now_cost"]/10:.1f}m</td><td>{p["form"]}</td>'
        f'<td>{p["_ticker"]}</td><td><b>{p["_score"]}</b></td></tr>'
        for p in sorted(squad, key=lambda x: x["_score"])
    )

    sugg_html = "".join(
        f'<li><span class="out">OUT {s["out"]}</span> — {s["out_reason"]}<br>'
        f'<span class="in">IN {s["in"]}</span> (£{s["in_price"]}m, form {s["in_form"]}) '
        f'{s["in_ticker"]}</li>'
        for s in suggestions
    ) or "<li>No clear upgrades this week — squad looks fine as is.</li>"

    chips_html = "".join(
        f"<li>{c['name']} — gameweek {c['event']}</li>" for c in chips_used
    ) or "<li>No chips used yet</li>"

    diff_html = "".join(
        f'<li>{p["web_name"]} ({POSITION_NAMES[p["element_type"]]}, '
        f'{p["selected_by_percent"]}% owned) — score {p["_score"]}</li>'
        for p in differentials
    )

    gw_bars = "".join(
        f'<div class="gw-bar"><span>GW{gw}</span>'
        f'<div class="bar-track"><div class="bar-fill" style="width:{(5-d["avg"])/5*100:.0f}%;'
        f'background:{"#4caf50" if d["avg"]<2.5 else "#e0a800" if d["avg"]<3.5 else "#c0392b"}">'
        f'</div></div>{"blanks: "+str(d["blanks"]) if d["blanks"] else d["avg"]}</div>'
        for gw, d in gw_avg.items()
    )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{entry.get('name','My FPL Dashboard')}</title>
<style>
:root {{ color-scheme: dark; }}
body {{ font-family: -apple-system, Segoe UI, sans-serif; max-width: 900px; margin: 0 auto;
        padding: 24px 16px 60px; background: #0f1115; color: #e6e6e6; }}
h1 {{ font-size: 24px; margin-bottom: 4px; }}
h2 {{ font-size: 17px; margin: 36px 0 12px; border-left: 4px solid #4caf50; padding-left: 10px; }}
.meta {{ color: #8a8a8a; font-size: 13px; }}
.stat-row {{ display: flex; gap: 16px; flex-wrap: wrap; margin-top: 16px; }}
.stat-card {{ background: #1a1d24; border-radius: 10px; padding: 14px 18px; flex: 1; min-width: 130px; }}
.stat-card .label {{ font-size: 12px; color: #8a8a8a; }}
.stat-card .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid #262a33; font-size: 14px; }}
th {{ color: #8a8a8a; font-weight: 500; font-size: 12px; text-transform: uppercase; }}
.fdr-chip {{ display: inline-block; color: white; font-size: 11px; padding: 2px 6px;
             border-radius: 4px; margin-right: 4px; }}
.captain-box {{ background: #1a1d24; border-radius: 10px; padding: 16px; display: flex; gap: 24px; }}
.captain-box div {{ flex: 1; }}
.captain-box .tag {{ font-size: 12px; color: #8a8a8a; }}
.captain-box .name {{ font-size: 18px; font-weight: 600; }}
ul {{ padding-left: 0; list-style: none; }}
li {{ background: #1a1d24; border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; font-size: 14px; }}
.out {{ color: #e2622a; font-weight: 600; }}
.in {{ color: #4caf50; font-weight: 600; }}
.gw-bar {{ display: grid; grid-template-columns: 50px 1fr 90px; align-items: center; gap: 10px;
           margin-bottom: 6px; font-size: 13px; }}
.bar-track {{ background: #262a33; border-radius: 4px; height: 10px; overflow: hidden; }}
.bar-fill {{ height: 100%; }}
.callout {{ background: #16261b; border: 1px solid #244; border-radius: 8px; padding: 12px 14px;
            font-size: 14px; margin-top: 10px; }}
</style></head><body>

<h1>{entry.get('name', 'My FPL Dashboard')}</h1>
<p class="meta">Gameweek {current_event} · Updated {now}</p>

<div class="stat-row">
  <div class="stat-card"><div class="label">Overall rank</div><div class="value">{entry.get('summary_overall_rank','—'):,}</div></div>
  <div class="stat-card"><div class="label">Total points</div><div class="value">{entry.get('summary_overall_points','—')}</div></div>
  <div class="stat-card"><div class="label">Team value</div><div class="value">£{entry.get('last_deadline_value',0)/10:.1f}m</div></div>
  <div class="stat-card"><div class="label">Bank</div><div class="value">£{bank}m</div></div>
</div>

<h2>Captain pick this week</h2>
<div class="captain-box">
  <div><div class="tag">Captain</div><div class="name">{captain['web_name']}</div></div>
  <div><div class="tag">Vice-captain</div><div class="name">{vice['web_name']}</div></div>
</div>

<h2>Suggested transfers</h2>
<ul>{sugg_html}</ul>

<h2>Full squad — fixtures &amp; scores</h2>
<table><tr><th>Player</th><th>Pos</th><th>Price</th><th>Form</th><th>Next {DISPLAY_LOOKAHEAD}</th><th>Score</th></tr>
{rows}</table>

<h2>Future path — chip timing</h2>
<div>{gw_bars}</div>
<div class="callout">
Best window: <b>GW{best_gw}</b> — squad's easiest average fixtures. Good spot to consider Bench Boost or Triple Captain.<br>
Watch out: <b>GW{worst_gw}</b> — toughest run or blanks piling up. Worth planning a Wildcard or Free Hit around here.
</div>

<h2>Differentials to watch</h2>
<ul>{diff_html}</ul>

<h2>Chips used</h2>
<ul>{chips_html}</ul>

</body></html>"""

    with open("docs/index.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
