"""
Personal FPL dashboard & transfer advisor — deep-dive edition.
Pulls your squad, season stats, and league-wide player data from the public
FPL API, then digs into per-player recent match history (minutes, expected
goals/assists, bonus points) for your squad and any real transfer
candidates, so suggestions are based on underlying performance, not just
FPL's own single 'form' number. Also weighs price/ownership trends and
surfaces real injury/fitness text, not just a binary flag.

No login, no API keys, no paid services. Just your public Team ID.
"""

import os
import time
from datetime import datetime, timezone
import requests

TEAM_ID = os.environ.get("FPL_TEAM_ID", "6150125")
SCORE_LOOKAHEAD = 3
DISPLAY_LOOKAHEAD = 5
FUTURE_WINDOW = 8
DIFFERENTIAL_MAX_OWNED = 10.0
RECENT_GAMES = 4          # matches used for the underlying-performance dig
CANDIDATES_PER_WEAK = 4   # how many alternatives to show per weak link
SHORTLIST_PER_WEAK = 8    # how many quick-scored candidates get the deep dig

BASE = "https://fantasy.premierleague.com/api"
POSITION_NAMES = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
FDR_COLOR = {1: "#3f7d4c", 2: "#5aa66a", 3: "#d9a634", 4: "#c76a3a", 5: "#c1454a"}
POSITION_COLOR = {1: "#d9a634", 2: "#6fb8d1", 3: "#c9c15a", 4: "#c1454a"}  # GKP/DEF/MID/FWD bib colors
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "personal-fpl-advisor"})


def fetch_json(url):
    r = SESSION.get(url, timeout=20)
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
                "event": f["event"], "opponent": teams_by_id[f[opp_side]]["short_name"],
                "difficulty": diff, "home": is_home,
            })
    return per_team


def availability_factor(p):
    """1.0 = fully fit and playing, down to 0.0 = definitely out."""
    chance = p.get("chance_of_playing_next_round")
    if chance is not None:
        return chance / 100.0
    return 1.0 if p["status"] == "a" else 0.0


def quick_score(p, fixture_map, n_fixtures):
    """Cheap first pass using only season-level fields — no extra API calls."""
    games = fixture_map.get(p["team"], [])[:n_fixtures]
    avg_fdr = sum(g["difficulty"] for g in games) / len(games) if games else 3
    form = float(p["form"] or 0)
    ppg = float(p["points_per_game"] or 0)
    base = form * 1.0 + ppg * 0.3 + (5 - avg_fdr) * 0.6
    return round(base * availability_factor(p), 2)


def fetch_underlying(player_id):
    """Recent-match dig: per-90 expected goal involvement + minutes reliability + bonus form."""
    try:
        data = fetch_json(f"{BASE}/element-summary/{player_id}/")
    except requests.RequestException:
        return None
    history = data.get("history", [])[-RECENT_GAMES:]
    if not history:
        return None
    minutes = sum(h["minutes"] for h in history)
    xg = sum(float(h.get("expected_goals", 0) or 0) for h in history)
    xa = sum(float(h.get("expected_assists", 0) or 0) for h in history)
    bps = sum(h.get("bps", 0) for h in history)
    xgi90 = ((xg + xa) / minutes * 90) if minutes > 0 else 0
    avg_minutes = minutes / len(history)
    return {"xgi90": round(xgi90, 2), "avg_minutes": round(avg_minutes), "avg_bps": round(bps / len(history), 1)}


def deep_score(p, fixture_map, n_fixtures, underlying):
    games = fixture_map.get(p["team"], [])[:n_fixtures]
    avg_fdr = sum(g["difficulty"] for g in games) / len(games) if games else 3
    form = float(p["form"] or 0)
    ppg = float(p["points_per_game"] or 0)
    base = form * 1.0 + ppg * 0.3 + (5 - avg_fdr) * 0.6
    if underlying:
        base += underlying["xgi90"] * 4.0
        if underlying["avg_minutes"] < 45:
            base *= 0.6  # rotation-risk / bit-part player, even if stats look fine
    return round(base * availability_factor(p), 2)


