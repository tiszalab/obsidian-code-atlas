#!/usr/bin/env python3
"""
gh_puller — turn this Obsidian vault into a GitHub activity dashboard.

Generates three things from your `gh`-authenticated account:

  1. Activity.md      — recent commits across every repo you touch
  2. Repos/ + Repos.base   — one note per repo, browsable as a Base database
  3. Scripts/ + Scripts.base — every .py / .R / .Rmd / .sh you've written, as searchable notes

Everything is regenerated on each run. The Repos/ and Scripts/ folders and the
Activity.md / *.base files are fully managed by this script — don't hand-edit them.

Usage (run from this folder):
    python3 gh_puller.py all        # refresh everything
    python3 gh_puller.py activity   # just the commit dashboard
    python3 gh_puller.py repos      # just the repo Base
    python3 gh_puller.py scripts    # just the script notes

Requires the `gh` CLI, authenticated (`gh auth status`).
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ─────────────────────────── configuration ────────────────────────────

# All output is written into this script's own folder, so the dashboard is a
# self-contained section you can drop into any Obsidian vault.
VAULT = Path(__file__).resolve().parent

# How many recent commits to pull for the Activity dashboard (max 1000; the
# GitHub commit-search API caps total results there).
COMMIT_LIMIT = 300

# Transient-failure retry policy for gh calls (network resets, DNS not-ready
# right after wake-from-sleep, 5xx, rate limiting). Delays are exponential:
# RETRY_BASE_DELAY, ×2, ×4, … Defaults give ~3+6+12+24 = 45s of retrying.
NETWORK_RETRIES = 5
RETRY_BASE_DELAY = 3.0  # seconds

# File extensions treated as "scripts" (case-insensitive).
SCRIPT_EXTS = {".py", ".r", ".rmd", ".sh"}

# Language label + code-fence hint per extension. (.Rmd content is fenced as
# literal text, so its own ```{r} chunks don't render — the fence width is
# auto-expanded to survive them.)
LANG = {
    ".py": ("python", "python"),
    ".r": ("R", "r"),
    ".rmd": ("R Markdown", "markdown"),
    ".sh": ("shell", "bash"),
}

# Embed each repo's README into its repo note (also makes READMEs searchable
# and filterable via Repos.base).
EMBED_READMES = True

# Skip embedding READMEs larger than this (bytes).
MAX_README_BYTES = 200_000

# Skip script files larger than this (bytes) — usually generated/data blobs.
MAX_SCRIPT_BYTES = 300_000

# Path fragments that mark vendored / generated code we don't want to index.
EXCLUDE_DIR_PARTS = {
    ".git", "node_modules", "site-packages", "__pycache__", "venv", ".venv",
    "env", ".env", "build", "dist", "vendor", "third_party", ".snakemake",
    ".tox", ".eggs", "egg-info", ".mypy_cache", ".pytest_cache",
}

# Include repos you own but that are forks? (Forks you've committed to are
# picked up regardless via commit activity.)
INCLUDE_OWNED_FORKS = False

# The Repos database and Scripts mirror only cover repos you *own* or that
# belong to one of your GitHub orgs. External repos you've merely committed to
# (e.g. a one-off PR to a big community project) still appear in the Activity
# feed, but their code is not pulled into the vault. Set to False to mirror
# every repo you've committed to (can be huge for shared community repos).
MIRROR_OWNED_AND_ORGS_ONLY = True

# Hard safety cap: skip mirroring scripts from any single repo above this many
# matching files (guards against unexpectedly large / vendored repos).
MAX_SCRIPTS_PER_REPO = 750

# Managed output locations (relative to vault).
ACTIVITY_MD = VAULT / "Activity.md"
REPOS_DIR = VAULT / "Repos"
REPOS_BASE = VAULT / "Repos.base"
SCRIPTS_DIR = VAULT / "Scripts"
SCRIPTS_BASE = VAULT / "Scripts.base"
HOME_MD = VAULT / "GitHub Dashboard.md"

# ─────────────────────────── gh helpers ───────────────────────────────


# A gh failure whose stderr matches one of these is permanent — don't retry.
_PERMANENT_ERR = re.compile(r"HTTP 404|Not Found|HTTP 410|Gone", re.IGNORECASE)


def gh(args: list[str], *, binary: bool = False) -> bytes | str:
    """Run a `gh` command, returning stdout.

    Transient failures (connection reset, DNS not-yet-up after wake, 5xx,
    rate limiting) are retried with exponential backoff — important for
    unattended cron/launchd runs. Permanent errors (404) raise immediately.
    """
    err = ""
    for attempt in range(NETWORK_RETRIES):
        res = subprocess.run(
            ["gh", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout if binary else res.stdout.decode("utf-8", "replace")
        err = res.stderr.decode("utf-8", "replace")
        if _PERMANENT_ERR.search(err):
            break
        if attempt < NETWORK_RETRIES - 1:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print(f"  … gh call failed (attempt {attempt + 1}/{NETWORK_RETRIES}); "
                  f"retrying in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"gh {' '.join(args)} failed:\n{err}")


def gh_json(args: list[str]):
    return json.loads(gh(args))


def whoami() -> str:
    return gh(["api", "/user", "--jq", ".login"]).strip()


def get_orgs() -> set[str]:
    try:
        return set(gh(["api", "/user/orgs", "--jq", ".[].login"]).split())
    except RuntimeError:
        return set()


# ─────────────────────────── string utils ─────────────────────────────


def yaml_scalar(v) -> str:
    """Render a Python value as a safe YAML scalar (JSON is valid YAML)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    # Bare ISO dates so Obsidian types them as Date (nicer in Bases).
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(str(x)) for x in v) + "]"
    return json.dumps(str(v))


