#!/usr/bin/env python3
"""Render the neofetch-style profile card (dark_mode.svg + light_mode.svg).

Content comes from config.json and ascii.txt. Live numbers (repos, stars,
commits, followers, lines of code) come from the GitHub API when a token is
set in GITHUB_TOKEN; otherwise the last values in cache/stats.json are used.

Usage:
    python generate.py            # fetch stats (if token) and render
    python generate.py --offline  # render from cache only
Only the Python standard library is needed.
"""

import calendar
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
CACHE = ROOT / "cache" / "stats.json"
API = "https://api.github.com"

THEMES = {
    "dark_mode.svg": {
        "bg": "#161b22", "text": "#c9d1d9", "key": "#ffa657", "value": "#a5d6ff",
        "add": "#3fb950", "del": "#f85149", "cc": "#616e7f",
    },
    "light_mode.svg": {
        "bg": "#f6f8fa", "text": "#24292f", "key": "#953800", "value": "#0a3069",
        "add": "#1a7f37", "del": "#cf222e", "cc": "#c2cfde",
    },
}

FONT_SIZE = 16
CHAR_W = 0.6 * FONT_SIZE      # every font in the stack renders ~0.6em per glyph
LINE_H = 20
PAD = 15
TOP = 30
GAP = 25


# --------------------------------------------------------------------------- #
# GitHub stats
# --------------------------------------------------------------------------- #

def _request(url, token, body=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-card-generator",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


CONTRIBUTED = """
query($login: String!) {
  user(login: $login) {
    repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) { totalCount }
  }
}
"""


def fetch_profile(login, token, cached):
    _, user = _request(f"{API}/users/{login}", token)
    repos, page = [], 1
    while True:
        _, batch = _request(f"{API}/users/{login}/repos?type=owner&per_page=100&page={page}", token)
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    info = {
        "created_at": user["created_at"],
        "followers": user["followers"],
        "repos": len(repos),
        "stars": sum(r["stargazers_count"] for r in repos),
        "contributed": cached.get("contributed", 0),
    }
    try:   # only available through GraphQL; keep the last value if it fails
        _, out = _request(f"{API}/graphql", token,
                          {"query": CONTRIBUTED, "variables": {"login": login}})
        info["contributed"] = out["data"]["user"]["repositoriesContributedTo"]["totalCount"]
    except Exception as err:
        print(f"  contributed count unavailable ({err})", file=sys.stderr)
    return info, [r["full_name"] for r in repos]


def fetch_code_stats(login, repos, token, cached):
    """Commits / additions / deletions by `login` per repo (GitHub's contributor stats)."""
    result = {}
    pending = list(repos)
    for attempt in range(10):         # first-time stats can take GitHub a few minutes
        still = []
        for repo in pending:
            try:
                status, body = _request(f"{API}/repos/{repo}/stats/contributors", token)
            except urllib.error.HTTPError as err:
                print(f"  {repo}: HTTP {err.code}, keeping cached value", file=sys.stderr)
                continue
            if status == 202:          # GitHub is still computing the stats
                still.append(repo)
                continue
            c = a = d = 0
            for entry in body or []:
                if (entry.get("author") or {}).get("login", "").lower() == login.lower():
                    c = entry["total"]
                    a = sum(w["a"] for w in entry["weeks"])
                    d = sum(w["d"] for w in entry["weeks"])
            result[repo] = {"c": c, "a": a, "d": d}
        pending = still
        if not pending:
            break
        print(f"  waiting for GitHub to compute stats for {len(pending)} repo(s)...")
        time.sleep(min(5 + 3 * attempt, 30))
    for repo in repos:                 # fall back to the cache for anything missing
        if repo not in result and repo in cached:
            result[repo] = cached[repo]
    if pending:
        print(f"  still pending, using cached values: {', '.join(pending)}", file=sys.stderr)
    return result


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {"summary": {}, "repos": {}}


def update_stats(login, offline):
    cache = load_cache()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("ACCESS_TOKEN")
    if offline or not token:
        if not offline:
            print("No GITHUB_TOKEN set, rendering from cache.", file=sys.stderr)
        return cache["summary"]
    try:
        info, repos = fetch_profile(login, token, cache.get("summary", {}))
        per_repo = fetch_code_stats(login, repos, token, cache.get("repos", {}))
    except Exception as err:  # keep yesterday's numbers rather than failing the run
        print(f"GitHub API failed ({err}), rendering from cache.", file=sys.stderr)
        return cache["summary"]
    info["commits"] = sum(v["c"] for v in per_repo.values())
    info["additions"] = sum(v["a"] for v in per_repo.values())
    info["deletions"] = sum(v["d"] for v in per_repo.values())
    info["loc"] = info["additions"] - info["deletions"]
    print("  stats: " + ", ".join(f"{k}={v}" for k, v in info.items()))
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({"summary": info, "repos": per_repo}, indent=2) + "\n",
                     encoding="utf-8")
    return info


# --------------------------------------------------------------------------- #
# Text layout
# --------------------------------------------------------------------------- #

def add_months(day, months):
    y, m = divmod(day.month - 1 + months, 12)
    y, m = day.year + y, m + 1
    return dt.date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def uptime(since, today):
    months = (today.year - since.year) * 12 + today.month - since.month
    if add_months(since, months) > today:
        months -= 1
    d = (today - add_months(since, months)).days
    y, m = divmod(months, 12)
    plural = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
    return f"{plural(y, 'year')}, {plural(m, 'month')}, {plural(d, 'day')}"


def fmt(n):
    return f"{n:,}" if isinstance(n, int) else str(n)