def fixture_ticker_html(games):
    chips = "".join(
        f'<span class="fdr-chip" style="background:{FDR_COLOR.get(g["difficulty"], "#999")}">'
        f'{g["opponent"]} {"H" if g["home"] else "A"}</span>' for g in games
    )
    return chips or '<span class="fdr-chip" style="background:#555">no fixture</span>'


def price_trend_text(p):
    week_change = p["cost_change_event"] / 10.0
    season_change = p["cost_change_start"] / 10.0
    net_transfers = p.get("transfers_in_event", 0) - p.get("transfers_out_event", 0)
    direction = "rising" if week_change > 0 else "falling" if week_change < 0 else "steady"
    crowd = "gaining owners" if net_transfers > 0 else "losing owners" if net_transfers < 0 else "flat ownership"
    return f"price {direction} (£{season_change:+.1f}m this season), {crowd}, {p['selected_by_percent']}% owned"


def news_text(p):
    if p.get("news"):
        return p["news"]
    chance = p.get("chance_of_playing_next_round")
    if chance is not None and chance < 100:
        return f"{chance}% chance of playing next round"
    return ""


def build_gw_plan(squad, gw_range, fmap_future, chip_names_used, best_gw, worst_gw):
    """A week-by-week narrative: fixture read, captaincy lean, rotation watch, chip callouts."""
    plan = []
    for gw in gw_range:
        games = []
        for p in squad:
            g = next((x for x in fmap_future.get(p["team"], []) if x["event"] == gw), None)
            if g:
                games.append((p, g))
        blanks = len(squad) - len(games)
        if games:
            games.sort(key=lambda pg: pg[1]["difficulty"])
            easiest_player = games[0][0]["web_name"]
            hardest_player = games[-1][0]["web_name"]
            avg = sum(g["difficulty"] for _, g in games) / len(games)
        else:
            easiest_player = hardest_player = None
            avg = 5
        label = "Easy week" if avg < 2.5 else "Mixed week" if avg < 3.5 else "Tough week"

        chip_note = None
        if gw == best_gw and blanks == 0:
            if "bboost" not in chip_names_used:
                chip_note = "Good spot for Bench Boost"
            elif "3xc" not in chip_names_used:
                chip_note = "Good spot for Triple Captain"
        if gw == worst_gw:
            if "wildcard" not in chip_names_used:
                chip_note = "Consider Wildcard here"
            elif "freehit" not in chip_names_used:
                chip_note = "Consider Free Hit here"

        plan.append({
            "gw": gw, "label": label, "avg": round(avg, 1), "blanks": blanks,
            "captain_tip": easiest_player, "watch_tip": hardest_player, "chip_note": chip_note,
        })
    return plan


