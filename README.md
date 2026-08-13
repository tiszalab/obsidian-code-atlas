# Obsidian Code Atlas

Mirror your GitHub activity into an [Obsidian](https://obsidian.md/) vault.

It generates three things from your `gh`-authenticated account:

1. **Activity.md** — recent commits across every repo you touch.
2. **Repos/** + **Repos.base** — one note per repo, browsable as an Obsidian Base database.
3. **Scripts/** + **Scripts.base** — every source file you’ve written, as searchable notes.

The generated `Repos/`, `Scripts/`, `Activity.md`, `GitHub Dashboard.md`, and `*.base` files are fully managed by the script — don’t hand-edit them.

## Requirements

- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated (`gh auth status`).
- Python 3.9 or newer.

## Quick start

1. Copy or clone this folder into your Obsidian vault:

   ```bash
   git clone https://github.com/tiszalab/obsidian-code-atlas.git
   cd obsidian-code-atlas
   ```

2. Make sure `gh` is authenticated:

   ```bash
   gh auth status
   ```

3. Run a one-off refresh:

   ```bash
   python3 gh_puller.py all
   ```

   `gh_puller.py` remains the compatibility entry point for existing installs and scripts. The repository and tool are now named **Obsidian Code Atlas**.

   Or use the wrapper script:

   ```bash
   ./refresh.sh all
   ```

The wrapper is especially handy from `cron` or `launchd`, where `PATH` is minimal.

Set `OBSIDIAN_CODE_ATLAS_PYTHON` (for example, `OBSIDIAN_CODE_ATLAS_PYTHON=/opt/homebrew/bin/python3`) to override which interpreter `refresh.sh` and `setup.sh` use. The legacy `GH_PULLER_PYTHON` variable is still accepted.

## Scheduling

### macOS launchd

```bash
./setup.sh --launchd
```

This installs a LaunchAgent that runs `refresh.sh all` every day at 08:00 and whenever you log in. Logs are written to `launchd.out.log` and `launchd.err.log` in the install directory.

### cron

```bash
./setup.sh --cron
```

This appends a daily 08:00 job to your user crontab. Logs are written to `refresh.log`.

### Manual examples

- `crontab.example` shows a cron line you can paste yourself.
- `obsidian-code-atlas.plist.template` is the launchd template used by `setup.sh`; edit and install manually if you prefer.

To remove the scheduler:

```bash
./setup.sh --uninstall
```

## Configuring language support

The script mirrors a wide set of source-file extensions by default, including TypeScript, Rust, Perl, Go, Ruby, Java, Kotlin, and more.

You can add, override, or remove extensions in two ways:

### 1. `obsidian-code-atlas.json` in the install folder

Copy `obsidian-code-atlas.json.example` to `obsidian-code-atlas.json` and edit it:

```json
{
  "script_extensions": {
    ".ex": {"label": "Elixir", "fence": "elixir"},
    ".exs": {"label": "Elixir Script", "fence": "elixir"},
    ".yml": null
  }
}
```

A value of `null` removes that extension from the default set. The `fence` is the Obsidian code-block language hint (e.g. `python`, `rust`, `typescript`). Existing `gh_puller.json` files continue to work when the new config file is absent. `OBSIDIAN_CODE_ATLAS_CONFIG` can point to a config file explicitly; the legacy `GH_PULLER_CONFIG` variable is also accepted.

### 2. `OBSIDIAN_CODE_ATLAS_EXTENSIONS` environment variable

Useful for one-off overrides or Docker:

```bash
OBSIDIAN_CODE_ATLAS_EXTENSIONS=".ex:Elixir:elixir,.exs:Elixir:elixir,-.yml" \
  python3 gh_puller.py all
```

The legacy `GH_PULLER_EXTENSIONS` variable remains supported. Format: `.ext:Label:fence`. Two parts (`.ext:Label`) are allowed; one part (`.ext`) will auto-generate a label. A leading `-` removes an extension.

## Customization

All top-level knobs in `gh_puller.py` are in one place near the top of the file:

- `COMMIT_LIMIT` — commits to pull for Activity.md.
- `MIRROR_OWNED_AND_ORGS_ONLY` — whether to mirror scripts from repos outside your own/orgs.
- `MAX_SCRIPTS_PER_REPO` — safety cap per repo.
- `EMBED_READMES`, `MAX_README_BYTES`, `MAX_SCRIPT_BYTES`, etc.

## File layout

```
.
├── gh_puller.py                  # compatibility-preserved main script
├── refresh.sh                    # scheduler-friendly wrapper
├── setup.sh                      # launchd / cron installer
├── obsidian-code-atlas.plist.template
├── crontab.example               # cron example
├── obsidian-code-atlas.json.example
├── GitHub Dashboard.md           # home note (generated on `all`, not tracked)
├── Activity.md                   # generated
├── Repos/ + Repos.base            # generated
└── Scripts/ + Scripts.base       # generated
```

## Uninstall

```bash
./setup.sh --uninstall
```

Then simply delete the `obsidian-code-atlas` folder. None of the generated files need to be preserved.
