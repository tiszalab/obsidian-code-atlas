# Obsidian Code Atlas

Mirror your GitHub activity into an [Obsidian](https://obsidian.md/) vault.

> [!NOTE]
> **Who is this for?:** People who write code, host it on GitHub, and use [Obsidian](https://obsidian.md/) as a work, lab, or personal notebook for its "Second Brain" features.
>
> **Why `obsidian-code-atlas`?:** As a scientist, I found that I had all my laboratory notes and ideas easily searchable within an Obisidian vault. But, when I wrote code, I often found myself clunkily searching for existing scripts on GitHub in the browser and/or through my local computer's files for previously written scripts. `obsidian-code-atlas` brings code from all my project into easily searchable and visually appealling `Base` and `Note` objects in an existing Obsidian vault.

It generates three things from your `gh`-authenticated account:

1. **Activity.md** — recent commits across every repo you touch.
2. **Repos/** + **Repos.base** — one note per repo, browsable as an Obsidian Base database.
3. **Scripts/** + **Scripts.base** — every source file you’ve written, as searchable notes.

The generated `Repos/`, `Scripts/`, `Activity.md`, `GitHub Dashboard.md`, and `*.base` files are fully managed by the tool — don’t hand-edit them.

## Requirements

- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated (`gh auth status`).
- Python 3.9 or newer.

## Installation

Install the command from a local clone with pip:

```bash
git clone https://github.com/tiszalab/obsidian-code-atlas.git
cd obsidian-code-atlas
python3 -m pip install .
```

For an isolated command-line installation, use pipx:

```bash
pipx install ./obsidian-code-atlas
```

You can also install directly from the Git URL if your pip supports it:

```bash
python3 -m pip install git+https://github.com/tiszalab/obsidian-code-atlas.git
```

Upgrade an existing installation from a newer checkout:

```bash
python3 -m pip install --upgrade .
# or, for pipx:
pipx upgrade --editable obsidian-code-atlas
```

The application code lives in your Python environment; generated Obsidian notes live in the vault you choose below.

## Initializing a vault

The `init` command prepares an existing Obsidian vault to receive generated notes and optionally installs a daily scheduler.

```bash
obsidian-code-atlas init "/path/to/My Vault" --output "Code Atlas" --scheduler launchd
```

Arguments:

- `VAULT_PATH` — root of an existing Obsidian vault (the directory containing `.obsidian/`).
- `--output` — name of the generated atlas folder inside the vault (default: `Code Atlas`).
- `--scheduler` — `launchd` (macOS), `cron` (Unix), or `none` (default).
- `--gitignore` — add the generated output folder to the parent Git repository’s `.gitignore` (default).
- `--no-gitignore` (alias `--track-generated`) — opt out of the default `.gitignore` entry.
- `--force` — initialize even if `.obsidian/` is missing.
- `--no-refresh` — skip the first `refresh all`.
- `--config` — path to a custom `obsidian-code-atlas.json` configuration file.

`init` is idempotent: rerunning it updates the scheduler and Git ignore entry without duplicating them or deleting existing generated notes.

### Git integration

If your vault is inside a Git worktree, `init` finds the actual repository root (even when the vault is nested below it) and adds an anchored ignore block:

```gitignore
# BEGIN obsidian-code-atlas: <stable-id>
/path/relative/to/repo/My Vault/Code Atlas/
# END obsidian-code-atlas: <stable-id>
```

This leaves unrelated `.gitignore` content untouched. Because the whole output directory is ignored, generated notes and output-local configuration will not be tracked by the parent repository. Use `--no-gitignore` (or its alias `--track-generated`) to skip this step. When the output directory *is* the worktree root there is no rule that could ignore it, so `init` and `doctor` say so instead of writing one.

### Diagnosing the setup

```bash
obsidian-code-atlas doctor --output "/path/to/My Vault/Code Atlas"
```

`doctor` checks:

- package and Python version;
- whether the output directory exists and is writable;
- whether the output is inside an Obsidian vault;
- `gh` availability and `gh auth status`;
- effective configuration file and JSON validity;
- installed scheduler status;
- parent Git worktree and whether the output is ignored.

It exits with status `1` if a condition prevents `refresh` from working. Warnings that do not block refresh keep a zero status. `doctor` never prints tokens or credentials.

## Refreshing content

Every package invocation needs an output directory. This prevents generated files from silently going into the current working directory or package installation:

```bash
obsidian-code-atlas refresh all --output "/path/to/My Vault/Code Atlas"
obsidian-code-atlas refresh activity --output "/path/to/My Vault/Code Atlas"
obsidian-code-atlas refresh repos --output "/path/to/My Vault/Code Atlas"
obsidian-code-atlas refresh scripts --output "/path/to/My Vault/Code Atlas"
```

The section defaults to `all`, so `obsidian-code-atlas refresh --output PATH` is also valid. Paths containing spaces are supported. `OBSIDIAN_CODE_ATLAS_OUTPUT` may supply the output path, but an explicit `--output` takes precedence. The command resolves the path to an absolute path and creates it when the refresh starts.

The same interface is available through Python:

```bash
python3 -m obsidian_code_atlas --version
python3 -m obsidian_code_atlas refresh all --output "/path/to/My Vault/Code Atlas"
```

## Scheduling

Installed-package users can manage a daily 08:00 refresh without depending on repository scripts:

```bash
obsidian-code-atlas scheduler install --launchd --output "/path/to/My Vault/Code Atlas"  # macOS
obsidian-code-atlas scheduler install --cron --output "/path/to/My Vault/Code Atlas"     # Unix cron
obsidian-code-atlas scheduler status --output "/path/to/My Vault/Code Atlas"
obsidian-code-atlas scheduler uninstall --output "/path/to/My Vault/Code Atlas"
```

Pass `--config PATH` to `scheduler install` to preserve an explicit configuration path in the scheduled command. The generated job uses the absolute interpreter running Obsidian Code Atlas, `refresh all`, and the absolute output path. Different output directories receive different identifiers, so multiple vaults can coexist. `--launchd` is rejected outside macOS with a recommendation to use cron.

launchd runs both at login and daily at 08:00. Cron runs daily at 08:00. Cron output is appended to `refresh.log` in the selected output directory; launchd writes `launchd.out.log` and `launchd.err.log` there. The install command creates the output directory before scheduling the first run.

`scheduler status` prints the expected identifier, installed scheduler types, schedule, and command. It returns status 1 when neither matching job is installed and 0 when at least one is found. Uninstall removes only jobs belonging to the selected output directory.

## Configuring language support

Create `obsidian-code-atlas.json` in the output directory using this structure (repository checkouts also include `obsidian-code-atlas.json.example`):

```json
{
  "script_extensions": {
    ".ex": {"label": "Elixir", "fence": "elixir"},
    ".exs": {"label": "Elixir Script", "fence": "elixir"},
    ".yml": null
  }
}
```

A value of `null` removes an extension. Configuration is selected in this exact order:

1. explicit `--config PATH`;
2. `OBSIDIAN_CODE_ATLAS_CONFIG`;
3. legacy `GH_PULLER_CONFIG`;
4. `obsidian-code-atlas.json` in the output directory;
5. legacy `gh_puller.json` in the output directory;
6. built-in defaults.

Extension environment overrides remain compatible and are applied after the selected file:

```bash
OBSIDIAN_CODE_ATLAS_EXTENSIONS=".ex:Elixir:elixir,.exs:Elixir:elixir,-.yml" \
  obsidian-code-atlas refresh all --output "/path/to/My Vault/Code Atlas"
```

The legacy `GH_PULLER_EXTENSIONS` name is supported. Format is `.ext:Label:fence`; two parts (`.ext:Label`) and one part (`.ext`) are also accepted, and a leading `-` removes an extension.

## Migration for existing clone-inside-vault users

Older setups cloned this repository directly into an Obsidian vault and ran `gh_puller.py`, `refresh.sh`, or `setup.sh` from there. That still works as a deprecated compatibility path, but the recommended setup is now:

1. Remove any old scheduler installed by `setup.sh`:
   ```bash
   ./setup.sh --uninstall
   ```
2. Install the package from a separate checkout:
   ```bash
   git clone https://github.com/tiszalab/obsidian-code-atlas.git
   cd obsidian-code-atlas
   python3 -m pip install .
   ```
3. Initialize your vault with the new command:
   ```bash
   obsidian-code-atlas init "/path/to/My Vault" --output "Code Atlas"
   ```

The generated dashboard instructions now point to the installed package command instead of a repository checkout.

## Development-checkout compatibility

From a repository checkout, the legacy commands still write to the repository directory:

```bash
python3 gh_puller.py all
./refresh.sh all
./setup.sh --launchd
./setup.sh --cron
./setup.sh --uninstall
```

These checkout-only scheduler helpers, `crontab.example`, and `obsidian-code-atlas.plist.template` are deprecated compatibility paths. New installations should use `obsidian-code-atlas init` and `obsidian-code-atlas scheduler`; the shell files and templates will be removed in a later migration.

## File layout

```text
|.
├── pyproject.toml
├── src/obsidian_code_atlas/      # installable package
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── doctor.py
│   ├── init.py
│   └── scheduler.py
├── gh_puller.py                  # thin legacy checkout wrapper
├── refresh.sh                    # scheduler-friendly wrapper
├── setup.sh                      # launchd / cron installer (deprecated)
├── obsidian-code-atlas.plist.template
├── crontab.example
└── obsidian-code-atlas.json.example
```

## Uninstall

Remove the scheduler first, then uninstall the package. Generated notes are retained unless you delete the output folder manually.

```bash
obsidian-code-atlas scheduler uninstall --output "/path/to/My Vault/Code Atlas"
python3 -m pip uninstall obsidian-code-atlas
# or, for pipx:
pipx uninstall obsidian-code-atlas
```

For a legacy scheduled checkout, run `./setup.sh --uninstall` before deleting the checkout directory.