def main():
    bootstrap = fetch_json(f"{BASE}/bootstrap-static/")
    fixtures = fetch_json(f"{BASE}/fixtures/")
    history_data = fetch_json(f"{BASE}/entry/{TEAM_ID}/history/")
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

    # Pass 1: cheap season-level score for the entire player pool (fast, no extra calls)
    for p in players.values():
        p["_quick"] = quick_score(p, fmap_score, SCORE_LOOKAHEAD)

    # Deep-dig list: whole squad + shortlisted replacement candidates + differential shortlist
    deep_dig_ids = set(squad_ids)
    weak_links = sorted([p for p in squad if p["_quick"] < 3 or p["status"] != "a"],
                         key=lambda p: p["_quick"])[:3]
    shortlist_by_weak = {}
    for weak in weak_links:
        budget = (weak["now_cost"] / 10.0) + bank
        pool = [p for p in players.values()
                if p["element_type"] == weak["element_type"]
                and p["id"] not in squad_ids
                and p["now_cost"] / 10.0 <= budget
                and p["status"] == "a"]
        pool.sort(key=lambda p: p["_quick"], reverse=True)
        shortlist_by_weak[weak["id"]] = pool[:SHORTLIST_PER_WEAK]
        deep_dig_ids.update(p["id"] for p in pool[:SHORTLIST_PER_WEAK])

    differential_pool = sorted(
        [p for p in players.values() if p["status"] == "a" and float(p["selected_by_percent"]) <= DIFFERENTIAL_MAX_OWNED],
        key=lambda p: p["_quick"], reverse=True,
    )[:15]
    deep_dig_ids.update(p["id"] for p in differential_pool)

    # Pass 2: real dig — recent match history for the shortlisted players only
    underlying_by_id = {}
    for pid in deep_dig_ids:
        underlying_by_id[pid] = fetch_underlying(pid)
        time.sleep(0.15)  # be polite to the API

    for pid in deep_dig_ids:
        p = players[pid]
        p["_score"] = deep_score(p, fmap_score, SCORE_LOOKAHEAD, underlying_by_id.get(pid))
        p["_underlying"] = underlying_by_id.get(pid)

    for p in squad:
        p["_ticker"] = fixture_ticker_html(fmap_display.get(p["team"], []))
        p["_news"] = news_text(p)

    def next_game_score(p):
        games = fmap_score.get(p["team"], [])
        next_diff = games[0]["difficulty"] if games else 3
        u = p.get("_underlying")
        base = float(p["form"] or 0) * 1.0 + (5 - next_diff) * 0.8 + float(p["points_per_game"] or 0) * 0.3
        if u:
            base += u["xgi90"] * 4.0
        return base * availability_factor(p)

    starters = [players[pid] for pid in starters_ids]
    starters_ranked = sorted(starters, key=next_game_score, reverse=True)
    captain, vice = starters_ranked[0], starters_ranked[1]

    suggestions = []
    for weak in weak_links:
        candidates = sorted(shortlist_by_weak[weak["id"]], key=lambda p: p["_score"], reverse=True)
        top = candidates[:CANDIDATES_PER_WEAK]
        reason = (weak["_news"] if weak["_news"] else
                  "tough fixture run and shaky underlying numbers" if weak["_score"] < 1
                  else "underlying stats (minutes/xGI) not backing up the price")
        suggestions.append({
            "out": weak["web_name"], "out_reason": reason, "out_score": weak["_score"],
            "alternatives": [{
                "name": c["web_name"], "price": c["now_cost"] / 10.0,
                "score": c["_score"], "ticker": fixture_ticker_html(fmap_display.get(c["team"], [])),
                "trend": price_trend_text(c),
                "xgi90": c["_underlying"]["xgi90"] if c["_underlying"] else None,
            } for c in top],
        })

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

    differentials = sorted(differential_pool, key=lambda p: p["_score"], reverse=True)[:5]
    chips_used = history_data.get("chips", [])
    chip_names_used = {c["name"] for c in chips_used}

    roadmap = build_gw_plan(squad, gw_range, fmap_future, chip_names_used, best_gw, worst_gw)

    render_html(entry, squad, suggestions, chips_used, bank, current_event,
                captain, vice, gw_avg, best_gw, worst_gw, differentials, roadmap)


