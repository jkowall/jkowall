#!/usr/bin/env python3
"""Render profile README images from the GitHub API and write them to assets/.

The profile used to hotlink shields.io star badges and third-party stats
cards. GitHub's image proxy times out when shields regenerates a star badge,
and the public stats hosts are often down or rate-limited. These SVGs are
plain files in the repo, refreshed by .github/workflows/update-profile-stats.yml.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
USER = "jkowall"
FONT = "Segoe UI, Ubuntu, sans-serif"
BADGE_FONT = "Verdana, Geneva, DejaVu Sans, sans-serif"

# Same order as the Notable Projects section in README.md.
STAR_REPOS = (
    ("jaegertracing", "jaeger", "stars-jaeger.svg"),
    ("opensearch-project", "OpenSearch", "stars-opensearch.svg"),
    ("open-telemetry", "opentelemetry-collector", "stars-opentelemetry-collector.svg"),
    ("jaegertracing", "jaeger-ui", "stars-jaeger-ui.svg"),
)

RANK_LEVELS = ("S", "A+", "A", "A-", "B+", "B", "B-", "C+", "C")
RANK_THRESHOLDS = (1, 12.5, 25, 37.5, 50, 62.5, 75, 87.5, 100)


def token() -> str:
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value.removeprefix("Bearer ").strip()
    completed = subprocess.run(
        ["gh", "auth", "token"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def github_request(url: str, payload: dict | None, auth: str) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {auth}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "jkowall-profile-readme",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    last_error = "unknown error"
    for attempt in range(3):
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode())
            if isinstance(body, dict) and body.get("errors"):
                last_error = json.dumps(body["errors"])[:800]
                # GraphQL validation errors will not succeed on retry.
                if attempt == 2 or "RATE_LIMIT" not in last_error.upper():
                    if "RATE_LIMIT" not in last_error.upper():
                        raise SystemExit(f"GitHub GraphQL error: {last_error}")
            else:
                return body
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:800]
            last_error = f"HTTP {error.code}: {detail}"
            if error.code not in (403, 502, 503, 504) or attempt == 2:
                raise SystemExit(f"GitHub API failed: {last_error}") from error
        except urllib.error.URLError as error:
            last_error = str(error)
            if attempt == 2:
                raise SystemExit(f"GitHub API failed: {last_error}") from error
        time.sleep(2**attempt)
    raise SystemExit(f"GitHub API failed: {last_error}")


def graphql(query: str, auth: str) -> dict:
    body = github_request("https://api.github.com/graphql", {"query": query}, auth)
    if "data" not in body:
        raise SystemExit(f"GitHub GraphQL response missing data: {json.dumps(body)[:800]}")
    return body["data"]


def compact_metric(value: int) -> str:
    """Match shields.io star badges: 1532 -> 1.5k, 13845 -> 14k."""
    if value < 1000:
        return str(value)
    scaled = value / 1000
    suffix = "k"
    if scaled >= 1000:
        scaled /= 1000
        suffix = "M"
    if scaled >= 10:
        return f"{round(scaled)}{suffix}"
    rounded = round(scaled, 1)
    text = f"{rounded:.1f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def format_int(value: int) -> str:
    return f"{value:,}"


def calculate_rank(
    *,
    commits: int,
    prs: int,
    issues: int,
    reviews: int,
    stars: int,
    followers: int,
) -> tuple[str, float]:
    """github-readme-stats rank heuristic (MIT), so the card can keep its grade."""

    def exponential_cdf(x: float) -> float:
        return 1 - 2 ** (-x)

    def log_normal_cdf(x: float) -> float:
        return x / (1 + x)

    commits_median, commits_weight = 250, 2
    prs_median, prs_weight = 50, 3
    issues_median, issues_weight = 25, 1
    reviews_median, reviews_weight = 2, 1
    stars_median, stars_weight = 50, 4
    followers_median, followers_weight = 10, 1
    total_weight = (
        commits_weight
        + prs_weight
        + issues_weight
        + reviews_weight
        + stars_weight
        + followers_weight
    )
    score = (
        commits_weight * exponential_cdf(commits / commits_median)
        + prs_weight * exponential_cdf(prs / prs_median)
        + issues_weight * exponential_cdf(issues / issues_median)
        + reviews_weight * exponential_cdf(reviews / reviews_median)
        + stars_weight * log_normal_cdf(stars / stars_median)
        + followers_weight * log_normal_cdf(followers / followers_median)
    )
    percentile = (1 - score / total_weight) * 100
    level = RANK_LEVELS[-1]
    for threshold, name in zip(RANK_THRESHOLDS, RANK_LEVELS):
        if percentile <= threshold:
            level = name
            break
    return level, percentile


def streaks(counts: dict[str, int], created: date, today: date) -> dict:
    if today < created:
        raise ValueError("today is before the account was created")
    filled: list[tuple[date, int]] = []
    cursor = created
    total = 0
    while cursor <= today:
        count = int(counts.get(cursor.isoformat(), 0))
        filled.append((cursor, count))
        total += count
        cursor += timedelta(days=1)

    def run_ending_at(index: int) -> tuple[int, date | None, date | None]:
        if filled[index][1] <= 0:
            return 0, None, None
        end = filled[index][0]
        start_index = index
        while start_index > 0 and filled[start_index - 1][1] > 0:
            start_index -= 1
        return index - start_index + 1, filled[start_index][0], end

    longest = (0, None, None)
    for index in range(len(filled)):
        if filled[index][1] <= 0:
            continue
        length, start, end = run_ending_at(index)
        if length > longest[0]:
            longest = (length, start, end)

    # Today is still open. A contribution later today should not zero the streak.
    current_index = len(filled) - 1
    if filled[current_index][1] <= 0 and current_index > 0:
        current_index -= 1
    current = run_ending_at(current_index)
    return {
        "total": total,
        "current_days": current[0],
        "current_start": current[1],
        "current_end": current[2],
        "longest_days": longest[0],
        "longest_start": longest[1],
        "longest_end": longest[2],
    }


def esc(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def month_day(value: date, with_year: bool = False) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    label = f"{months[value.month - 1]} {value.day}"
    if with_year:
        return f"{label}, {value.year}"
    return label


def date_span(start: date | None, end: date | None) -> str:
    if start is None or end is None:
        return "—"
    if start.year == end.year:
        return f"{month_day(start)} - {month_day(end)}"
    return f"{month_day(start, True)} - {month_day(end, True)}"


def text_px(value: str) -> int:
    """Approximate Verdana 11px width so badge text stays inside the rect."""
    widths = {
        " ": 4,
        ".": 4,
        "1": 6,
        "k": 6,
        "M": 9,
    }
    total = 0
    for char in value:
        if char in widths:
            total += widths[char]
        elif char.isdigit():
            total += 7
        elif char.isupper():
            total += 8
        else:
            total += 6
    return total


def star_badge(owner: str, repo: str, stars: int) -> str:
    label = "stars"
    value = compact_metric(stars)
    label_w = text_px(label) + 12
    value_w = max(text_px(value) + 12, 28)
    total = label_w + value_w
    title = f"{owner}/{repo}: {format_int(stars)} stars"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img" aria-label="{esc(title)}">
  <title>{esc(title)}</title>
  <g shape-rendering="crispEdges">
    <rect width="{label_w}" height="20" fill="#555555"/>
    <rect x="{label_w}" width="{value_w}" height="20" fill="#007ec6"/>
  </g>
  <g fill="#ffffff" text-anchor="middle" font-family="{BADGE_FONT}" font-size="11">
    <text x="{label_w / 2:.1f}" y="14">{label}</text>
    <text x="{label_w + value_w / 2:.1f}" y="14" font-weight="bold">{esc(value)}</text>
  </g>
</svg>
"""


