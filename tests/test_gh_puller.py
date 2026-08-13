import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# The compatibility entry point builds its language map at import time from
# the environment and from the install-folder config. Both are user-owned and
# the JSON files are gitignored, so import the module with a clean environment
# — otherwise a developer's own config decides whether the suite passes.
CLEAN_ENV = {
    "OBSIDIAN_CODE_ATLAS_EXTENSIONS": "",
    "OBSIDIAN_CODE_ATLAS_CONFIG": "",
    "GH_PULLER_EXTENSIONS": "",
    "GH_PULLER_CONFIG": str(HERE / "no-such-config.json"),
}


def _import_gh_puller():
    spec = importlib.util.spec_from_file_location("gh_puller", ROOT / "gh_puller.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, CLEAN_ENV):
        spec.loader.exec_module(module)
    return module


gh_puller = _import_gh_puller()


class TestStringUtils(unittest.TestCase):
    def test_sanitize_removes_special_characters(self):
        self.assertEqual(gh_puller.sanitize("foo/bar:baz?qux"), "foo-bar-baz-qux")

    def test_sanitize_strips_leading_dot(self):
        self.assertEqual(gh_puller.sanitize(".hidden"), "hidden")

    def test_code_fence_uses_enough_backticks(self):
        content = "`````"
        fence = "``````"
        result = gh_puller.code_fence(content, "python")
        self.assertTrue(result.startswith(f"{fence}python"))
        self.assertTrue(result.endswith(fence))

    def test_frontmatter_renders_lists_and_booleans(self):
        out = gh_puller.frontmatter({
            "tags": ["foo", "bar"],
            "private": True,
            "stars": 42,
            "url": "https://example.com",
        })
        self.assertIn("tags:", out)
        self.assertIn('  - "foo"', out)
        self.assertIn("private: true", out)
        self.assertIn("stars: 42", out)
        self.assertIn('url: "https://example.com"', out)


class TestLanguageConfig(unittest.TestCase):
    def test_default_languages_include_requested_extensions(self):
        # Assert on the defaults, not the effective map: LANG reflects whatever
        # config the machine running the tests happens to have.
        defaults = gh_puller._default_languages()
        for ext in (".ts", ".tsx", ".rs", ".pl", ".go", ".rb"):
            self.assertIn(ext, defaults)

    def test_parse_env_extensions_adds_and_removes(self):
        env = ".ts:TypeScript:typescript,.rs:Rust,-.yml"
        out = gh_puller._parse_env_extensions(env)
        self.assertEqual(out[".ts"], ("TypeScript", "typescript"))
        self.assertEqual(out[".rs"], ("Rust", "rust"))
        self.assertIsNone(out[".yml"])

    def test_parse_env_extensions_one_part_defaults(self):
        out = gh_puller._parse_env_extensions(".ex")
        self.assertEqual(out[".ex"], ("EX", "ex"))

    def test_parse_languages_accepts_various_forms(self):
        d = {
            ".ex": "Elixir",
            ".exs": {"label": "Elixir Script", "fence": "elixir"},
            ".yml": None,
        }
        out = gh_puller._parse_languages(d)
        self.assertEqual(out[".ex"], ("Elixir", "elixir"))
        self.assertEqual(out[".exs"], ("Elixir Script", "elixir"))
        self.assertIsNone(out[".yml"])

    def test_load_languages_from_config_file(self):
        cfg = {
            "script_extensions": {
                ".ex": {"label": "Elixir", "fence": "elixir"},
                ".yml": None,
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "obsidian-code-atlas.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            env = dict(CLEAN_ENV, OBSIDIAN_CODE_ATLAS_CONFIG=str(cfg_path))
            with mock.patch.dict(os.environ, env):
                langs = gh_puller._load_languages()
            self.assertEqual(langs[".ex"], ("Elixir", "elixir"))
            self.assertNotIn(".yml", langs)

    def test_load_languages_accepts_legacy_config_name(self):
        cfg = {"script_extensions": {".ex": "Elixir"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "gh_puller.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            env = dict(CLEAN_ENV, GH_PULLER_CONFIG="")
            with mock.patch.object(gh_puller, "VAULT", Path(tmpdir)):
                with mock.patch.dict(os.environ, env):
                    langs = gh_puller._load_languages()
            self.assertEqual(langs[".ex"], ("Elixir", "elixir"))

    def test_load_languages_ignores_missing_config(self):
        with mock.patch.dict(os.environ, CLEAN_ENV):
            langs = gh_puller._load_languages()
        self.assertEqual(langs, gh_puller._default_languages())

    def test_env_overrides_config_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "gh_puller.json"
            cfg_path.write_text(
                json.dumps({"script_extensions": {".ex": "Elixir"}}), encoding="utf-8")
            env = dict(CLEAN_ENV,
                       GH_PULLER_CONFIG=str(cfg_path),
                       GH_PULLER_EXTENSIONS="-.ex,.zig:Zig:zig")
            with mock.patch.dict(os.environ, env):
                langs = gh_puller._load_languages()
        self.assertNotIn(".ex", langs)
        self.assertEqual(langs[".zig"], ("Zig", "zig"))

    def test_new_env_name_takes_precedence(self):
        env = dict(
            CLEAN_ENV,
            OBSIDIAN_CODE_ATLAS_EXTENSIONS=".zig:Zig:zig",
            GH_PULLER_EXTENSIONS=".ex:Elixir:elixir",
        )
        with mock.patch.dict(os.environ, env):
            langs = gh_puller._load_languages()
        self.assertIn(".zig", langs)
        self.assertNotIn(".ex", langs)

    def test_default_languages_drops_data_and_iac_extensions(self):
        defaults = gh_puller._default_languages()
        for ext in (".yaml", ".yml", ".sql", ".tf", ".tfvars", ".h"):
            self.assertNotIn(ext, defaults)

    def test_default_languages_m_is_matlab(self):
        self.assertEqual(gh_puller._default_languages()[".m"], ("MATLAB", "matlab"))

    def test_parse_languages_one_element_list(self):
        out = gh_puller._parse_languages({".ex": ["Elixir"]})
        self.assertEqual(out[".ex"], ("Elixir", "elixir"))

    def test_parse_env_extensions_ignores_bare_dash(self):
        out = gh_puller._parse_env_extensions("-.py,-")
        self.assertIsNone(out[".py"])
        self.assertNotIn(".-", out)


class TestScriptsBase(unittest.TestCase):
    def test_build_scripts_base_contains_views(self):
        base = gh_puller._build_scripts_base({
            ".py": ("Python", "python"),
            ".rs": ("Rust", "rust"),
        })
        self.assertIn("All scripts", base)
        self.assertIn('name: "Python"', base)
        self.assertIn('note.ext == ".py"', base)
        self.assertIn('name: "Rust"', base)
        self.assertIn('note.ext == ".rs"', base)

    def test_build_scripts_base_groups_languages(self):
        base = gh_puller._build_scripts_base({
            ".pl": ("Perl", "perl"),
            ".pm": ("Perl", "perl"),
        })
        # A single view per language, with an "or" filter across its extensions.
        self.assertIn('name: "Perl"', base)
        self.assertIn('note.ext == ".pl"', base)
        self.assertIn('note.ext == ".pm"', base)
        self.assertIn("or:", base)
        # No (.ext) disambiguation suffix is needed.
        self.assertNotIn("name: Perl (", base)

    def test_build_scripts_base_quotes_view_names(self):
        base = gh_puller._build_scripts_base({
            ".ex": ("Elixir: Foo", "elixir"),
        })
        self.assertIn('name: "Elixir: Foo"', base)


class TestShellScripts(unittest.TestCase):
    def test_refresh_sh_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "refresh.sh")],
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_setup_sh_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "setup.sh")],
            check=False,
        )
        self.assertEqual(result.returncode, 0)


