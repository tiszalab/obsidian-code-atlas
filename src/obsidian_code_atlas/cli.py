"""Command-line implementation for Obsidian Code Atlas."""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Mapping, Optional, Set, Tuple, Union

from . import __version__
from . import scheduler

COMMIT_LIMIT = 300
NETWORK_RETRIES = 5
RETRY_BASE_DELAY = 3.0
EMBED_READMES = True
MAX_README_BYTES = 200_000
MAX_SCRIPT_BYTES = 300_000
INCLUDE_OWNED_FORKS = False
MIRROR_OWNED_AND_ORGS_ONLY = True
MAX_SCRIPTS_PER_REPO = 750
MAX_ISSUES_PER_REPO = 500
MAX_ISSUE_BODY_BYTES = 100_000
MAX_FRONTMATTER_TEXT = 300
EXCLUDE_DIR_PARTS = {
    ".git", "node_modules", "site-packages", "__pycache__", "venv", ".venv",
    "env", ".env", "build", "dist", "vendor", "third_party", ".snakemake",
    ".tox", ".eggs", "egg-info", ".mypy_cache", ".pytest_cache",
}
LanguageMap = Dict[str, Tuple[str, str]]


def _default_languages() -> LanguageMap:
    return {
        ".py": ("Python", "python"), ".r": ("R", "r"),
        ".rmd": ("R Markdown", "markdown"), ".sh": ("Shell", "bash"),
        ".bash": ("Bash", "bash"), ".zsh": ("Zsh", "zsh"),
        ".fish": ("Fish", "fish"), ".ts": ("TypeScript", "typescript"),
        ".tsx": ("TypeScript TSX", "tsx"), ".js": ("JavaScript", "javascript"),
        ".jsx": ("JavaScript JSX", "jsx"), ".mjs": ("JavaScript Module", "javascript"),
        ".cjs": ("JavaScript CommonJS", "javascript"), ".rs": ("Rust", "rust"),
        ".go": ("Go", "go"), ".c": ("C", "c"), ".cpp": ("C++", "cpp"),
        ".cc": ("C++", "cpp"), ".cxx": ("C++", "cpp"),
        ".hpp": ("C++ Header", "cpp"), ".cs": ("C#", "csharp"),
        ".java": ("Java", "java"), ".kt": ("Kotlin", "kotlin"),
        ".kts": ("Kotlin Script", "kotlin"), ".scala": ("Scala", "scala"),
        ".sc": ("Scala", "scala"), ".swift": ("Swift", "swift"),
        ".m": ("MATLAB", "matlab"), ".mm": ("Objective-C++", "objectivec"),
        ".pl": ("Perl", "perl"), ".pm": ("Perl Module", "perl"),
        ".rb": ("Ruby", "ruby"), ".rake": ("Ruby Rake", "ruby"),
        ".php": ("PHP", "php"), ".lua": ("Lua", "lua"),
        ".vim": ("Vim", "vim"), ".groovy": ("Groovy", "groovy"),
        ".gradle": ("Gradle", "groovy"),
    }


def _parse_languages(values: Mapping[str, object]) -> Dict[str, Optional[Tuple[str, str]]]:
    out: Dict[str, Optional[Tuple[str, str]]] = {}
    for raw_ext, value in values.items():
        ext = str(raw_ext).lower().strip()
        if not ext.startswith("."):
            ext = "." + ext
        if value is None:
            out[ext] = None
        elif isinstance(value, str):
            out[ext] = (value, value.lower())
        elif isinstance(value, (list, tuple)) and len(value) >= 2:
            out[ext] = (str(value[0]), str(value[1]))
        elif isinstance(value, (list, tuple)) and len(value) == 1:
            out[ext] = (str(value[0]), str(value[0]).lower())
        elif isinstance(value, dict):
            label = str(value.get("label") or ext[1:].upper())
            out[ext] = (label, str(value.get("fence") or label.lower()))
        else:
            out[ext] = (str(value), str(value).lower())
    return out


def _parse_env_extensions(value: str) -> Dict[str, Optional[Tuple[str, str]]]:
    out: Dict[str, Optional[Tuple[str, str]]] = {}
    for token in value.split(","):
        token = token.strip()
        if not token or token == "-":
            continue
        if token.startswith("-") and "." in token:
            ext = token[1:].lower().strip()
            out[ext if ext.startswith(".") else "." + ext] = None
            continue
        parts = [part.strip() for part in token.split(":")]
        ext = parts[0].lower()
        ext = ext if ext.startswith(".") else "." + ext
        if len(parts) == 3:
            out[ext] = (parts[1], parts[2])
        elif len(parts) == 2:
            out[ext] = (parts[1], parts[1].lower())
        elif len(parts) == 1:
            out[ext] = (ext[1:].upper(), ext[1:].lower())
    return out