def render_html(entry, squad, suggestions, chips_used, bank, current_event,
                 captain, vice, gw_avg, best_gw, worst_gw, differentials, roadmap):
    os.makedirs("docs", exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def underlying_str(p):
        u = p.get("_underlying")
        return f'xGI/90 {u["xgi90"]} · {u["avg_minutes"]} min avg' if u else "no recent data"

    def pos_dot(p):
        return f'<span class="pos-dot" style="background:{POSITION_COLOR[p["element_type"]]}"></span>'

    rows = "".join(
        f'<tr><td>{pos_dot(p)}{p["web_name"]}{" — " + p["_news"] if p["_news"] else ""}</td>'
        f'<td>{POSITION_NAMES[p["element_type"]]}</td><td>£{p["now_cost"]/10:.1f}m</td>'
        f'<td>{p["form"]}</td><td class="dim">{underlying_str(p)}</td>'
        f'<td>{p["_ticker"]}</td><td class="num">{p.get("_score","—")}</td></tr>'
        for p in sorted(squad, key=lambda x: x.get("_score", 0))
    )

    sugg_html = ""
    for s in suggestions:
        alt_chips = "".join(
            f'<div class="alt-chip"><div class="name">{a["name"]}</div>'
            f'<div class="dim">£{a["price"]}m · score {a["score"]} · xGI/90 {a["xgi90"]}</div>'
            f'<div class="dim">{a["trend"]}</div><div>{a["ticker"]}</div></div>'
            for a in s["alternatives"]
        ) or '<div class="dim">No affordable upgrade found</div>'
        sugg_html += (
            f'<div class="transfer-row"><div class="transfer-head">'
            f'<span class="out-name">{s["out"]}</span> <span class="arrow">&#8594;</span> '
            f'<span class="dim">{s["out_reason"]} (score {s["out_score"]})</span></div>'
            f'<div class="alt-chips">{alt_chips}</div></div>'
        )
    sugg_html = sugg_html or '<div class="transfer-row dim">No weak links found — squad looks solid this week.</div>'

    chips_html = "".join(
        f'<div class="chip-row"><span class="chip-name">{c["name"]}</span><span class="dim">gameweek {c["event"]}</span></div>'
        for c in chips_used
    ) or '<div class="dim">No chips used yet</div>'

    diff_html = "".join(
        f'<div class="diff-row"><span class="chip-name">{p["web_name"]}</span> '
        f'<span class="dim">{POSITION_NAMES[p["element_type"]]} · {p["selected_by_percent"]}% owned · '
        f'score {p["_score"]} · {underlying_str(p)} · {price_trend_text(p)}</span></div>'
        for p in differentials
    )

    gw_bars = "".join(
        f'<div class="gw-bar"><span>GW{gw}</span><div class="bar-track"><div class="bar-fill" '
        f'style="width:{(5-d["avg"])/5*100:.0f}%;background:{FDR_COLOR[5] if d["avg"]>=3.5 else FDR_COLOR[3] if d["avg"]>=2.5 else FDR_COLOR[1]}">'
        f'</div></div><span class="dim">{"blanks: "+str(d["blanks"]) if d["blanks"] else round(d["avg"],1)}</span></div>'
        for gw, d in gw_avg.items()
    )

    road_html = ""
    for step in roadmap:
        bits = [f'<span class="road-tag" style="background:{FDR_COLOR[5] if step["avg"]>=3.5 else FDR_COLOR[3] if step["avg"]>=2.5 else FDR_COLOR[1]}">{step["label"]}</span>']
        if step["blanks"]:
            bits.append(f'<span class="dim">{step["blanks"]} blank{"s" if step["blanks"]>1 else ""}</span>')
        if step["captain_tip"]:
            bits.append(f'Captaincy lean: <b>{step["captain_tip"]}</b>')
        if step["watch_tip"] and step["watch_tip"] != step["captain_tip"]:
            bits.append(f'watch <b>{step["watch_tip"]}</b>\'s fixture')
        line = " · ".join(bits)
        chip_html = f'<div class="chip-flag">{step["chip_note"]}</div>' if step["chip_note"] else ""
        road_html += (
            f'<div class="road-step"><div class="road-node">{step["gw"]}</div>'
            f'<div class="road-content"><div class="gw-label">Gameweek {step["gw"]}</div>'
            f'<div class="dim">{line}</div>{chip_html}</div></div>'
        )

    team_name = entry.get("name", "My FPL Squad")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{team_name}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --ink: #0b1710; --turf: #16301f; --turf-2: #1d3d27; --line: #2f5138;
  --chalk: #f2efe4; --dim: #9db3a2; --floodlight: #f4b942; --brick: #d1585a;
}}
* {{ box-sizing: border-box; }}
body {{ font-family: 'Inter', -apple-system, sans-serif; max-width: 880px; margin: 0 auto;
        padding: 20px 16px 70px; background: var(--ink); color: var(--chalk); }}