# A line is a list of (text, css_class or None) segments.

def seg_len(segs):
    return sum(len(t) for t, _ in segs)


def kv(key, value_segs, width, lead=". "):
    """`. Key: ....... value` padded with dots to exactly `width` characters."""
    if isinstance(value_segs, str):
        value_segs = [(value_segs, "value")]
    head = [(lead, "cc"), (key, "key"), (":", None)]
    dots = width - seg_len(head) - seg_len(value_segs) - 2
    if dots < 1:
        print(f"warning: '{key}' line is {-dots + 1} chars too long for width {width}",
              file=sys.stderr)
        return head + [(" ", None)] + value_segs
    return head + [(" " + "." * dots + " ", "cc")] + value_segs


def rule(title, width):
    """`title -——————-—-` filled to exactly `width` characters."""
    text = f"{title} -"
    return [(text + "—" * (width - len(text) - 3) + "-—-", None)]


def stats_lines(s, width):
    left_w = width // 2 + 4
    right_w = width - left_w - 3
    repos = kv("Repos", [(fmt(s.get("repos", 0)), "value"), (" {", None),
                         ("Contributed", "key"), (": ", None),
                         (fmt(s.get("contributed", 0)), "value"), ("}", None)], left_w)
    stars = kv("Stars", fmt(s.get("stars", 0)), right_w, lead="")
    commits = kv("Commits", fmt(s.get("commits", 0)), left_w)
    followers = kv("Followers", fmt(s.get("followers", 0)), right_w, lead="")
    sep = [(" | ", None)]
    loc = kv("Lines of Code on GitHub", [
        (fmt(s.get("loc", 0)), "value"), (" ( ", None),
        (fmt(s.get("additions", 0)) + "++", "add"), (", ", None),
        (fmt(s.get("deletions", 0)) + "--", "del"), (" )", None)], width)
    return [repos + sep + stars, commits + sep + followers, loc]


def build_lines(cfg, stats, today):
    width = cfg["width"]
    since = cfg.get("uptime_since") or (stats.get("created_at") or "")[:10]
    up = uptime(dt.date.fromisoformat(since), today) if since else "—"
    lines = [rule(cfg["header"], width)]          # yugal@kishore -————————-—-
    for item in cfg["lines"]:
        if item == "":
            lines.append([(".", "cc")])
        elif item == "{stats}":
            lines += stats_lines(stats, width)
        elif isinstance(item, str) and item.startswith("#"):
            lines.append(rule("- " + item[1:].strip(), width))   # - Contact -————-—-
        else:
            key, value = item
            lines.append(kv(key, value.replace("{uptime}", up), width))
    return lines


# --------------------------------------------------------------------------- #
# SVG
# --------------------------------------------------------------------------- #

def tspan_line(segs, x, y):
    inner = "".join(
        f'<tspan class="{c}">{escape(t)}</tspan>' if c else escape(t) for t, c in segs)
    return f'<tspan x="{x}" y="{y}">{inner}</tspan>'


def render(cfg, lines, art, theme, art_w):
    art_fs = cfg.get("ascii_font_size", FONT_SIZE)
    art_lh = art_fs * LINE_H / FONT_SIZE
    x_info = round(PAD + art_w * 0.6 * art_fs + GAP)
    width = round(x_info + cfg["width"] * CHAR_W + PAD)
    info_h = (len(lines) - 1) * LINE_H
    art_h = (len(art) - 1) * art_lh
    height = round(TOP + max(info_h, art_h) + 20)
    art_y0 = TOP + max(0, (info_h - art_h) / 2)

    art_svg = "".join(tspan_line([(l, None)], PAD, round(art_y0 + i * art_lh, 1))
                      for i, l in enumerate(art))
    info_svg = "".join(tspan_line(segs, x_info, TOP + i * LINE_H) for i, segs in enumerate(lines))
    t = theme
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{width}px" height="{height}px" viewBox="0 0 {width} {height}" font-family="ConsolasFallback, Consolas, Menlo, 'DejaVu Sans Mono', 'Liberation Mono', monospace" font-size="{FONT_SIZE}px">
<style>
@font-face {{ font-family: 'ConsolasFallback'; src: local('Consolas'); size-adjust: 109%; }}
text, tspan {{ white-space: pre; }}
.key {{ fill: {t['key']}; }}
.value {{ fill: {t['value']}; }}
.add {{ fill: {t['add']}; }}
.del {{ fill: {t['del']}; }}
.cc {{ fill: {t['cc']}; }}
</style>
<rect width="{width}px" height="{height}px" fill="{t['bg']}" rx="15"/>
<text xml:space="preserve" fill="{t['text']}" font-size="{art_fs}px">{art_svg}</text>
<text xml:space="preserve" fill="{t['text']}">{info_svg}</text>
</svg>
"""


def main():
    offline = "--offline" in sys.argv
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    stats = update_stats(cfg["username"], offline)
    def read_art(name):
        path = ROOT / name if name else None
        if path and path.exists():
            return path.read_text(encoding="utf-8").rstrip("\n").split("\n")
        return None

    dark_art = read_art(cfg.get("ascii_file", "ascii.txt")) or []
    # Light mode can use its own art (dark pixels -> dense characters).
    light_art = read_art(cfg.get("ascii_file_light")) or dark_art
    art_w = max((len(l) for l in dark_art + light_art), default=0)   # same card size in both themes
    lines = build_lines(cfg, stats, dt.date.today())
    for name, theme in THEMES.items():
        art = light_art if name.startswith("light") else dark_art
        (ROOT / name).write_text(render(cfg, lines, art, theme, art_w), encoding="utf-8")
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