def select_config_path(output: Path, explicit: Optional[str], env: Mapping[str, str]) -> Optional[Path]:
    """Select configuration using the documented precedence."""
    configured = explicit if explicit is not None else (
        env.get("OBSIDIAN_CODE_ATLAS_CONFIG") or env.get("GH_PULLER_CONFIG") or None
    )
    if configured is not None:
        return Path(configured).expanduser().resolve()
    for name in ("obsidian-code-atlas.json", "gh_puller.json"):
        candidate = output / name
        if candidate.is_file():
            return candidate
    return None


def load_languages(output: Path, explicit_config: Optional[str] = None,
                   env: Optional[Mapping[str, str]] = None) -> LanguageMap:
    environment = os.environ if env is None else env
    langs = _default_languages()
    config_path = select_config_path(output, explicit_config, environment)
    if config_path is not None:
        if not config_path.is_file():
            raise FileNotFoundError("configuration file does not exist or is not a file: {}".format(config_path))
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(config, dict) and "script_extensions" in config:
                extensions = config["script_extensions"]
                if not isinstance(extensions, dict):
                    raise ValueError("script_extensions must be a JSON object")
                for ext, value in _parse_languages(extensions).items():
                    if value is None:
                        langs.pop(ext, None)
                    else:
                        langs[ext] = value
        except Exception as exc:
            print("Warning: could not load {}: {}".format(config_path, exc), file=sys.stderr)
    extension_env = environment.get("OBSIDIAN_CODE_ATLAS_EXTENSIONS") or environment.get(
        "GH_PULLER_EXTENSIONS", "")
    if extension_env:
        for ext, value in _parse_env_extensions(extension_env).items():
            if value is None:
                langs.pop(ext, None)
            else:
                langs[ext] = value
    return langs


class ManagedPathError(ValueError):
    pass


@dataclass(frozen=True)
class Context:
    output: Path
    languages: LanguageMap

    def path(self, *parts: str) -> Path:
        candidate = self.output.joinpath(*parts).resolve()
        try:
            candidate.relative_to(self.output)
        except ValueError:
            raise ManagedPathError("managed path escapes output directory: {}".format(candidate))
        return candidate


_PERMANENT_ERR = re.compile(r"HTTP 404|Not Found|HTTP 410|Gone", re.IGNORECASE)


def gh(args: List[str], binary: bool = False) -> Union[bytes, str]:
    err = ""
    for attempt in range(NETWORK_RETRIES):
        result = subprocess.run(["gh"] + args, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        if result.returncode == 0:
            return result.stdout if binary else result.stdout.decode("utf-8", "replace")
        err = result.stderr.decode("utf-8", "replace")
        if _PERMANENT_ERR.search(err):
            break
        if attempt < NETWORK_RETRIES - 1:
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            print("  … gh call failed (attempt {}/{}); retrying in {:.0f}s".format(
                attempt + 1, NETWORK_RETRIES, delay), file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError("gh {} failed:\n{}".format(" ".join(args), err))


def gh_json(args: List[str]):
    return json.loads(gh(args))


def whoami() -> str:
    return str(gh(["api", "/user", "--jq", ".login"])).strip()


def get_orgs() -> Set[str]:
    try:
        return set(str(gh(["api", "/user/orgs", "--jq", ".[].login"])).split())
    except RuntimeError:
        return set()


def yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(item)) for item in value) + "]"
    return json.dumps(str(value))


def frontmatter(fields: Mapping[str, object]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list) and value:
            lines.append("{}:".format(key))
            lines.extend("  - {}".format(json.dumps(str(item))) for item in value)
        elif isinstance(value, list):
            lines.append("{}: []".format(key))
        else:
            lines.append("{}: {}".format(key, yaml_scalar(value)))
    return "\n".join(lines + ["---"]) + "\n"


