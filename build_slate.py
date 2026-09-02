#!/usr/bin/env python3
"""
Strike Zone — Daily Slate Builder
Fetches today's (or a given date's) MLB games + probable pitchers from the
public MLB Stats API, runs each pitcher through the same TBF-based /
recency-weighted / Poisson K-prop model as the web calculator, and writes
a static HTML report you can open in any browser.

Usage:
    pip3 install requests --break-system-packages
    python3 build_slate.py                # today
    python3 build_slate.py 2026-09-05     # specific date

Output:
    slate_report.html  (in the same folder — open it in your browser)
"""

import sys
import math
import json
from datetime import date, datetime
import requests

BASE = "https://statsapi.mlb.com/api/v1"
LEAGUE_AVG_K_PCT = 22.1
BF_PER_IP = 4.3
RECENT_WEIGHT = 0.65


def bb_ip_to_decimal(ip_str):
    """MLB reports IP in baseball notation: '5.1' = 5 1/3, '5.2' = 5 2/3."""
    s = str(ip_str)
    if "." not in s:
        return float(s)
    whole, frac = s.split(".")
    w = float(whole) if whole else 0.0
    if frac == "1":
        return w + 1 / 3
    if frac == "2":
        return w + 2 / 3
    return w + (float("0." + frac) if frac else 0.0)


def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def get_season(season_year):
    return season_year


team_k_cache = {}


def get_team_k_pct(team_id, season):
    if team_id in team_k_cache:
        return team_k_cache[team_id]
    r = requests.get(f"{BASE}/teams/{team_id}/stats",
                      params={"stats": "season", "group": "hitting", "season": season})
    r.raise_for_status()
    data = r.json()
    splits = data.get("stats", [{}])[0].get("splits", [])
    if not splits:
        return None
    stat = splits[0]["stat"]
    so = stat.get("strikeOuts")
    pa = stat.get("plateAppearances")
    if pa is None:
        pa = (float(stat.get("atBats", 0)) + float(stat.get("baseOnBalls", 0)) +
              float(stat.get("hitByPitch", 0)) + float(stat.get("sacFlies", 0)) +
              float(stat.get("sacBunts", 0)))
    if not pa:
        return None
    kpct = (so / pa) * 100
    team_k_cache[team_id] = kpct
    return kpct


def get_pitcher_data(pitcher_id, season):
    r = requests.get(f"{BASE}/people/{pitcher_id}/stats",
                      params={"stats": "season", "group": "pitching", "season": season})
    r.raise_for_status()
    sdata = r.json()
    splits = sdata.get("stats", [{}])[0].get("splits", [])
    if not splits:
        return None
    season_stat = splits[0]["stat"]

    r2 = requests.get(f"{BASE}/people/{pitcher_id}/stats",
                       params={"stats": "gameLog", "group": "pitching", "season": season})
    r2.raise_for_status()
    ldata = r2.json()
    lsplits = ldata.get("stats", [{}])[0].get("splits", [])
    starts = [s for s in lsplits if s["stat"].get("gamesStarted") in (1, "1")]
    starts.sort(key=lambda s: s["date"])
    last5 = starts[-5:]
    last5_parsed = [
        {"k": s["stat"]["strikeOuts"], "ip": bb_ip_to_decimal(s["stat"]["inningsPitched"]), "date": s["date"]}
        for s in last5
    ]
    return {"season": season_stat, "last5": last5_parsed}