FAKE_CRONTAB = """#!/bin/bash
# Stand-in for crontab(1): reads and writes $CRONTAB_STATE instead of the
# real user crontab.
if [ "$1" = "-l" ]; then
    [ -s "$CRONTAB_STATE" ] || exit 1
    cat "$CRONTAB_STATE"
else
    cat "$1" > "$CRONTAB_STATE"
fi
"""


class TestCronInstaller(unittest.TestCase):
    """setup.sh --cron / --uninstall against a fake crontab binary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        bindir = tmpdir / "bin"
        bindir.mkdir()
        fake = bindir / "crontab"
        fake.write_text(FAKE_CRONTAB, encoding="utf-8")
        fake.chmod(0o755)

        self.state = tmpdir / "crontab.state"
        self.state.write_text("", encoding="utf-8")
        self.env = dict(
            os.environ,
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            CRONTAB_STATE=str(self.state),
        )

    def run_setup(self, *args):
        result = subprocess.run(
            ["bash", str(ROOT / "setup.sh"), *args],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.state.read_text(encoding="utf-8")

    def test_install_is_idempotent(self):
        self.run_setup("--cron")
        crontab = self.run_setup("--cron")
        job_lines = [ln for ln in crontab.splitlines() if "refresh.sh" in ln]
        self.assertEqual(len(job_lines), 1, crontab)
        self.assertIn("# obsidian-code-atlas auto-refresh", crontab)

    def test_uninstall_removes_the_job_not_just_the_marker(self):
        self.run_setup("--cron")
        crontab = self.run_setup("--uninstall")
        self.assertNotIn("refresh.sh", crontab)
        self.assertNotIn("gh_puller", crontab)
        self.assertNotIn("obsidian-code-atlas", crontab)

    def test_uninstall_removes_legacy_two_line_entries(self):
        self.state.write_text(
            "# gh_puller auto-refresh\n"
            '0 8 * * * "/old/gh_puller/refresh.sh" all >> '
            '"/old/gh_puller/refresh.log" 2>&1\n',
            encoding="utf-8",
        )
        crontab = self.run_setup("--uninstall")
        self.assertNotIn("refresh.sh", crontab)

    def test_unrelated_entries_are_preserved(self):
        self.state.write_text("*/5 * * * * /usr/bin/true\n", encoding="utf-8")
        self.run_setup("--cron")
        crontab = self.run_setup("--uninstall")
        self.assertIn("*/5 * * * * /usr/bin/true", crontab)


if __name__ == "__main__":
    unittest.main()