def sanitize(name: str) -> str:
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[:*?"<>|#^\[\]]', "-", name).lstrip(".")
    return name.strip() or "unnamed"


def code_fence(content: str, lang: str) -> str:
    longest = max((len(match) for match in re.findall(r"`+", content)), default=0)
    ticks = "`" * max(3, longest + 1)
    return "{}{}\n{}\n{}".format(ticks, lang, content, ticks)


def iso_date(value: Optional[str]) -> str:
    return value[:10] if value else ""


REPO_FIELDS = [
    "name", "nameWithOwner", "owner", "description", "primaryLanguage",
    "pushedAt", "createdAt", "isPrivate", "isFork", "isArchived",
    "stargazerCount", "forkCount", "repositoryTopics", "url",
    "defaultBranchRef", "diskUsage", "licenseInfo",
]


def normalize_owned(repo: dict) -> dict:
    return {
        "full_name": repo["nameWithOwner"], "owner": repo["owner"]["login"],
        "name": repo["name"], "description": repo.get("description") or "",
        "language": (repo.get("primaryLanguage") or {}).get("name") or "",
        "private": bool(repo.get("isPrivate")), "fork": bool(repo.get("isFork")),
        "archived": bool(repo.get("isArchived")), "stars": repo.get("stargazerCount", 0),
        "forks": repo.get("forkCount", 0),
        "topics": [topic["name"] for topic in (repo.get("repositoryTopics") or []) if topic.get("name")],
        "url": repo["url"], "pushed": iso_date(repo.get("pushedAt")),
        "created": iso_date(repo.get("createdAt")),
        "branch": (repo.get("defaultBranchRef") or {}).get("name") or "",
        "size_kb": repo.get("diskUsage", 0),
        "license": (repo.get("licenseInfo") or {}).get("name") or "",
    }


def normalize_rest(repo: dict) -> dict:
    return {
        "full_name": repo["full_name"], "owner": repo["owner"]["login"],
        "name": repo["name"], "description": repo.get("description") or "",
        "language": repo.get("language") or "", "private": bool(repo.get("private")),
        "fork": bool(repo.get("fork")), "archived": bool(repo.get("archived")),
        "stars": repo.get("stargazers_count", 0), "forks": repo.get("forks_count", 0),
        "topics": repo.get("topics") or [], "url": repo["html_url"],
        "pushed": iso_date(repo.get("pushed_at")), "created": iso_date(repo.get("created_at")),
        "branch": repo.get("default_branch") or "", "size_kb": repo.get("size", 0),
        "license": (repo.get("license") or {}).get("name") or "" if repo.get("license") else "",
    }


def get_owned_repos() -> Dict[str, dict]:
    print("• Listing repos you own …")
    raw = gh_json(["repo", "list", "--limit", "1000", "--json", ",".join(REPO_FIELDS)])
    repos = {}
    for item in raw:
        repo = normalize_owned(item)
        if repo["fork"] and not INCLUDE_OWNED_FORKS:
            continue
        repos[repo["full_name"]] = repo
    print("  → {} owned repos".format(len(repos)))
    return repos


def search_commits(login: str, limit: int) -> List[dict]:
    print("• Searching recent commits by {} …".format(login))
    items = []
    page = 1
    total = "?"
    while len(items) < limit:
        data = json.loads(str(gh([
            "api", "-H", "Accept: application/vnd.github.cloak-preview+json",
            "/search/commits?q=author:{}&sort=author-date&order=desc&per_page=100&page={}".format(login, page),
        ])))
        total = data.get("total_count", "?")
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    items = items[:limit]
    print("  → {} commits (of {} total)".format(len(items), total))
    return items


def repos_from_commits(commits: List[dict]) -> Set[str]:
    return {commit["repository"]["full_name"] for commit in commits}


def fetch_readme(full_name: str) -> Optional[str]:
    try:
        text = str(gh(["api", "/repos/{}/readme".format(full_name),
                       "-H", "Accept: application/vnd.github.raw"]))
    except RuntimeError:
        return None
    return text if text.strip() and len(text.encode("utf-8")) <= MAX_README_BYTES else None


def fetch_repo_meta(full_name: str) -> Optional[dict]:
    try:
        return normalize_rest(json.loads(str(gh(["api", "/repos/{}".format(full_name)]))))
    except RuntimeError as exc:
        print("  ! skip {}: {}".format(full_name, str(exc).splitlines()[0]))
        return None


def build_activity(context: Context, commits: List[dict]) -> None:
    now = datetime.now(timezone.utc)
    by_day = defaultdict(list)
    repos_seen = set()
    last7 = last30 = 0
    for commit in commits:
        date_str = commit["commit"]["author"]["date"]
        by_day[date_str[:10]].append(commit)
        repos_seen.add(commit["repository"]["full_name"])
        try:
            age = (now - datetime.fromisoformat(date_str).astimezone(timezone.utc)).days
            last7 += age <= 7
            last30 += age <= 30
        except ValueError:
            pass
    lines = ["---", "source: obsidian-code-atlas",
             "generated: {}".format(now.strftime("%Y-%m-%d %H:%M UTC")), "---",
             "# 📊 GitHub Activity", "",
             "> Last refreshed **{}** · showing the {} most recent commits.".format(
                 now.strftime("%Y-%m-%d %H:%M UTC"), len(commits)), "",
             "| Window | Commits |", "| --- | --- |",
             "| Last 7 days | **{}** |".format(last7),
             "| Last 30 days | **{}** |".format(last30),
             "| Repos touched | **{}** |".format(len(repos_seen)), "", "## Commits by day", ""]
    for day in sorted(by_day, reverse=True):
        day_commits = by_day[day]
        lines.extend(["### {}  ·  {} commit{}".format(day, len(day_commits),
                      "s" if len(day_commits) != 1 else ""), "",
                      "| Repo | Message | Commit |", "| --- | --- | --- |"])
        for commit in sorted(day_commits, key=lambda item: item["commit"]["author"]["date"], reverse=True):
            full = commit["repository"]["full_name"]
            message = commit["commit"]["message"].splitlines()[0].strip().replace("|", "\\|")[:100]
            lines.append("| `{}` | {} | [`{}`]({}) |".format(
                full, message, commit["sha"][:7], commit["html_url"]))
        lines.append("")
    path = context.path("Activity.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    print("✓ Wrote {}".format(path.relative_to(context.output)))


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


def build_repos(context: Context, repos: Dict[str, dict]) -> None:
    repos_dir = context.path("Repos")
    if repos_dir.exists():
        shutil.rmtree(repos_dir)
    repos_dir.mkdir(parents=True)
    for full, repo in sorted(repos.items()):
        fields = {
            "source": "obsidian-code-atlas", "tags": ["gh/repo"], "repo": repo["full_name"],
            "owner": repo["owner"], "language": repo["language"], "description": repo["description"],
            "private": repo["private"], "fork": repo["fork"], "archived": repo["archived"],
            "stars": repo["stars"], "forks": repo["forks"], "pushed": repo["pushed"],
            "created": repo["created"], "topics": repo["topics"], "license": repo["license"],
            "url": repo["url"],
        }
        body = [frontmatter(fields), "# {}".format(repo["full_name"]), "",
                repo["description"] or "_No description._", "",
                "🔗 [Open on GitHub]({})".format(repo["url"]), "",
                "- **Language:** {}".format(repo["language"] or "—"),
                "- **Stars:** {}  ·  **Forks:** {}".format(repo["stars"], repo["forks"]),
                "- **Last push:** {}  ·  **Created:** {}".format(repo["pushed"] or "—", repo["created"] or "—"),
                "- **Visibility:** {}{}{}".format("private" if repo["private"] else "public",
                    " · fork" if repo["fork"] else "", " · archived" if repo["archived"] else "")]
        if EMBED_READMES:
            readme = fetch_readme(full)
            if readme:
                body.extend(["", "## README", "", readme.rstrip()])
        context.path("Repos", sanitize(repo["full_name"]) + ".md").write_text(
            "\n".join(body), encoding="utf-8")
    context.path("Repos.base").write_text(REPOS_BASE_CONTENT, encoding="utf-8")
    print("✓ Wrote {} repo notes to Repos/ and Repos.base".format(len(repos)))


def _build_scripts_base(langs: LanguageMap) -> str:
    base = """filters:
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
    name: "All scripts"
    order:
      - file.name
      - note.repo
      - note.language
      - note.lines
    sort:
      - property: note.repo
        direction: ASC
"""
    groups = defaultdict(list)
    for ext, (label, _fence) in sorted(langs.items(), key=lambda item: (item[1][0].lower(), item[0])):
        groups[label].append(ext)
    for label in sorted(groups, key=str.lower):
        exts = sorted(groups[label])
        filters = "\n".join("        - note.ext == {}".format(json.dumps(ext)) for ext in exts)
        base += """  - type: table
    name: {}
    filters:
      {}:
{}
    order:
      - file.name
      - note.repo
      - note.language
      - note.lines
""".format(json.dumps(label), "or" if len(exts) > 1 else "and", filters)
    return base


def _safe_archive_relative(name: str) -> Optional[str]:
    parts = name.split("/", 1)
    relative = parts[1] if len(parts) == 2 else parts[0]
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or ".." in pure.parts:
        return None
    return str(pure)


def build_scripts(context: Context, repos: Dict[str, dict]) -> None:
    scripts_dir = context.path("Scripts")
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir)
    scripts_dir.mkdir(parents=True)
    total = 0
    for full, repo in sorted(repos.items()):
        branch = repo["branch"] or "HEAD"
        print("• Fetching scripts from {} …".format(full))
        endpoint = "/repos/{}/tarball/{}".format(full, branch) if repo["branch"] else "/repos/{}/tarball".format(full)
        try:
            blob = gh(["api", endpoint], binary=True)
        except RuntimeError as exc:
            print("  ! skip {}: {}".format(full, str(exc).splitlines()[0]))
            continue
        try:
            archive = tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")
        except tarfile.TarError as exc:
            print("  ! bad tarball for {}: {}".format(full, exc))
            continue
        candidates = []
        for member in archive.getmembers():
            relative = _safe_archive_relative(member.name)
            if not member.isfile() or relative is None:
                continue
            ext = Path(relative).suffix.lower()
            if ext not in context.languages or any(part in EXCLUDE_DIR_PARTS for part in PurePosixPath(relative).parts):
                continue
            if member.size <= MAX_SCRIPT_BYTES:
                candidates.append((relative, ext, member))
        if len(candidates) > MAX_SCRIPTS_PER_REPO:
            print("  ! {} has {} script files (> {} cap) — truncating".format(
                full, len(candidates), MAX_SCRIPTS_PER_REPO))
            candidates = sorted(candidates, key=lambda item: item[0])[:MAX_SCRIPTS_PER_REPO]
        count = 0
        for relative, ext, member in candidates:
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            try:
                text = extracted.read().decode("utf-8")
            except UnicodeDecodeError:
                continue
            label, fence = context.languages[ext]
            url = "{}/blob/{}/{}".format(repo["url"], branch, relative)
            note = [frontmatter({"source": "obsidian-code-atlas", "tags": ["gh/script"],
                    "repo": repo["full_name"], "path": relative, "language": label,
                    "ext": ext, "lines": text.count("\n") + 1, "bytes": member.size, "url": url}),
                    "# {}".format(PurePosixPath(relative).name), "",
                    "`{}` · [`{}`]({})".format(repo["full_name"], relative, url), "",
                    code_fence(text, fence), ""]
            out = context.path("Scripts", sanitize(full), *PurePosixPath(relative + ".md").parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(note), encoding="utf-8")
            count += 1
        archive.close()
        total += count
        print("  → {} scripts".format(count))
    context.path("Scripts.base").write_text(_build_scripts_base(context.languages), encoding="utf-8")
    print("✓ Wrote {} script notes to Scripts/ and Scripts.base".format(total))


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: object, limit: int = MAX_FRONTMATTER_TEXT) -> str:
    """Flatten a GitHub-supplied string into a single safe frontmatter line."""
    text = "" if value is None else str(value)
    text = _CONTROL_CHARS.sub("", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def normalize_issue(full_name: str, issue: dict) -> dict:
    return {
        "repo": full_name, "number": int(issue["number"]),
        "title": clean_text(issue.get("title")), "state": clean_text(issue.get("state") or "open", 20),
        "author": clean_text((issue.get("user") or {}).get("login"), 100),
        "labels": [clean_text(label.get("name"), 100) for label in (issue.get("labels") or [])
                   if isinstance(label, dict) and label.get("name")],
        "assignees": [clean_text(user.get("login"), 100) for user in (issue.get("assignees") or [])
                      if isinstance(user, dict) and user.get("login")],
        "milestone": clean_text((issue.get("milestone") or {}).get("title"), 100),
        "comments": int(issue.get("comments") or 0),
        "created": iso_date(issue.get("created_at")), "updated": iso_date(issue.get("updated_at")),
        "url": clean_text(issue.get("html_url"), 500), "body": issue.get("body") or "",
    }


def fetch_issues(full_name: str, limit: int = MAX_ISSUES_PER_REPO) -> List[dict]:
    """Return open issues (pull requests excluded) for one repository."""
    issues: List[dict] = []
    page = 1
    while len(issues) < limit:
        try:
            batch = gh_json(["api", "/repos/{}/issues?state=open&per_page=100&page={}".format(full_name, page)])
        except RuntimeError as exc:
            print("  ! skip issues for {}: {}".format(full_name, str(exc).splitlines()[0]))
            return issues
        if not isinstance(batch, list) or not batch:
            break
        issues.extend(normalize_issue(full_name, item) for item in batch
                      if isinstance(item, dict) and "pull_request" not in item)
        if len(batch) < 100:
            break
        page += 1
    if len(issues) > limit:
        print("  ! {} has more than {} open issues — truncating".format(full_name, limit))
        issues = issues[:limit]
    return issues


def _build_issues_base(login: str) -> str:
    return """filters:
  and:
    - file.hasTag("gh/issue")
properties:
  note.repo:
    displayName: Repo
  note.number:
    displayName: "#"
  note.title:
    displayName: Title
  note.labels:
    displayName: Labels
  note.assignees:
    displayName: Assignees
  note.milestone:
    displayName: Milestone
  note.comments:
    displayName: 💬
  note.updated:
    displayName: Updated
  note.created:
    displayName: Created
views:
  - type: table
    name: "Recently updated"
    order:
      - note.repo
      - note.number
      - note.title
      - note.labels
      - note.comments
      - note.updated
    sort:
      - property: note.updated
        direction: DESC
  - type: table
    name: "Assigned to me"
    filters:
      and:
        - note.assignees.contains({login})
    order:
      - note.repo
      - note.number
      - note.title
      - note.labels
      - note.updated
    sort:
      - property: note.updated
        direction: DESC
  - type: table
    name: "Most discussed"
    order:
      - note.repo
      - note.number
      - note.title
      - note.comments
      - note.updated
    sort:
      - property: note.comments
        direction: DESC
  - type: table
    name: "Oldest open"
    order:
      - note.repo
      - note.number
      - note.title
      - note.milestone
      - note.created
    sort:
      - property: note.created
        direction: ASC
""".format(login=json.dumps(login))


def _issue_body(body: str) -> str:
    text = _CONTROL_CHARS.sub("", body.replace("\r\n", "\n")).strip()
    if len(text.encode("utf-8")) > MAX_ISSUE_BODY_BYTES:
        text = text.encode("utf-8")[:MAX_ISSUE_BODY_BYTES].decode("utf-8", "ignore").rstrip() + "\n\n_(truncated)_"
    return text or "_No description._"


def build_issues(context: Context, repos: Dict[str, dict], login: str) -> None:
    issues_dir = context.path("Issues")
    if issues_dir.exists():
        shutil.rmtree(issues_dir)
    issues_dir.mkdir(parents=True)
    total = 0
    for full in sorted(repos):
        print("• Fetching open issues from {} …".format(full))
        issues = fetch_issues(full)
        for issue in issues:
            fields = {
                "source": "obsidian-code-atlas", "tags": ["gh/issue"], "repo": issue["repo"],
                "number": issue["number"], "title": issue["title"], "state": issue["state"],
                "author": issue["author"], "labels": issue["labels"], "assignees": issue["assignees"],
                "milestone": issue["milestone"], "comments": issue["comments"],
                "created": issue["created"], "updated": issue["updated"], "url": issue["url"],
            }
            note = [frontmatter(fields), "# #{} {}".format(issue["number"], issue["title"]), "",
                    "`{}` · 🔗 [Open on GitHub]({})".format(issue["repo"], issue["url"]), "",
                    "- **State:** {}  ·  **Author:** {}".format(issue["state"], issue["author"] or "—"),
                    "- **Labels:** {}".format(", ".join(issue["labels"]) or "—"),
                    "- **Assignees:** {}".format(", ".join(issue["assignees"]) or "—"),
                    "- **Milestone:** {}".format(issue["milestone"] or "—"),
                    "- **Comments:** {}".format(issue["comments"]),
                    "- **Created:** {}  ·  **Updated:** {}".format(issue["created"] or "—", issue["updated"] or "—"),
                    "", "## Description", "", _issue_body(issue["body"]), ""]
            out = context.path("Issues", sanitize(full), "{}.md".format(issue["number"]))
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(note), encoding="utf-8")
        total += len(issues)
        print("  → {} open issues".format(len(issues)))
    context.path("Issues.base").write_text(_build_issues_base(login), encoding="utf-8")
    print("✓ Wrote {} issue notes to Issues/ and Issues.base".format(total))