h1 {{ font-family: 'Oswald', sans-serif; font-weight: 600; letter-spacing: 0.3px; margin: 0; font-size: 30px; }}
.dim {{ color: var(--dim); }}
.num {{ font-variant-numeric: tabular-nums; font-weight: 600; }}

.hero {{ position: relative; overflow: hidden; background: linear-gradient(160deg, var(--turf) 0%, var(--turf-2) 100%);
         border: 1px solid var(--line); border-radius: 16px; padding: 26px 96px 26px 26px; margin-top: 10px; }}
.hero::after {{ content: ""; position: absolute; top: -60px; right: -60px; width: 180px; height: 180px;
                border-radius: 50%; border: 2px solid rgba(242,239,228,0.08); }}
.hero .updated {{ font-size: 13px; margin-top: 6px; }}
.gw-badge {{ position: absolute; top: 22px; right: 22px; width: 58px; height: 58px; border-radius: 50%;
             border: 3px solid var(--floodlight); display: flex; align-items: center; justify-content: center;
             font-family: 'Oswald', sans-serif; font-size: 19px; font-weight: 700; color: var(--floodlight); }}

.ticker {{ display: flex; background: var(--ink); border: 1px solid var(--line); border-radius: 10px;
           overflow: hidden; margin-top: 18px; flex-wrap: wrap; }}
.ticker .seg {{ flex: 1; min-width: 110px; padding: 12px 16px; border-right: 1px solid var(--line); }}
.ticker .seg:last-child {{ border-right: none; }}
.seg .val {{ font-family: 'Oswald', sans-serif; font-size: 22px; font-weight: 600; color: var(--floodlight); }}
.seg .lbl {{ font-size: 12px; color: var(--dim); margin-top: 2px; }}

.divider {{ display: flex; align-items: center; gap: 12px; margin: 38px 0 16px; }}
.divider::before, .divider::after {{ content: ""; flex: 1; height: 1px;
    background-image: linear-gradient(to right, var(--line) 60%, transparent 40%); background-size: 9px 1px; }}
.divider span {{ font-family: 'Oswald', sans-serif; font-size: 15px; font-weight: 500; color: var(--dim); white-space: nowrap; }}

.captain-box {{ display: flex; gap: 14px; }}
.captain-box .card {{ flex: 1; background: var(--turf); border: 1px solid var(--line); border-radius: 12px;
                       padding: 14px 16px; display: flex; align-items: center; gap: 12px; }}
.armband {{ width: 34px; height: 34px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
            font-family: 'Oswald', sans-serif; font-weight: 700; font-size: 13px; background: var(--floodlight); color: var(--ink); flex-shrink: 0; }}
.armband.vc {{ background: var(--dim); }}
.captain-box .name {{ font-size: 16px; font-weight: 600; }}

.transfer-row {{ background: var(--turf); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; }}
.transfer-head {{ font-family: 'Oswald', sans-serif; font-size: 16px; }}
.out-name {{ color: var(--brick); font-weight: 600; }}
.arrow {{ color: var(--dim); }}
.alt-chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
.alt-chip {{ background: var(--turf-2); border: 1px solid var(--line); border-radius: 8px; padding: 9px 11px; font-size: 12.5px; flex: 1; min-width: 170px; }}
.alt-chip .name {{ font-weight: 600; margin-bottom: 3px; }}

table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; padding: 9px 6px; border-bottom: 1px solid var(--line); font-size: 14px; }}
th {{ color: var(--dim); font-weight: 500; font-size: 12px; }}
.pos-dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 8px; }}
.fdr-chip {{ display: inline-block; color: var(--ink); font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 4px; margin: 1px 3px 1px 0; }}

