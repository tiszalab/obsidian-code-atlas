# Obsidian Code Atlas

Mirror your GitHub activity into an [Obsidian](https://obsidian.md/) vault.

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
python3 -m pip install ./obsidian-code-atlas
```

For an isolated command-line installation, use pipx:

```bash
pipx install ./obsidian-code-atlas
```

Development checkouts can instead use the compatibility scripts described below.

## CLI

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

## Development-checkout compatibility

From a repository checkout, the legacy command still writes to the repository directory and delegates to package code:

```bash
python3 gh_puller.py all
./refresh.sh all
```

`refresh.sh` is especially handy from `cron` or `launchd`, where `PATH` is minimal. Set `OBSIDIAN_CODE_ATLAS_PYTHON` to override its Python interpreter; the legacy `GH_PULLER_PYTHON` variable is still accepted.

## Scheduling

A checkout includes the existing scheduler helpers:

```bash
./setup.sh --launchd  # macOS
./setup.sh --cron     # Unix cron
./setup.sh --uninstall
```

The launchd and cron jobs run `refresh.sh all` every day at 08:00. See `crontab.example` and `obsidian-code-atlas.plist.template` for manual setup.

## Configuring language support

Copy `obsidian-code-atlas.json.example` into the output directory and edit its `script_extensions` map:

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

## File layout

```text
.
├── pyproject.toml
├── src/obsidian_code_atlas/      # installable package
├── gh_puller.py                  # thin legacy checkout wrapper
├── refresh.sh                    # scheduler-friendly wrapper
├── setup.sh                      # launchd / cron installer
├── obsidian-code-atlas.plist.template
├── crontab.example
└── obsidian-code-atlas.json.example
```

## Uninstall

Run `./setup.sh --uninstall` before deleting a scheduled checkout. Remove a pipx installation with `pipx uninstall obsidian-code-atlas`, or uninstall a pip installation with `python3 -m pip uninstall obsidian-code-atlas`.