HOME_CONTENT = """---
source: obsidian-code-atlas
---
# 🐙 GitHub Dashboard

Your GitHub activity, mirrored into this vault by Obsidian Code Atlas.

## Views

- [[Activity]] — recent commits across every repo you touch
- **Repos** — open [[Repos.base]] for the repo database (sortable / filterable)
- **Scripts** — open [[Scripts.base]] to browse & search every source file
- **Issues** — open [[Issues.base]] for every open issue across your repos

## Refresh

Regenerate everything from a terminal:

```bash
obsidian-code-atlas refresh all --output "/path/to/your/vault/Code Atlas"
```

Or refresh one section: `activity`, `repos`, `scripts`, or `issues`. Install a daily job with
`obsidian-code-atlas scheduler install --cron --output "/path/to/your/vault/Code Atlas"`
or use `--launchd` on macOS.

> The `Repos/`, `Scripts/` and `Issues/` folders and the `Activity.md` / `*.base` files are
> fully managed by the tool — edits there are overwritten on the next run.
"""


def write_home(context: Context) -> None:
    context.path("GitHub Dashboard.md").write_text(HOME_CONTENT, encoding="utf-8")
    print("✓ Wrote GitHub Dashboard.md")


def refresh(context: Context, section: str) -> int:
    try:
        login = whoami()
    except RuntimeError as exc:
        print("Cannot reach GitHub via gh: {}".format(exc), file=sys.stderr)
        return 1
    orgs = get_orgs()
    scope = {login} | orgs
    print("Authenticated as {}".format(login))
    print("Orgs in mirror scope: {}\n".format(", ".join(sorted(orgs)) or "(none)"))
    repos = get_owned_repos()
    commits = search_commits(login, COMMIT_LIMIT)
    for full in sorted(repos_from_commits(commits)):
        if full in repos:
            continue
        owner = full.split("/", 1)[0]
        if MIRROR_OWNED_AND_ORGS_ONLY and owner not in scope:
            continue
        metadata = fetch_repo_meta(full)
        if metadata:
            repos[full] = metadata
    print("→ {} repos to mirror ({} commits span more via Activity)\n".format(len(repos), len(commits)))
    if section in ("all", "activity"):
        build_activity(context, commits)
    if section in ("all", "repos"):
        build_repos(context, repos)
    if section in ("all", "scripts"):
        build_scripts(context, repos)
    if section in ("all", "issues"):
        build_issues(context, repos, login)
    if section == "all":
        write_home(context)
    print("\nDone.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obsidian-code-atlas",
                                     description="Mirror GitHub activity into Obsidian.")
    parser.add_argument("--version", action="version", version="%(prog)s {}".format(__version__))
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh_parser = subparsers.add_parser("refresh", help="refresh generated vault content")
    refresh_parser.add_argument("section", nargs="?", default="all",
                                choices=["all", "activity", "repos", "scripts", "issues"])
    refresh_parser.add_argument("--output", help="directory receiving all generated content")
    refresh_parser.add_argument("--config", help="JSON configuration file")

    scheduler_parser = subparsers.add_parser("scheduler", help="manage automatic refresh jobs")
    scheduler_subparsers = scheduler_parser.add_subparsers(dest="scheduler_command", required=True)
    install_parser = scheduler_subparsers.add_parser("install", help="install an automatic refresh job")
    install_group = install_parser.add_mutually_exclusive_group(required=True)
    install_group.add_argument("--launchd", action="store_true", help="install a macOS LaunchAgent")
    install_group.add_argument("--cron", action="store_true", help="install a cron job")
    install_parser.add_argument("--output", help="directory receiving all generated content")
    install_parser.add_argument("--config", help="JSON configuration file")
    status_parser = scheduler_subparsers.add_parser("status", help="inspect automatic refresh jobs")
    status_parser.add_argument("--output", help="directory receiving all generated content")
    uninstall_parser = scheduler_subparsers.add_parser("uninstall", help="remove automatic refresh jobs")
    uninstall_parser.add_argument("--output", help="directory receiving all generated content")

    init_parser = subparsers.add_parser("init", help="initialize an Obsidian vault for Code Atlas")
    init_parser.add_argument("vault", help="path to the Obsidian vault root")
    init_parser.add_argument("--output", default="Code Atlas",
                             help="atlas output directory name inside the vault (default: Code Atlas)")
    init_parser.add_argument("--scheduler", choices=["launchd", "cron", "none"], default="none",
                             help="install a daily scheduler (default: none)")
    init_parser.add_argument("--gitignore", dest="gitignore", action="store_true", default=True,
                             help="ignore generated output in the parent Git repository (default)")
    init_parser.add_argument("--no-gitignore", "--track-generated", dest="gitignore",
                             action="store_false",
                             help="do not add generated output to .gitignore")
    init_parser.add_argument("--force", action="store_true",
                             help="initialize even if the vault has no .obsidian directory")
    init_parser.add_argument("--no-refresh", action="store_true",
                             help="skip the initial refresh")
    init_parser.add_argument("--config", help="JSON configuration file")

    doctor_parser = subparsers.add_parser("doctor", help="diagnose Obsidian Code Atlas setup")
    doctor_parser.add_argument("--output", help="directory receiving all generated content")
    doctor_parser.add_argument("--config", help="JSON configuration file")
    return parser