.gw-bar {{ display: grid; grid-template-columns: 44px 1fr 70px; align-items: center; gap: 10px; margin-bottom: 7px; font-size: 13px; }}
.bar-track {{ background: var(--turf-2); border-radius: 4px; height: 9px; overflow: hidden; }}
.bar-fill {{ height: 100%; }}
.callout {{ background: var(--turf); border: 1px solid var(--line); border-radius: 10px; padding: 13px 15px; font-size: 14px; margin-top: 12px; }}

.chip-row, .diff-row {{ padding: 9px 0; border-bottom: 1px solid var(--line); font-size: 14px; }}
.chip-name {{ font-weight: 600; }}

.roadmap {{ position: relative; padding-left: 30px; }}
.roadmap::before {{ content: ""; position: absolute; left: 13px; top: 4px; bottom: 4px; width: 2px; background: var(--line); }}
.road-step {{ position: relative; margin-bottom: 14px; }}
.road-node {{ position: absolute; left: -30px; top: 0; width: 26px; height: 26px; border-radius: 50%;
              background: var(--turf); border: 2px solid var(--floodlight); display: flex; align-items: center;
              justify-content: center; font-family: 'Oswald', sans-serif; font-size: 11px; font-weight: 700; color: var(--floodlight); }}
.road-content {{ background: var(--turf); border: 1px solid var(--line); border-radius: 10px; padding: 11px 14px; }}
.gw-label {{ font-family: 'Oswald', sans-serif; font-weight: 600; font-size: 14px; margin-bottom: 4px; }}
.road-tag {{ display: inline-block; color: var(--ink); font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 4px; margin-right: 6px; }}
.chip-flag {{ display: inline-block; margin-top: 8px; background: var(--floodlight); color: var(--ink);
              font-size: 11.5px; font-weight: 700; padding: 3px 9px; border-radius: 5px; }}
</style></head><body>

<div class="hero">
  <h1>{team_name}</h1>
  <div class="dim updated">Gameweek {current_event}. Last updated {now}. Deep-dive on {len(squad)} squad players plus shortlisted alternatives.</div>
  <div class="gw-badge">GW{current_event}</div>
  <div class="ticker">
    <div class="seg"><div class="val">{entry.get('summary_overall_rank','—'):,}</div><div class="lbl">Overall rank</div></div>
    <div class="seg"><div class="val">{entry.get('summary_overall_points','—')}</div><div class="lbl">Total points</div></div>
    <div class="seg"><div class="val">£{entry.get('last_deadline_value',0)/10:.1f}m</div><div class="lbl">Team value</div></div>
    <div class="seg"><div class="val">£{bank}m</div><div class="lbl">In the bank</div></div>
  </div>
</div>

<div class="divider"><span>Season roadmap</span></div>
<div class="roadmap">
{road_html}
</div>

<div class="divider"><span>Captaincy</span></div>
<div class="captain-box">
  <div class="card"><div class="armband">C</div><div class="name">{captain['web_name']}</div></div>
  <div class="card"><div class="armband vc">VC</div><div class="name">{vice['web_name']}</div></div>
</div>

<div class="divider"><span>Suggested transfers</span></div>
{sugg_html}

<div class="divider"><span>Squad sheet</span></div>
<table><tr><th>Player</th><th>Pos</th><th>Price</th><th>Form</th><th>Underlying (last {RECENT_GAMES})</th><th>Next {DISPLAY_LOOKAHEAD}</th><th>Score</th></tr>
{rows}</table>

<div class="divider"><span>Fixture difficulty at a glance</span></div>
{gw_bars}
<div class="callout">
Best window: GW{best_gw} — squad's easiest average fixtures. Good spot to consider Bench Boost or Triple Captain.<br>
Watch out: GW{worst_gw} — toughest run or blanks piling up. Worth planning a Wildcard or Free Hit around here.
</div>

<div class="divider"><span>Differentials to watch</span></div>
{diff_html}

<div class="divider"><span>Chips used</span></div>
{chips_html}

</body></html>"""

    with open("docs/index.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