def project(season_stat, last5, opp_k_pct, weight=RECENT_WEIGHT, bf_per_ip=BF_PER_IP):
    ip = bb_ip_to_decimal(season_stat["inningsPitched"])
    k = season_stat["strikeOuts"]
    gs = season_stat.get("gamesStarted") or 1
    bb9 = season_stat.get("baseOnBallsPer9")
    bb9 = float(bb9) if bb9 is not None else None

    season_bf = ip * bf_per_ip
    season_k_rate = k / season_bf if season_bf else 0

    recent_k_rate = season_k_rate
    avg_recent_ip = ip / gs
    if len(last5) >= 2:
        n = len(last5)
        wts = [1.4 ** i for i in range(n)]
        num_ = sum(w * s["k"] for w, s in zip(wts, last5))
        den_ = sum(w * (s["ip"] * bf_per_ip) for w, s in zip(wts, last5))
        recent_k_rate = num_ / den_ if den_ else season_k_rate
        avg_recent_ip = sum(s["ip"] for s in last5) / n

    blended = weight * recent_k_rate + (1 - weight) * season_k_rate
    opp_adj = (opp_k_pct / LEAGUE_AVG_K_PCT) if opp_k_pct else 1.0

    control_adj = 1.0
    if bb9 is not None:
        control_adj = 1 - max(0, bb9 - 3.0) * 0.03 + max(0, 2.0 - bb9) * 0.015
        control_adj = max(0.75, min(1.08, control_adj))

    proj_ip = avg_recent_ip * control_adj
    proj_bf = proj_ip * bf_per_ip
    adj_rate = blended * opp_adj
    lam = adj_rate * proj_bf

    lo = hi = 0
    cum = 0.0
    for i in range(40):
        cum += poisson_pmf(i, lam)
        if cum >= 0.10 and lo == 0:
            lo = i
        if cum >= 0.90:
            hi = i
            break

    l5_str = "·".join(str(s["k"]) for s in last5) if last5 else "—"
    l5_vs_season = ((recent_k_rate / season_k_rate - 1) * 100) if season_k_rate else 0

    return {
        "lambda": round(lam, 2), "lo": lo, "hi": hi, "proj_ip": round(proj_ip, 1),
        "season_era": season_stat.get("era"), "season_k9": season_stat.get("strikeoutsPer9Inn"),
        "bb9": bb9, "whip": season_stat.get("whip"),
        "l5_str": l5_str, "l5_vs_season": round(l5_vs_season, 0),
    }


def build_slate(target_date):
    season = target_date.year
    r = requests.get(f"{BASE}/schedule", params={
        "sportId": 1, "date": target_date.isoformat(), "hydrate": "probablePitcher,team"
    })
    r.raise_for_status()
    data = r.json()
    games = data.get("dates", [{}])[0].get("games", [])

    slate = []
    for g in games:
        away, home = g["teams"]["away"], g["teams"]["home"]
        game_time = g.get("gameDate", "")
        try:
            t = datetime.fromisoformat(game_time.replace("Z", "+00:00")).strftime("%I:%M %p UTC")
        except Exception:
            t = ""
        entry = {"away": away["team"]["name"], "home": home["team"]["name"], "time": t, "pitchers": []}

        for side_name, side, opp in (("away", away, home), ("home", home, away)):
            prob = side.get("probablePitcher")
            if not prob:
                entry["pitchers"].append({"side": side_name, "name": None})
                continue
            print(f"  Fetching {prob['fullName']}…")
            try:
                pdata = get_pitcher_data(prob["id"], season)
                opp_kpct = None
                try:
                    opp_kpct = get_team_k_pct(opp["team"]["id"], season)
                except Exception:
                    pass
                if pdata:
                    proj = project(pdata["season"], pdata["last5"], opp_kpct)
                    entry["pitchers"].append({
                        "side": side_name, "name": prob["fullName"],
                        "team": side["team"]["name"], "opp": opp["team"]["name"],
                        "opp_kpct": round(opp_kpct, 1) if opp_kpct else None,
                        **proj
                    })
                else:
                    entry["pitchers"].append({"side": side_name, "name": prob["fullName"], "no_stats": True})
            except Exception as e:
                entry["pitchers"].append({"side": side_name, "name": prob["fullName"], "error": str(e)})

        slate.append(entry)
    return slate


HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Strike Zone — Slate for {date}</title>
<style>
  :root{{--bg:#0b0f14; --panel:#121820; --panel2:#161d27; --border:#233040; --text:#e8edf2; --sub:#8b98a8; --yellow:#facc15; --green:#22c55e;}}
  body{{margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:16px; max-width:640px; margin:0 auto;}}
  h1{{font-size:20px; margin-bottom:4px;}}
  .sub{{color:var(--sub); font-size:13px; margin-bottom:18px;}}
  .gameGroup{{margin-bottom:14px; border:1px solid var(--border); border-radius:12px; overflow:hidden; background:var(--panel);}}
  .gameHead{{background:var(--panel2); padding:10px 14px; font-size:13px; color:var(--sub); display:flex; justify-content:space-between;}}
  .pitcherRow{{display:flex; justify-content:space-between; align-items:center; padding:12px 14px; border-top:1px solid var(--border);}}
  .pName{{font-weight:700; font-size:15px;}}
  .pMeta{{font-size:11px; color:var(--sub); margin-top:2px;}}
  .pProj{{text-align:right;}}
  .pProjNum{{font-size:20px; font-weight:800; color:var(--yellow);}}
  .pProjSub{{font-size:11px; color:var(--sub);}}
  .noPitcher{{padding:12px 14px; color:var(--sub); font-size:12px; font-style:italic;}}
  .footnote{{font-size:11px; color:var(--sub); text-align:center; margin-top:20px; line-height:1.6;}}
</style></head>
<body>
<h1>⚾ Strike Zone — Daily Slate</h1>
<div class="sub">{date} · generated {generated}</div>
{games_html}
<div class="footnote">Projections blend season K-rate/batter-faced with a recency-weighted last-5 rate ({weight}% recent), adjust for opponent K% and BB/9-driven outing length, then use a Poisson distribution for the range. No betting line/odds included — plug those in yourself against the projected mean.</div>
</body></html>
"""

GAME_TEMPLATE = """<div class="gameGroup">
  <div class="gameHead"><span>{away} @ {home}</span><span>{time}</span></div>
  {pitcher_rows}
</div>"""

PITCHER_ROW = """<div class="pitcherRow">
  <div>
    <div class="pName">{name}</div>
    <div class="pMeta">{team} vs {opp} · L5 Ks: {l5_str} ({l5_delta}) · BB/9 {bb9}</div>
  </div>
  <div class="pProj">
    <div class="pProjNum">{lam}</div>
    <div class="pProjSub">{lo}–{hi} range · {proj_ip} IP</div>
  </div>
</div>"""

NO_PITCHER_ROW = """<div class="noPitcher">Probable pitcher not yet announced</div>"""


def render_html(slate, target_date):
    games_html = []
    for g in slate:
        rows = []
        for p in g["pitchers"]:
            if not p.get("name"):
                rows.append(NO_PITCHER_ROW)
            elif p.get("error") or p.get("no_stats"):
                rows.append(f'<div class="noPitcher">{p["name"]}: no stats available</div>')
            else:
                delta = f'{"+" if p["l5_vs_season"]>=0 else ""}{p["l5_vs_season"]:.0f}% vs season'
                rows.append(PITCHER_ROW.format(
                    name=p["name"], team=p["team"], opp=p["opp"],
                    l5_str=p["l5_str"], l5_delta=delta,
                    bb9=p["bb9"] if p["bb9"] is not None else "—",
                    lam=p["lambda"], lo=p["lo"], hi=p["hi"], proj_ip=p["proj_ip"],
                ))
        games_html.append(GAME_TEMPLATE.format(
            away=g["away"], home=g["home"], time=g["time"], pitcher_rows="".join(rows)
        ))
    return HTML_TEMPLATE.format(
        date=target_date.isoformat(), generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        games_html="".join(games_html), weight=int(RECENT_WEIGHT * 100),
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    else:
        target = date.today()

    print(f"Fetching slate for {target.isoformat()}…")
    slate = build_slate(target)
    html = render_html(slate, target)

    out_path = "docs/index.html"
    import os
    os.makedirs("docs", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html)
    print(f"\nDone. {len(slate)} games written to {out_path} — open it in your browser.")

    # also dump raw JSON in case you want to feed it into another tool
    with open("docs/slate_report.json", "w") as f:
        json.dump(slate, f, indent=2, default=str)