def _selected_output(args: argparse.Namespace, environment: Mapping[str, str],
                     parser: argparse.ArgumentParser) -> Path:
    output_value = args.output if args.output is not None else environment.get("OBSIDIAN_CODE_ATLAS_OUTPUT")
    if not output_value:
        parser.error("output directory is required; use --output PATH or OBSIDIAN_CODE_ATLAS_OUTPUT")
    return Path(output_value).expanduser().resolve()


def _scheduler_home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home()).expanduser().resolve()


def _explicit_config_path(explicit: Optional[str], env: Mapping[str, str]) -> Optional[Path]:
    """Resolve a config path the user actually opted into (flag or env var).

    Unlike `select_config_path`, this intentionally skips the auto-discovery
    fallback inside the output directory: scheduled jobs must not bake in a
    file that was merely *found*, or deleting/renaming it later turns a soft
    default into a hard failure every night.
    """
    configured = explicit if explicit is not None else (
        env.get("OBSIDIAN_CODE_ATLAS_CONFIG") or env.get("GH_PULLER_CONFIG") or None
    )
    if configured is None:
        return None
    return Path(configured).expanduser().resolve()


def _run_scheduler(args: argparse.Namespace, output: Path, environment: Mapping[str, str],
                   parser: argparse.ArgumentParser) -> int:
    home = _scheduler_home(environment)
    try:
        if args.scheduler_command == "install":
            config = _explicit_config_path(args.config, environment)
            if config is not None and not config.is_file():
                parser.error("configuration file does not exist or is not a file: {}".format(config))
            if args.launchd:
                path = scheduler.install_launchd(output, home, config, environment=environment)
                print("Installed LaunchAgent: {}".format(path))
                print("Schedule: daily at 08:00 and when the agent loads")
            else:
                line = scheduler.install_cron(output, config, environment=environment)
                print("Installed cron job: {}".format(line))
            return 0
        if args.scheduler_command == "uninstall":
            launchd_removed = scheduler.uninstall_launchd(output, home)
            cron_removed = scheduler.uninstall_cron(output)
            print("LaunchAgent: {}".format("removed" if launchd_removed else "not installed"))
            print("Cron: {}".format("removed" if cron_removed else "not installed"))
            return 0
        launchd_entry, cron_entry = scheduler.scheduler_status(output, home)
    except scheduler.SchedulerError as exc:
        print("Scheduler error: {}".format(exc), file=sys.stderr)
        return 1
    print("Output: {}".format(output))
    print("Identifier: {}".format(scheduler.scheduler_identifier(output)))
    if launchd_entry is None:
        print("launchd: not installed")
    else:
        print("launchd: installed")
        print("  Schedule: daily at 08:00 and when the agent loads")
        print("  Command: {}".format(shlex.join(launchd_entry.get("ProgramArguments", []))))
    if cron_entry is None:
        print("cron: not installed")
    else:
        print("cron: installed")
        print("  Schedule: daily at 08:00")
        print("  Entry: {}".format(cron_entry))
    return 0 if launchd_entry is not None or cron_entry is not None else 1


