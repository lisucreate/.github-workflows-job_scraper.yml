"""
Job scraper - checks job sources for technical writing / documentation /
knowledge roles open to Canada, alerts a Discord channel about new matches, and
writes a results file so every run is visible.

Sources
  * Himalayas (free public API, remote jobs)  -> works from GitHub Actions.
  * Dice, Indeed, ZipRecruiter, Hiring Cafe, Upwork are NOT scraped here: they
    block automated requests or have no public feed. See README notes in the
    repo / ask Claude to search them for you on a schedule instead.

Outputs
  * Discord alert per NEW match (needs the DISCORD_WEBHOOK_URL secret)
  * latest_results.md   every current match, newest first (committed by the workflow)
  * GitHub run summary  counts + matches, shown at the top of each Actions run
  * seen_jobs.json      ids already alerted, so nothing is sent twice
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

# --- CONFIGURATION -----------------------------------------------------------
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
SUMMARY_FILE = os.getenv("GITHUB_STEP_SUMMARY")
SEEN_JOBS_FILE = "seen_jobs.json"
RESULTS_FILE = "latest_results.md"
MAX_ALERTS_PER_RUN = 25  # avoids flooding Discord on the first run; the rest go next run
HEADERS = {"User-Agent": "job-scraper/2.0 (personal job search)"}

HIMALAYAS_SEARCH_URL = "https://himalayas.app/jobs/api/search"
SEARCH_QUERIES = [
    "technical writer",
    "documentation",
    "knowledge management",
    "content strategist",
    "information architect",
    "taxonomy",
    "product knowledge",
    "content designer",
    "technical author",
]
PAGES_PER_QUERY = 2  # Himalayas returns 20 jobs per page

# A posting's TITLE must match one of these...
TARGET_ROLES_REGEX = re.compile(
    r"("
    r"technical\s+(?:[\w&/-]+\s+)?writer|technical\s+content\s+writer|content\s+engineer|"
    r"information\s+author|information\s+developer|documentation\s+engineer|"
    r"technical\s+content\s+engineer|technical\s+information\s+author|technical\s+author|"
    r"documentation\s+(?:specialist|manager|lead|writer|analyst|developer)|"
    r"technical\s+communicator|api\s+writer|programmer\s+writer|docs?\s+engineer|"
    r"developer\s+content\s+writer|knowledge\s+base\s+specialist|content\s+designer|"
    r"information\s+architect|technical\s+editor|developer\s+educator|"
    r"knowledge\s+management|knowledge\s+(?:specialist|engineer|manager|operations)|"
    r"product\s+knowledge|content\s+strateg(?:ist|y)|content\s+architect|"
    r"content\s+operations|taxonom(?:y|ist)|metadata\s+specialist|"
    r"structured\s+authoring|\bDITA\b|\bCCMS\b|ai\s+content|ai\s+documentation"
    r")",
    re.IGNORECASE,
)

# ...and must NOT match any of these.
EXCLUDE_TITLE_REGEX = re.compile(
    r"\b(nurse|clinical|sales|librarian|warehouse|intern|recruiter|driver|"
    r"pharmacist|physician|cashier|proposal|grant\s+writer|copywriter|seo)\b",
    re.IGNORECASE,
)


# --- STATE -------------------------------------------------------------------
def load_seen_jobs():
    if os.path.exists(SEEN_JOBS_FILE):
        try:
            with open(SEEN_JOBS_FILE, "r") as f:
                return set(json.load(f))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen_jobs(seen):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=1)


# --- SOURCES -----------------------------------------------------------------
def _salary_text(j):
    lo, hi, cur = j.get("minSalary"), j.get("maxSalary"), j.get("currency") or ""
    if lo and hi:
        return f"{cur} {int(lo):,}-{int(hi):,} {j.get('salaryPeriod') or ''}".strip()
    return ""


def fetch_himalayas(stats):
    """Himalayas public API: remote jobs open to Canada (or worldwide)."""
    jobs = []
    for query in SEARCH_QUERIES:
        for page in range(1, PAGES_PER_QUERY + 1):
            try:
                resp = requests.get(
                    HIMALAYAS_SEARCH_URL,
                    params={"q": query, "country": "Canada", "sort": "recent", "page": page},
                    headers=HEADERS,
                    timeout=30,
                )
            except requests.RequestException as exc:
                print(f"[himalayas] '{query}' page {page}: request failed ({exc})")
                stats["errors"] += 1
                break
            stats["requests"] += 1
            if resp.status_code == 429:
                print("[himalayas] rate limited (429); waiting before continuing")
                stats["errors"] += 1
                time.sleep(10)
                break
            if resp.status_code != 200:
                print(f"[himalayas] '{query}' page {page}: HTTP {resp.status_code}")
                stats["errors"] += 1
                break
            batch = resp.json().get("jobs", [])
            for j in batch:
                restrictions = j.get("locationRestrictions") or []
                posted = j.get("pubDate")
                jobs.append(
                    {
                        "id": "himalayas:" + str(j.get("guid") or j.get("applicationLink")),
                        "title": (j.get("title") or "").strip(),
                        "company": j.get("companyName") or "N/A",
                        "location": ", ".join(restrictions) + " (remote)"
                        if restrictions
                        else "Worldwide (remote)",
                        "link": j.get("applicationLink") or "",
                        "type": j.get("employmentType") or "",
                        "salary": _salary_text(j),
                        "posted": datetime.fromtimestamp(posted, tz=timezone.utc).date().isoformat()
                        if isinstance(posted, (int, float))
                        else "",
                        "source": "Himalayas",
                    }
                )
            if len(batch) < 20:
                break
            time.sleep(1)
        time.sleep(1)
    return jobs


SOURCES = [("Himalayas", fetch_himalayas)]  # add more fetchers here


# --- FILTER / OUTPUT ---------------------------------------------------------
def select_matches(all_jobs):
    seen_ids, matches = set(), []
    for job in all_jobs:
        if job["id"] in seen_ids or not job["title"]:
            continue
        seen_ids.add(job["id"])
        if TARGET_ROLES_REGEX.search(job["title"]) and not EXCLUDE_TITLE_REGEX.search(job["title"]):
            matches.append(job)
    matches.sort(key=lambda j: j["posted"], reverse=True)
    return matches


def send_discord_alert(job):
    if not DISCORD_WEBHOOK_URL:
        return False
    fields = [
        {"name": "Company", "value": job["company"], "inline": True},
        {"name": "Location", "value": job["location"], "inline": True},
    ]
    if job["type"]:
        fields.append({"name": "Type", "value": job["type"], "inline": True})
    if job["salary"]:
        fields.append({"name": "Pay", "value": job["salary"], "inline": True})
    payload = {
        "username": "Job Scraper",
        "embeds": [
            {
                "title": job["title"][:250],
                "url": job["link"],
                "color": 3447003,
                "fields": fields,
                "footer": {"text": f"Source: {job['source']} (himalayas.app) - posted {job['posted']}"},
            }
        ],
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    except requests.RequestException as exc:
        print(f"Discord request failed: {exc}")
        return False
    if resp.status_code in (200, 204):
        return True
    print(f"Discord alert failed for '{job['title']}': HTTP {resp.status_code}")
    return False


def render_results(matches, new_ids):
    lines = [
        "# Latest job matches",
        "",
        f"{len(matches)} current matches. Marked NEW when first alerted in the most recent run.",
        "Source: [Himalayas](https://himalayas.app) remote jobs open to Canada.",
        "",
        "| | Title | Company | Location | Pay | Posted |",
        "|---|---|---|---|---|---|",
    ]
    for j in matches:
        flag = "NEW" if j["id"] in new_ids else ""
        title = f"[{j['title']}]({j['link']})" if j["link"] else j["title"]
        lines.append(
            f"| {flag} | {title} | {j['company']} | {j['location']} | {j['salary'] or '-'} | {j['posted']} |"
        )
    return "\n".join(lines) + "\n"


# --- MAIN --------------------------------------------------------------------
if __name__ == "__main__":
    print("Starting job search execution...")
    stats = {"requests": 0, "errors": 0}
    all_jobs = []
    for name, fetch in SOURCES:
        got = fetch(stats)
        print(f"[{name}] fetched {len(got)} postings")
        all_jobs.extend(got)

    matches = select_matches(all_jobs)
    seen = load_seen_jobs()
    unseen = [j for j in matches if j["id"] not in seen]

    alerted, new_ids = 0, set()
    if unseen and not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set: showing results but sending no alerts.")
    for job in unseen[:MAX_ALERTS_PER_RUN]:
        if send_discord_alert(job):
            seen.add(job["id"])  # only remember a job once its alert actually went out
            new_ids.add(job["id"])
            alerted += 1
            time.sleep(0.6)
    save_seen_jobs(seen)

    results_md = render_results(matches, new_ids)
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        f.write(results_md)

    summary = (
        f"## Job scraper run\n\n"
        f"- Postings fetched: **{len(all_jobs)}**\n"
        f"- Matching your titles: **{len(matches)}**\n"
        f"- Not yet alerted: **{len(unseen)}**, alerts sent this run: **{alerted}**\n"
        f"- Requests: {stats['requests']}, errors: {stats['errors']}\n"
        f"- Discord webhook configured: **{'yes' if DISCORD_WEBHOOK_URL else 'NO'}**\n\n"
    )
    if SUMMARY_FILE:
        with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
            f.write(summary + results_md)
    print(summary)

    # Make a broken source visible instead of a silent green run.
    if not all_jobs and stats["errors"]:
        print("No postings retrieved and errors occurred: failing the run.")
        sys.exit(1)