def safe_color(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return value
    return "#8b949e"


def icon(kind: str, x: int, y: int) -> str:
    origin = f'transform="translate({x} {y})"'
    color = "#bf91f3"
    if kind == "star":
        body = f'<path fill="{color}" d="M8 1.4 9.9 5.3l4.3.6-3.1 3 .7 4.3L8 11.2 4.2 13.2l.7-4.3-3.1-3 4.3-.6L8 1.4z"/>'
    elif kind == "commits":
        body = (
            f'<circle cx="8" cy="8" r="5.4" fill="none" stroke="{color}" stroke-width="1.4"/>'
            f'<path d="M8 4.6V8.2l2.4 1.5" fill="none" stroke="{color}" stroke-width="1.4" stroke-linecap="round"/>'
        )
    elif kind == "prs":
        body = (
            f'<circle cx="4" cy="3.5" r="1.5" fill="{color}"/>'
            f'<circle cx="4" cy="12.5" r="1.5" fill="{color}"/>'
            f'<circle cx="12" cy="8" r="1.5" fill="{color}"/>'
            f'<path d="M4 5v6M4 3.5h3.2A3.3 3.3 0 0 1 10.5 6.8V8" fill="none" stroke="{color}" stroke-width="1.3"/>'
        )
    elif kind == "issues":
        body = (
            f'<circle cx="8" cy="8" r="5.4" fill="none" stroke="{color}" stroke-width="1.4"/>'
            f'<path d="M8 4.7v3.8" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round"/>'
            f'<circle cx="8" cy="11" r="0.8" fill="{color}"/>'
        )
    elif kind == "repos":
        body = (
            f'<path d="M3 2.4h7.2A2.4 2.4 0 0 1 12.6 4.8v8.4H5.2A2.2 2.2 0 0 0 3 15.2V2.4z" fill="none" stroke="{color}" stroke-width="1.3"/>'
            f'<path d="M3 12.8h9.6" fill="none" stroke="{color}" stroke-width="1.3"/>'
        )
    else:
        raise ValueError(kind)
    return f'<g {origin}>{body}</g>'


def stats_card(snapshot: dict) -> str:
    level, percentile = calculate_rank(
        commits=snapshot["commits_last_year"],
        prs=snapshot["pull_requests"],
        issues=snapshot["issues"],
        reviews=snapshot["reviews_last_year"],
        stars=snapshot["stars_earned"],
        followers=snapshot["followers"],
    )
    rows = (
        ("star", "Total Stars Earned:", format_int(snapshot["stars_earned"])),
        ("commits", "Total Commits (last year):", format_int(snapshot["commits_last_year"])),
        ("prs", "Total PRs:", format_int(snapshot["pull_requests"])),
        ("issues", "Total Issues:", format_int(snapshot["issues"])),
        ("repos", "Contributed to (last year):", format_int(snapshot["contributed_to"])),
    )
    description = ", ".join(f"{label} {value}" for _, label, value in rows)
    title = f"{snapshot['name']}'s GitHub Stats"
    radius = 36
    circumference = 2 * math.pi * radius
    dashoffset = circumference * (percentile / 100)
    cx, cy = 432, 108
    row_svg = []
    for index, (kind, label, value) in enumerate(rows):
        y = 58 + index * 25
        row_svg.append(
            "<g>"
            f"{icon(kind, 22, y - 11)}"
            f'<text x="46" y="{y}" fill="#38bdae" font-family="{FONT}" font-size="14" font-weight="600">{esc(label)}</text>'
            f'<text x="292" y="{y}" fill="#38bdae" font-family="{FONT}" font-size="14" font-weight="700">{esc(value)}</text>'
            "</g>"
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="495" height="195" viewBox="0 0 495 195" role="img" aria-label="{esc(title)}. Rank {esc(level)}. {esc(description)}">
  <title>{esc(title)}, Rank: {esc(level)}</title>
  <desc>{esc(description)}</desc>
  <rect x="0.5" y="0.5" width="494" height="194" rx="4.5" fill="#1a1b27" stroke="#e4e2e2" stroke-opacity="0"/>
  <text x="25" y="34" fill="#70a5fd" font-family="{FONT}" font-size="18" font-weight="600">{esc(title)}</text>
  {''.join(row_svg)}
  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="#70a5fd" stroke-opacity="0.25" stroke-width="6"/>
  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="#70a5fd" stroke-width="6" stroke-linecap="round" stroke-dasharray="{circumference:.2f}" stroke-dashoffset="{dashoffset:.2f}" transform="rotate(-90 {cx} {cy})"/>
  <text x="{cx}" y="{cy}" fill="#38bdae" font-family="{FONT}" font-size="24" font-weight="800" text-anchor="middle" dominant-baseline="central">{esc(level)}</text>
</svg>
"""


def languages_card(languages: list[dict]) -> str:
    shown = languages[:6]
    total_bytes = sum(item["bytes"] for item in languages) or 1
    width = 360
    bar_x, bar_y, bar_w, bar_h = 25, 48, 310, 8
    parts = []
    cursor = bar_x
    labels = []
    for index, item in enumerate(shown):
        share = item["bytes"] / total_bytes
        piece = bar_w * share
        parts.append(
            f'<rect x="{cursor:.2f}" y="{bar_y}" width="{max(piece, 0):.2f}" height="{bar_h}" fill="{safe_color(item["color"])}"/>'
        )
        cursor += piece
        column = index // 3
        row = index % 3
        x = 25 + column * 170
        y = 78 + row * 24
        percent = 100 * share
        labels.append(
            "<g>"
            f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{safe_color(item["color"])}"/>'
            f'<text x="{x + 16}" y="{y}" fill="#38bdae" font-family="{FONT}" font-size="12">{esc(item["name"])} {percent:.2f}%</text>'
            "</g>"
        )
    remainder = bar_x + bar_w - cursor
    if remainder > 0.4:
        parts.append(
            f'<rect x="{cursor:.2f}" y="{bar_y}" width="{remainder:.2f}" height="{bar_h}" fill="#3d4158"/>'
        )
    summary = ", ".join(
        f"{item['name']} {100 * item['bytes'] / total_bytes:.2f}%" for item in shown
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="165" viewBox="0 0 {width} 165" role="img" aria-label="Most Used Languages. {esc(summary)}">
  <title>Most Used Languages</title>
  <desc>{esc(summary)}</desc>
  <rect x="0.5" y="0.5" width="{width - 1}" height="164" rx="4.5" fill="#1a1b27" stroke="#e4e2e2" stroke-opacity="0"/>
  <text x="25" y="32" fill="#70a5fd" font-family="{FONT}" font-size="18" font-weight="600">Most Used Languages</text>
  {''.join(parts)}
  {''.join(labels)}
</svg>
"""


def streak_column(center: int, number: str, label: str, span: str, number_color: str, label_color: str) -> str:
    return (
        f'<text x="{center}" y="86" fill="{number_color}" font-family="{FONT}" font-size="28" font-weight="700" text-anchor="middle">{esc(number)}</text>'
        f'<text x="{center}" y="124" fill="{label_color}" font-family="{FONT}" font-size="14" text-anchor="middle">{esc(label)}</text>'
        f'<text x="{center}" y="148" fill="#38bdae" font-family="{FONT}" font-size="12" text-anchor="middle">{esc(span)}</text>'
    )


def streak_card(snapshot: dict, streak: dict) -> str:
    created = date.fromisoformat(snapshot["created"])
    total_span = f"{month_day(created, True)} - Present"
    current_span = date_span(streak["current_start"], streak["current_end"])
    longest_span = date_span(streak["longest_start"], streak["longest_end"])
    description = (
        f"Total Contributions {format_int(streak['total'])} {total_span}. "
        f"Current Streak {streak['current_days']} {current_span}. "
        f"Longest Streak {streak['longest_days']} {longest_span}."
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="495" height="195" viewBox="0 0 495 195" role="img" aria-label="{esc(description)}">
  <title>GitHub Streak</title>
  <desc>{esc(description)}</desc>
  <rect x="0.5" y="0.5" width="494" height="194" rx="4.5" fill="#1a1b27" stroke="#e4e2e2" stroke-opacity="0"/>
  <line x1="165" y1="28" x2="165" y2="168" stroke="#e4e2e2" stroke-opacity="0.35"/>
  <line x1="330" y1="28" x2="330" y2="168" stroke="#e4e2e2" stroke-opacity="0.35"/>
  <circle cx="247.5" cy="78" r="34" fill="none" stroke="#70a5fd" stroke-width="5"/>
  {streak_column(82, format_int(streak["total"]), "Total Contributions", total_span, "#70a5fd", "#70a5fd")}
  {streak_column(247, format_int(streak["current_days"]), "Current Streak", current_span, "#bf91f3", "#bf91f3")}
  {streak_column(412, format_int(streak["longest_days"]), "Longest Streak", longest_span, "#70a5fd", "#70a5fd")}
</svg>
"""


def render_all(snapshot: dict) -> dict[str, str]:
    created = date.fromisoformat(snapshot["created"])
    today = date.fromisoformat(snapshot["today"])
    streak = streaks(snapshot["contributions"], created, today)
    images = {
        item["file"]: star_badge(item["owner"], item["repo"], item["stars"])
        for item in snapshot["repo_stars"]
    }
    images["github-stats.svg"] = stats_card(snapshot)
    images["top-languages.svg"] = languages_card(snapshot["languages"])
    images["github-streak.svg"] = streak_card(snapshot, streak)
    for name, svg in images.items():
        try:
            ET.fromstring(svg)
        except ET.ParseError as error:
            raise SystemExit(f"{name} is not valid SVG: {error}") from error
        if "<style" in svg or "opacity:0" in svg.replace(" ", "") or "<script" in svg:
            raise SystemExit(f"{name} depends on CSS animation or script and may not render")
    return images


def add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def contribution_windows(created: date, today: date) -> list[tuple[date, date]]:
    """Half-open ranges of at most one year, which is the GraphQL limit."""
    end_exclusive = today + timedelta(days=1)
    cursor = created
    windows = []
    while cursor < end_exclusive:
        nxt = min(add_years(cursor, 1), end_exclusive)
        # An exact one-year span is rejected as exceeding one year.
        if nxt - cursor >= timedelta(days=365) and nxt != end_exclusive:
            nxt = cursor + timedelta(days=364)
        elif (nxt - cursor).days > 365:
            nxt = cursor + timedelta(days=365)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows


def fetch_snapshot(auth: str, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    repo_fields = "\n".join(
        f'repo{index}: repository(owner: "{owner}", name: "{repo}") {{ stargazerCount }}'
        for index, (owner, repo, _) in enumerate(STAR_REPOS)
    )
    data = graphql(
        f"""
        query {{
          user(login: "{USER}") {{
            name
            createdAt
            followers {{ totalCount }}
            pullRequests {{ totalCount }}
            issues {{ totalCount }}
            repositoriesContributedTo(contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]) {{
              totalCount
            }}
            contributionsCollection {{
              totalCommitContributions
              totalPullRequestReviewContributions
            }}
            repositories(first: 100, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {{
              totalCount
              nodes {{
                stargazerCount
                languages(first: 20, orderBy: {{field: SIZE, direction: DESC}}) {{
                  edges {{ size node {{ name color }} }}
                }}
              }}
            }}
          }}
          {repo_fields}
        }}
        """,
        auth,
    )
    user = data["user"]
    if user["repositories"]["totalCount"] > 100:
        raise SystemExit("more than 100 public owned repositories; pagination is required")
    created = datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00")).date()
    languages: dict[str, dict] = {}
    stars_earned = 0
    for repo in user["repositories"]["nodes"]:
        stars_earned += repo["stargazerCount"]
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            slot = languages.setdefault(name, {"name": name, "bytes": 0, "color": edge["node"]["color"]})
            slot["bytes"] += edge["size"]
            if not slot["color"] and edge["node"]["color"]:
                slot["color"] = edge["node"]["color"]
    language_rows = sorted(languages.values(), key=lambda item: item["bytes"], reverse=True)

    counts: dict[str, int] = {}
    for start, end in contribution_windows(created, today):
        calendar = graphql(
            f"""
            query {{
              user(login: "{USER}") {{
                contributionsCollection(from: "{start.isoformat()}T00:00:00Z", to: "{end.isoformat()}T00:00:00Z") {{
                  contributionCalendar {{
                    weeks {{
                      contributionDays {{ date contributionCount }}
                    }}
                  }}
                }}
              }}
            }}
            """,
            auth,
        )
        weeks = calendar["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
        for week in weeks:
            for day in week["contributionDays"]:
                day_date = date.fromisoformat(day["date"])
                if day_date < start or day_date >= end or day_date > today:
                    continue
                count = int(day["contributionCount"])
                previous = counts.get(day["date"])
                if previous is not None and previous != count:
                    raise SystemExit(f"conflicting contribution count for {day['date']}")
                counts[day["date"]] = count

    expected = (today - created).days + 1
    if len(counts) != expected:
        raise SystemExit(
            f"contribution calendar has {len(counts)} days, expected {expected} "
            f"from {created.isoformat()} through {today.isoformat()}"
        )

    repo_stars = []
    for index, (owner, repo, filename) in enumerate(STAR_REPOS):
        repo_stars.append(
            {
                "owner": owner,
                "repo": repo,
                "file": filename,
                "stars": int(data[f"repo{index}"]["stargazerCount"]),
            }
        )
    return {
        "name": user["name"] or "Jonah Kowall",
        "created": created.isoformat(),
        "today": today.isoformat(),
        "stars_earned": stars_earned,
        "commits_last_year": int(user["contributionsCollection"]["totalCommitContributions"]),
        "pull_requests": int(user["pullRequests"]["totalCount"]),
        "issues": int(user["issues"]["totalCount"]),
        "contributed_to": int(user["repositoriesContributedTo"]["totalCount"]),
        "reviews_last_year": int(user["contributionsCollection"]["totalPullRequestReviewContributions"]),
        "followers": int(user["followers"]["totalCount"]),
        "repo_stars": repo_stars,
        "languages": language_rows,
        "contributions": counts,
    }


def write_images(images: dict[str, str]) -> None:
    ASSETS.mkdir(exist_ok=True)
    expected = set(images)
    for name, svg in images.items():
        path = ASSETS / name
        text = svg if svg.endswith("\n") else svg + "\n"
        if path.is_file() and path.read_text() == text:
            continue
        path.write_text(text)
    for stale in ASSETS.glob("*.svg"):
        if stale.name not in expected:
            stale.unlink()


def main() -> None:
    snapshot = fetch_snapshot(token())
    images = render_all(snapshot)
    write_images(images)
    created = date.fromisoformat(snapshot["created"])
    today = date.fromisoformat(snapshot["today"])
    streak = streaks(snapshot["contributions"], created, today)
    level, percentile = calculate_rank(
        commits=snapshot["commits_last_year"],
        prs=snapshot["pull_requests"],
        issues=snapshot["issues"],
        reviews=snapshot["reviews_last_year"],
        stars=snapshot["stars_earned"],
        followers=snapshot["followers"],
    )
    print(f"wrote {len(images)} svgs to {ASSETS}")
    print(
        json.dumps(
            {
                "rank": level,
                "rank_percentile": round(percentile, 2),
                "stars_earned": snapshot["stars_earned"],
                "commits_last_year": snapshot["commits_last_year"],
                "pull_requests": snapshot["pull_requests"],
                "issues": snapshot["issues"],
                "contributed_to": snapshot["contributed_to"],
                "streak_total": streak["total"],
                "current_streak": streak["current_days"],
                "longest_streak": streak["longest_days"],
                "repo_stars": {
                    f"{item['owner']}/{item['repo']}": item["stars"] for item in snapshot["repo_stars"]
                },
                "top_languages": [
                    item["name"] for item in snapshot["languages"][:6]
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