def _run_init(args: argparse.Namespace, environment: Mapping[str, str],
              parser: argparse.ArgumentParser) -> int:
    from . import init
    config_path: Optional[Path] = None
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
    try:
        vault, output, info, refresh_rc = init.run_init(
            Path(args.vault).expanduser().resolve(),
            args.output,
            args.scheduler,
            track_generated=not args.gitignore,
            config_path=config_path,
            force=args.force,
            no_refresh=args.no_refresh,
            environment=environment,
        )
    except init.InitError as exc:
        parser.error(str(exc))
    except FileNotFoundError as exc:
        # The initial refresh loads configuration, which may come from
        # OBSIDIAN_CODE_ATLAS_CONFIG rather than --config; init only validates
        # the flag, so a stale env var surfaces here.
        parser.error(str(exc))
    except ManagedPathError as exc:
        print("Cannot write generated files: {}".format(exc), file=sys.stderr)
        return 1
    except scheduler.SchedulerError as exc:
        print("Scheduler error: {}".format(exc), file=sys.stderr)
        return 1

    print("Initialized Obsidian Code Atlas")
    print("  Vault:    {}".format(vault))
    print("  Output:   {}".format(output))
    print("  Config:   {}".format(info["config"] or "(none)"))
    print("  Git:      {}".format(info["git_action"]))
    print("  Scheduler: {}".format(info["scheduler"]))
    print("  Logs:     output/refresh.log (cron) or output/launchd.*.log (launchd)")
    print("  Refresh manually with:")
    print("    obsidian-code-atlas refresh all --output \"{}\"".format(output))
    return refresh_rc


def _run_doctor(args: argparse.Namespace, output: Path, environment: Mapping[str, str]) -> int:
    from . import doctor
    config_path: Optional[Path] = None
    if args.config:
        config_path = Path(args.config).expanduser().resolve()
    return doctor.run_doctor(output, config_path, environment)


def main(argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    environment = os.environ if env is None else env
    if args.command == "init":
        return _run_init(args, environment, parser)
    output = _selected_output(args, environment, parser)
    if args.command == "scheduler":
        return _run_scheduler(args, output, environment, parser)
    if args.command == "doctor":
        return _run_doctor(args, output, environment)
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        parser.error("cannot create output directory {}: {}".format(output, exc))
    if not output.is_dir():
        parser.error("output path is not a directory: {}".format(output))
    try:
        languages = load_languages(output, args.config, environment)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    context = Context(output=output, languages=languages)
    try:
        return refresh(context, args.section)
    except ManagedPathError as exc:
        print("Cannot write generated files: {}".format(exc), file=sys.stderr)
        return 1
