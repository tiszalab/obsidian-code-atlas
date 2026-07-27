---
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
