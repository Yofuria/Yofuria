"""Generate shared homepage metrics and small SVG badges, using only the stdlib.

Compare Google Scholar's existing cache with the Semantic Scholar author API.
Display the larger citation count and link the badge to its author profile.
Stars count the listed projects, including organization repositories, once each.
On a failed request, retain that source's last successful value and timestamp.
"""

import json
import os
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "metrics"


def fetch_json(url):
    headers = {"User-Agent": "Yofuria-profile-metrics", "Accept": "application/json"}
    if url.startswith("https://api.github.com/"):
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if token := os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=25) as response:
        return json.load(response)


def count(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("Expected a non-negative integer count")
    return value


def utc_date(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # The existing Scholar crawler writes its UTC runner time without an offset.
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def collect_citations(config, previous, now):
    old_citations = previous.get("citations", {})
    cached = dict(old_citations.get("sources", {}))
    # Migrate the original Google-only JSON without losing its last good value.
    if not cached and old_citations.get("url", "").startswith("https://scholar.google.com/"):
        cached["googleScholar"] = {k: old_citations[k] for k in ("value", "updatedAt", "url")}
    sources = {}
    try:
        scholar = fetch_json(config["scholarDataUrl"])
        if scholar.get("scholar_id") != config["scholarId"]:
            raise ValueError("Scholar profile does not match the configured ID")
        sources["googleScholar"] = {
            "value": count(scholar["citedby"]),
            "updatedAt": utc_date(scholar["updated"]),
            "url": f"https://scholar.google.com/citations?user={config['scholarId']}",
        }
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
        if "googleScholar" in cached:
            sources["googleScholar"] = cached["googleScholar"]
        print("Google Scholar unavailable; retaining its last valid snapshot if available.", file=sys.stderr)

    try:
        author_id = config["semanticScholarId"]
        author = fetch_json(f"https://api.semanticscholar.org/graph/v1/author/{author_id}?fields=authorId,name,citationCount")
        if author.get("authorId") != author_id:
            raise ValueError("Semantic Scholar author does not match the configured ID")
        sources["semanticScholar"] = {
            "value": count(author["citationCount"]),
            "updatedAt": now,
            "url": config["semanticScholarUrl"],
        }
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
        if "semanticScholar" in cached:
            sources["semanticScholar"] = cached["semanticScholar"]
        print("Semantic Scholar unavailable; retaining its last valid snapshot if available.", file=sys.stderr)

    if not sources:
        raise RuntimeError("No valid citation counts available; leaving all published output untouched")
    for source in sources.values():
        count(source["value"])
        utc_date(source["updatedAt"])
    # Ties prefer Google Scholar. Compare the current snapshots, not past maxima:
    # fresh counts can decrease after profile corrections.
    winner = max(sources, key=lambda key: (sources[key]["value"], key == "googleScholar"))
    return {**sources[winner], "sources": sources}


def collect(config, previous, now):
    citations = collect_citations(config, previous, now)

    old_repos = {r["source"].lower(): r for r in previous.get("github", {}).get("repositories", [])}
    repositories = {}
    for source in dict.fromkeys(r.lower() for r in config["repositories"]):
        try:
            repo = fetch_json(f"https://api.github.com/repos/{source}")
            if repo.get("private") is not False:
                raise ValueError("Expected a public project repository")
            entry = {
                "id": count(repo["id"]), "source": source,
                "name": repo["full_name"], "stars": count(repo["stargazers_count"]),
                "url": repo["html_url"], "updatedAt": now,
            }
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
            if source not in old_repos:
                raise RuntimeError(f"{source} unavailable and no previous data exists") from error
            entry = old_repos[source]
            count(entry["stars"])
            print(f"{source} unavailable; keeping its last successful count and date.", file=sys.stderr)
        # A repository rename or two aliases must not cause double counting.
        repositories[entry["id"]] = entry

    entries = sorted(repositories.values(), key=lambda r: (-r["stars"], r["name"].lower()))
    if not entries:
        raise ValueError("No projects configured")
    return {
        "schemaVersion": 1,
        "citations": citations,
        "github": {
            "value": sum(r["stars"] for r in entries),
            "updatedAt": min(r["updatedAt"] for r in entries),
            "repositories": entries,
            "url": "https://github.com/Yofuria/Yofuria/blob/main/metrics/README.md",
        },
        "generatedAt": now,
    }


def badge(label, value, updated, dark=False):
    number = f"{count(value):,}"
    value_x = 77 if label == "Citations" else 94
    width = value_x + len(number) * 7 + 11
    paper, rule, muted, ink = (
        ("#0d1117", "#30363d", "#9198a1", "#f0f6fc") if dark
        else ("#ffffff", "#e8eaed", "#646a70", "#24282c")
    )
    title = escape(f"{label}: {number}. Updated {updated}.")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="26" viewBox="0 0 {width} 26" role="img" aria-labelledby="title">
  <title id="title">{title}</title>
  <rect x="0.5" y="0.5" width="{width - 1}" height="25" rx="4" fill="{paper}" stroke="{rule}"/>
  <g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif" font-size="12">
    <text x="10" y="17" fill="{muted}">{escape(label)}</text>
    <text x="{value_x}" y="17" fill="{ink}" font-weight="600" font-family="SFMono-Regular,Consolas,monospace">{number}</text>
  </g>
</svg>
'''


def update_readme_citation_link(text, url):
    start_marker = "<!-- citations-badge:start -->"
    end_marker = "<!-- citations-badge:end -->"
    start = text.index(start_marker)
    end = text.index(end_marker, start) + len(end_marker)
    block = f'''{start_marker}
<a href="{escape(url, quote=True)}"><picture><source media="(prefers-color-scheme: dark)" srcset="metrics/citations-dark.svg"><img src="metrics/citations.svg" alt="Citations" height="26"></picture></a>
{end_marker}'''
    return text[:start] + block + text[end:]


def write_outputs(stats):
    profile_readme = ROOT / "README.md"
    updated_readme = update_readme_citation_link(profile_readme.read_text(), stats["citations"]["url"])
    OUTPUT.mkdir(exist_ok=True)
    # Collect and validate every source before replacing any generated files.
    (OUTPUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n")
    for filename, label, metric in [
        ("citations", "Citations", stats["citations"]),
        ("project-stars", "Project stars", stats["github"]),
    ]:
        for dark in (False, True):
            suffix = "-dark" if dark else ""
            (OUTPUT / f"{filename}{suffix}.svg").write_text(badge(label, metric["value"], metric["updatedAt"], dark))

    rows = "\n".join(
        f"| [{r['name']}]({r['url']}) | {r['stars']:,} | {r['updatedAt']} |"
        for r in stats["github"]["repositories"]
    )
    (OUTPUT / "README.md").write_text(f'''# Profile metrics

These small badges share the same data on [my website](https://yofuria.github.io/) and [GitHub profile](https://github.com/Yofuria).

**Citations: {stats['citations']['value']:,}** · Last successfully collected: {stats['citations']['updatedAt']}.

**Project stars: {stats['github']['value']:,}** across {len(stats['github']['repositories'])} listed open-source projects, including organization repositories. Each repository is counted once, using its stable GitHub ID. This is a project total, not stars owned by the personal account or a count of unique people.

| Repository | Stars | Last successfully checked (UTC) |
| --- | ---: | --- |
{rows}

The workflow attempts an update daily at 10:47 UTC (18:47 China time), and supports manual runs. It compares Google Scholar and Semantic Scholar on every update and displays the larger count. The citations badge links to the corresponding author profile; ties prefer Google Scholar. A failed source keeps its previous value and timestamp. If it has no snapshot, the available source is used; if neither has valid data, all published output is left untouched.

To change the scope, edit [`config.json`](config.json). The list covers repositories linked from the website's publications/projects and GitHub profile. `LLM-Memory-Survey` is now `Awesome-AI-Memory`; the stable repository ID prevents duplicate counts after renames.

The website fetches [`stats.json`](stats.json); GitHub displays the generated light/dark SVGs. The workflow also updates the citations link in the profile README when the selected author profile changes. No extra service or API key is needed. The existing Google Scholar crawler is maintained in the website repository; Semantic Scholar uses its official author API.
''')
    profile_readme.write_text(updated_readme)


def main():
    config = json.loads((OUTPUT / "config.json").read_text())
    path = OUTPUT / "stats.json"
    previous = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    stats = collect(config, previous, now)
    write_outputs(stats)
    print(f"Published metrics: {stats['citations']['value']} citations; {stats['github']['value']} project stars.")


if __name__ == "__main__":
    main()