def frontmatter(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list) and v:
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {json.dumps(str(item))}")
        elif isinstance(v, list):
            lines.append(f"{k}: []")
        else:
            lines.append(f"{k}: {yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def sanitize(name: str) -> str:
    """Make a string safe as an Obsidian filename component."""
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[:*?"<>|#^\[\]]', "-", name)
    name = name.lstrip(".")  # avoid hidden files
    return name.strip() or "unnamed"


def code_fence(content: str, lang: str) -> str:
    """Fence content, using enough backticks to survive embedded fences."""
    longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{content}\n{ticks}"


def iso_date(s: str | None) -> str:
    if not s:
        return ""
    return s[:10]


# ─────────────────────────── repo discovery ───────────────────────────

REPO_FIELDS = [
    "name", "nameWithOwner", "owner", "description", "primaryLanguage",
    "pushedAt", "createdAt", "isPrivate", "isFork", "isArchived",
    "stargazerCount", "forkCount", "repositoryTopics", "url",
    "defaultBranchRef", "diskUsage", "licenseInfo",
]


def normalize_owned(r: dict) -> dict:
    return {
        "full_name": r["nameWithOwner"],
        "owner": r["owner"]["login"],
        "name": r["name"],
        "description": r.get("description") or "",
        "language": (r.get("primaryLanguage") or {}).get("name") or "",
        "private": bool(r.get("isPrivate")),
        "fork": bool(r.get("isFork")),
        "archived": bool(r.get("isArchived")),
        "stars": r.get("stargazerCount", 0),
        "forks": r.get("forkCount", 0),
        "topics": [t["name"] for t in (r.get("repositoryTopics") or [])
                   if t.get("name")],
        "url": r["url"],
        "pushed": iso_date(r.get("pushedAt")),
        "created": iso_date(r.get("createdAt")),
        "branch": (r.get("defaultBranchRef") or {}).get("name") or "",
        "size_kb": r.get("diskUsage", 0),
        "license": (r.get("licenseInfo") or {}).get("name") or "",
    }


def normalize_rest(r: dict) -> dict:
    return {
        "full_name": r["full_name"],
        "owner": r["owner"]["login"],
        "name": r["name"],
        "description": r.get("description") or "",
        "language": r.get("language") or "",
        "private": bool(r.get("private")),
        "fork": bool(r.get("fork")),
        "archived": bool(r.get("archived")),
        "stars": r.get("stargazers_count", 0),
        "forks": r.get("forks_count", 0),
        "topics": r.get("topics") or [],
        "url": r["html_url"],
        "pushed": iso_date(r.get("pushed_at")),
        "created": iso_date(r.get("created_at")),
        "branch": r.get("default_branch") or "",
        "size_kb": r.get("size", 0),
        "license": (r.get("license") or {}).get("name") or "" if r.get("license") else "",
    }


def get_owned_repos() -> dict[str, dict]:
    print("• Listing repos you own …")
    raw = gh_json([
        "repo", "list", "--limit", "1000", "--json", ",".join(REPO_FIELDS),
    ])
    out = {}
    for r in raw:
        repo = normalize_owned(r)
        if repo["fork"] and not INCLUDE_OWNED_FORKS:
            continue
        out[repo["full_name"]] = repo
    print(f"  → {len(out)} owned repos")
    return out


def search_commits(login: str, limit: int) -> list[dict]:
    """Recent commits authored by `login`, newest first."""
    print(f"• Searching recent commits by {login} …")
    items = []
    per_page = 100
    page = 1
    while len(items) < limit:
        data = json.loads(gh([
            "api", "-H", "Accept: application/vnd.github.cloak-preview+json",
            f"/search/commits?q=author:{login}&sort=author-date&order=desc"
            f"&per_page={per_page}&page={page}",
        ]))
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    items = items[:limit]
    print(f"  → {len(items)} commits (of {data.get('total_count', '?')} total)")
    return items


def repos_from_commits(commits: list[dict]) -> set[str]:
    return {c["repository"]["full_name"] for c in commits}


def fetch_readme(full_name: str) -> str | None:
    """Return a repo's README as raw markdown, or None if it has none."""
    try:
        text = gh(["api", f"/repos/{full_name}/readme",
                   "-H", "Accept: application/vnd.github.raw"])
    except RuntimeError:
        return None
    if not text.strip() or len(text.encode("utf-8")) > MAX_README_BYTES:
        return None
    return text


def fetch_repo_meta(full_name: str) -> dict | None:
    try:
        r = json.loads(gh(["api", f"/repos/{full_name}"]))
        return normalize_rest(r)
    except RuntimeError as e:
        print(f"  ! skip {full_name}: {str(e).splitlines()[0]}")
        return None


# ─────────────────────────── builders ─────────────────────────────────


def build_activity(commits: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    by_day: dict[str, list[dict]] = defaultdict(list)
    repos_seen: set[str] = set()
    last7 = last30 = 0
    for c in commits:
        date_str = c["commit"]["author"]["date"]
        day = date_str[:10]
        by_day[day].append(c)
        repos_seen.add(c["repository"]["full_name"])
        try:
            dt = datetime.fromisoformat(date_str)
            age = (now - dt.astimezone(timezone.utc)).days
            if age <= 7:
                last7 += 1
            if age <= 30:
                last30 += 1
        except ValueError:
            pass

    lines = [
        "---",
        "source: gh_puller",
        f"generated: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "---",
        "# 📊 GitHub Activity",
        "",
        f"> Last refreshed **{now.strftime('%Y-%m-%d %H:%M UTC')}** · "
        f"showing the {len(commits)} most recent commits.",
        "",
        "| Window | Commits |",
        "| --- | --- |",
        f"| Last 7 days | **{last7}** |",
        f"| Last 30 days | **{last30}** |",
        f"| Repos touched | **{len(repos_seen)}** |",
        "",
        "## Commits by day",
        "",
    ]
    for day in sorted(by_day, reverse=True):
        day_commits = by_day[day]
        lines.append(f"### {day}  ·  {len(day_commits)} commit"
                     f"{'s' if len(day_commits) != 1 else ''}")
        lines.append("")
        lines.append("| Repo | Message | Commit |")
        lines.append("| --- | --- | --- |")
        for c in sorted(day_commits, key=lambda x: x["commit"]["author"]["date"],
                        reverse=True):
            full = c["repository"]["full_name"]
            msg = c["commit"]["message"].splitlines()[0].strip()
            msg = msg.replace("|", "\\|")[:100]
            sha = c["sha"][:7]
            url = c["html_url"]
            lines.append(f"| `{full}` | {msg} | [`{sha}`]({url}) |")
        lines.append("")

    ACTIVITY_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ Wrote {ACTIVITY_MD.relative_to(VAULT)}")


def build_repos(repos: dict[str, dict]) -> None:
    if REPOS_DIR.exists():
        shutil.rmtree(REPOS_DIR)
    REPOS_DIR.mkdir(parents=True)

    for full, r in sorted(repos.items()):
        fields = {
            "source": "gh_puller",
            "tags": ["gh/repo"],
            "repo": r["full_name"],
            "owner": r["owner"],
            "language": r["language"],
            "description": r["description"],
            "private": r["private"],
            "fork": r["fork"],
            "archived": r["archived"],
            "stars": r["stars"],
            "forks": r["forks"],
            "pushed": r["pushed"],
            "created": r["created"],
            "topics": r["topics"],
            "license": r["license"],
            "url": r["url"],
        }
        body = [
            frontmatter(fields),
            f"# {r['full_name']}",
            "",
            r["description"] or "_No description._",
            "",
            f"🔗 [Open on GitHub]({r['url']})",
            "",
            f"- **Language:** {r['language'] or '—'}",
            f"- **Stars:** {r['stars']}  ·  **Forks:** {r['forks']}",
            f"- **Last push:** {r['pushed'] or '—'}  ·  "
            f"**Created:** {r['created'] or '—'}",
            f"- **Visibility:** {'private' if r['private'] else 'public'}"
            f"{' · fork' if r['fork'] else ''}"
            f"{' · archived' if r['archived'] else ''}",
        ]
        if EMBED_READMES:
            readme = fetch_readme(full)
            if readme:
                body += ["", "## README", "", readme.rstrip()]
        fname = sanitize(r["full_name"]) + ".md"
        (REPOS_DIR / fname).write_text("\n".join(body), encoding="utf-8")

    REPOS_BASE.write_text(REPOS_BASE_CONTENT, encoding="utf-8")
    print(f"✓ Wrote {len(repos)} repo notes to {REPOS_DIR.relative_to(VAULT)}/ "
          f"and {REPOS_BASE.name}")


def build_scripts(repos: dict[str, dict]) -> None:
    if SCRIPTS_DIR.exists():
        shutil.rmtree(SCRIPTS_DIR)
    SCRIPTS_DIR.mkdir(parents=True)

    total = 0
    for full, r in sorted(repos.items()):
        branch = r["branch"] or "HEAD"
        print(f"• Fetching scripts from {full} …")
        tar_path = f"/repos/{full}/tarball/{branch}" if r["branch"] \
            else f"/repos/{full}/tarball"
        try:
            blob = gh(["api", tar_path], binary=True)
        except RuntimeError as e:
            print(f"  ! skip {full}: {str(e).splitlines()[0]}")
            continue

        count = 0
        try:
            tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
        except tarfile.TarError as e:
            print(f"  ! bad tarball for {full}: {e}")
            continue

        # Guard against unexpectedly huge repos before writing anything.
        n_matching = sum(
            1 for m in tf.getmembers() if m.isfile()
            and Path(m.name.split("/", 1)[-1]).suffix.lower() in SCRIPT_EXTS
        )
        if n_matching > MAX_SCRIPTS_PER_REPO:
            print(f"  ! {full} has {n_matching} script files "
                  f"(> {MAX_SCRIPTS_PER_REPO} cap) — skipping")
            tf.close()
            continue

        for member in tf.getmembers():
            if not member.isfile():
                continue
            # Strip the leading "<owner>-<repo>-<sha>/" top-level dir.
            parts = member.name.split("/", 1)
            rel = parts[1] if len(parts) == 2 else parts[0]
            if not rel:
                continue
            ext = Path(rel).suffix.lower()
            if ext not in SCRIPT_EXTS:
                continue
            if any(p in EXCLUDE_DIR_PARTS for p in Path(rel).parts):
                continue
            if member.size > MAX_SCRIPT_BYTES:
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            raw = f.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue

            lang_label, fence_lang = LANG.get(ext, ("text", ""))
            gh_url = f"{r['url']}/blob/{branch}/{rel}"
            fields = {
                "source": "gh_puller",
                "tags": ["gh/script"],
                "repo": r["full_name"],
                "path": rel,
                "language": lang_label,
                "ext": ext,
                "lines": text.count("\n") + 1,
                "bytes": member.size,
                "url": gh_url,
            }
            note = [
                frontmatter(fields),
                f"# {Path(rel).name}",
                "",
                f"`{r['full_name']}` · [`{rel}`]({gh_url})",
                "",
                code_fence(text, fence_lang),
                "",
            ]
            out = SCRIPTS_DIR / sanitize(full) / (rel + ".md")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(note), encoding="utf-8")
            count += 1
        tf.close()
        total += count
        print(f"  → {count} scripts")

    SCRIPTS_BASE.write_text(SCRIPTS_BASE_CONTENT, encoding="utf-8")
    print(f"✓ Wrote {total} script notes to {SCRIPTS_DIR.relative_to(VAULT)}/ "
          f"and {SCRIPTS_BASE.name}")


def write_home() -> None:
    HOME_MD.write_text(HOME_CONTENT, encoding="utf-8")
    print(f"✓ Wrote {HOME_MD.name}")


# ─────────────────────────── static content ───────────────────────────

REPOS_BASE_CONTENT = """filters:
  and:
    - file.hasTag("gh/repo")
properties:
  note.stars:
    displayName: ⭐
  note.pushed:
    displayName: Pushed
  note.language:
    displayName: Lang
  note.private:
    displayName: Private
views:
  - type: table
    name: Recently pushed
    order:
      - file.name
      - note.language
      - note.stars
      - note.private
      - note.pushed
    sort:
      - property: note.pushed
        direction: DESC
  - type: table
    name: Most starred
    order:
      - file.name
      - note.language
      - note.stars
    sort:
      - property: note.stars
        direction: DESC
  - type: table
    name: Public only
    filters:
      and:
        - note.private == false
    order:
      - file.name
      - note.language
      - note.stars
      - note.pushed
    sort:
      - property: note.pushed
        direction: DESC
"""

SCRIPTS_BASE_CONTENT = """filters:
  and:
    - file.hasTag("gh/script")
properties:
  note.repo:
    displayName: Repo
  note.language:
    displayName: Lang
  note.lines:
    displayName: Lines
  note.path:
    displayName: Path
views:
  - type: table
    name: All scripts
    order:
      - file.name
      - note.repo
      - note.language
      - note.lines
    sort:
      - property: note.repo
        direction: ASC
  - type: table
    name: Python
    filters:
      and:
        - note.ext == ".py"
    order:
      - file.name
      - note.repo
      - note.lines
  - type: table
    name: R
    filters:
      and:
        - note.ext == ".r"
    order:
      - file.name
      - note.repo
      - note.lines
  - type: table
    name: Shell
    filters:
      and:
        - note.ext == ".sh"
    order:
      - file.name
      - note.repo
      - note.lines
"""

HOME_CONTENT = """---
source: gh_puller
---
# 🐙 GitHub Dashboard

Your GitHub activity, mirrored into this vault by `gh_puller`.

## Views

- [[Activity]] — recent commits across every repo you touch
- **Repos** — open [[Repos.base]] for the repo database (sortable / filterable)
- **Scripts** — open [[Scripts.base]] to browse & search every `.py` / `.R` / `.Rmd` / `.sh`

## Refresh

Regenerate everything from a terminal:

```bash
cd "/Users/michaeltisza/mike_tisza/github_repos/TiszaMike_notes/gh_puller"
python3 gh_puller.py all
```

Or refresh one section: `activity`, `repos`, or `scripts`. It also runs
automatically via the `com.mtisza.ghpuller` LaunchAgent (daily + at login).

> The `Repos/` and `Scripts/` folders and the `Activity.md` / `*.base` files are
> fully managed by the script — edits there are overwritten on the next run.
"""

# ─────────────────────────── entrypoint ───────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Mirror GitHub activity into Obsidian.")
    ap.add_argument("command", choices=["all", "activity", "repos", "scripts"],
                    help="what to refresh")
    args = ap.parse_args()

    try:
        login = whoami()
    except RuntimeError as e:
        print(f"Cannot reach GitHub via gh: {e}", file=sys.stderr)
        return 1
    orgs = get_orgs()
    scope = {login} | orgs
    print(f"Authenticated as {login}")
    print(f"Orgs in mirror scope: {', '.join(sorted(orgs)) or '(none)'}\n")

    # Discover the repo universe once (owned + anything you've committed to).
    # Commit search runs for every command, since it both feeds the Activity
    # dashboard and expands repo scope to the org repos you contribute to.
    repos = get_owned_repos()
    commits = search_commits(login, COMMIT_LIMIT)
    for full in sorted(repos_from_commits(commits)):
        if full in repos:
            continue
        owner = full.split("/", 1)[0]
        # Only pull metadata/code for repos you own or that are in your orgs.
        if MIRROR_OWNED_AND_ORGS_ONLY and owner not in scope:
            continue
        meta = fetch_repo_meta(full)
        if meta:
            repos[full] = meta
    print(f"→ {len(repos)} repos to mirror "
          f"({len(commits)} commits span more via Activity)\n")

    if args.command in ("all", "activity"):
        build_activity(commits)
    if args.command in ("all", "repos"):
        build_repos(repos)
    if args.command in ("all", "scripts"):
        build_scripts(repos)
    if args.command == "all":
        write_home()

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
