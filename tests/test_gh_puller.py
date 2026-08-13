import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_code_atlas import __version__
from obsidian_code_atlas import cli


class TestStringUtils(unittest.TestCase):
    def test_sanitize_removes_special_characters(self):
        self.assertEqual(cli.sanitize("foo/bar:baz?qux"), "foo-bar-baz-qux")

    def test_sanitize_strips_leading_dot(self):
        self.assertEqual(cli.sanitize(".hidden"), "hidden")

    def test_code_fence_uses_enough_backticks(self):
        result = cli.code_fence("`````", "python")
        self.assertTrue(result.startswith("``````python"))
        self.assertTrue(result.endswith("``````"))

    def test_frontmatter_renders_lists_and_booleans(self):
        out = cli.frontmatter({"tags": ["foo"], "private": True, "stars": 42})
        self.assertIn('  - "foo"', out)
        self.assertIn("private: true", out)
        self.assertIn("stars: 42", out)


class TestLanguageConfig(unittest.TestCase):
    def test_defaults_include_source_languages_not_data_extensions(self):
        defaults = cli._default_languages()
        for ext in (".ts", ".tsx", ".rs", ".pl", ".go", ".rb"):
            self.assertIn(ext, defaults)
        for ext in (".yaml", ".yml", ".sql", ".tf", ".tfvars", ".h"):
            self.assertNotIn(ext, defaults)
        self.assertEqual(defaults[".m"], ("MATLAB", "matlab"))

    def test_extension_environment_adds_and_removes(self):
        parsed = cli._parse_env_extensions(".ex:Elixir:elixir,.rs:Rust,-.py,-")
        self.assertEqual(parsed[".ex"], ("Elixir", "elixir"))
        self.assertEqual(parsed[".rs"], ("Rust", "rust"))
        self.assertIsNone(parsed[".py"])
        self.assertNotIn(".-", parsed)

    def test_parse_languages_accepts_supported_forms(self):
        parsed = cli._parse_languages({
            ".ex": "Elixir",
            ".exs": {"label": "Elixir Script", "fence": "elixir"},
            ".eex": ["Elixir Template"],
            ".yml": None,
        })
        self.assertEqual(parsed[".ex"], ("Elixir", "elixir"))
        self.assertEqual(parsed[".exs"], ("Elixir Script", "elixir"))
        self.assertEqual(parsed[".eex"], ("Elixir Template", "elixir template"))
        self.assertIsNone(parsed[".yml"])

    def test_config_precedence_is_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            output_new = output / "obsidian-code-atlas.json"
            output_old = output / "gh_puller.json"
            env_new = output / "env-new.json"
            env_old = output / "env-old.json"
            explicit = output / "explicit.json"
            for path in (output_new, output_old, env_new, env_old, explicit):
                path.write_text("{}", encoding="utf-8")
            environment = {
                "OBSIDIAN_CODE_ATLAS_CONFIG": str(env_new),
                "GH_PULLER_CONFIG": str(env_old),
            }
            self.assertEqual(cli.select_config_path(output, str(explicit), environment), explicit.resolve())
            self.assertEqual(cli.select_config_path(output, None, environment), env_new.resolve())
            self.assertEqual(cli.select_config_path(output, None, {"GH_PULLER_CONFIG": str(env_old)}), env_old.resolve())
            self.assertEqual(cli.select_config_path(output, None, {}), output_new)
            output_new.unlink()
            self.assertEqual(cli.select_config_path(output, None, {}), output_old)
            output_old.unlink()
            self.assertIsNone(cli.select_config_path(output, None, {}))

    def test_load_languages_uses_legacy_output_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "gh_puller.json").write_text(
                json.dumps({"script_extensions": {".ex": "Elixir"}}), encoding="utf-8")
            self.assertEqual(cli.load_languages(output, env={})[".ex"], ("Elixir", "elixir"))

    def test_load_languages_rejects_missing_selected_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            with self.assertRaisesRegex(FileNotFoundError, "configuration file"):
                cli.load_languages(Path(temporary), str(missing), {})

    def test_empty_config_environment_uses_output_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "obsidian-code-atlas.json").write_text(
                json.dumps({"script_extensions": {".ex": "Elixir"}}), encoding="utf-8")
            languages = cli.load_languages(output, env={
                "OBSIDIAN_CODE_ATLAS_CONFIG": "",
                "GH_PULLER_CONFIG": "",
            })
        self.assertEqual(languages[".ex"], ("Elixir", "elixir"))

    def test_malformed_script_extensions_warns(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            config = output / "config.json"
            config.write_text(json.dumps({"script_extensions": []}), encoding="utf-8")
            with mock.patch("sys.stderr") as stderr:
                languages = cli.load_languages(output, str(config), {})
        self.assertEqual(languages, cli._default_languages())
        self.assertIn("script_extensions must be a JSON object",
                      "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_extension_env_overrides_selected_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            config = output / "config.json"
            config.write_text(json.dumps({"script_extensions": {".ex": "Elixir"}}), encoding="utf-8")
            langs = cli.load_languages(output, str(config), {
                "OBSIDIAN_CODE_ATLAS_EXTENSIONS": "-.ex,.zig:Zig:zig",
            })
            self.assertNotIn(".ex", langs)
            self.assertEqual(langs[".zig"], ("Zig", "zig"))

    def test_new_extension_env_name_precedes_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            langs = cli.load_languages(Path(temporary), env={
                "OBSIDIAN_CODE_ATLAS_EXTENSIONS": ".zig:Zig:zig",
                "GH_PULLER_EXTENSIONS": ".ex:Elixir:elixir",
            })
        self.assertIn(".zig", langs)
        self.assertNotIn(".ex", langs)


class TestScriptsBase(unittest.TestCase):
    def test_views_group_extensions_by_language(self):
        base = cli._build_scripts_base({
            ".py": ("Python", "python"),
            ".pl": ("Perl", "perl"),
            ".pm": ("Perl", "perl"),
        })
        self.assertIn('name: "All scripts"', base)
        self.assertIn('name: "Python"', base)
        self.assertIn('name: "Perl"', base)
        self.assertIn('note.ext == ".pl"', base)
        self.assertIn('note.ext == ".pm"', base)
        self.assertIn("or:", base)

    def test_view_names_are_quoted(self):
        base = cli._build_scripts_base({".ex": ("Elixir: Foo", "elixir")})
        self.assertIn('name: "Elixir: Foo"', base)


class TestCLI(unittest.TestCase):
    def run_refresh(self, output, section="all", environment=None):
        with mock.patch.object(cli, "whoami", return_value="alice"), \
             mock.patch.object(cli, "get_orgs", return_value=set()), \
             mock.patch.object(cli, "get_owned_repos", return_value={}), \
             mock.patch.object(cli, "search_commits", return_value=[]):
            return cli.main(["refresh", section, "--output", str(output)], env=environment or {})

    def test_each_section_only_writes_its_managed_outputs(self):
        expected = {
            "activity": {"Activity.md"},
            "repos": {"Repos", "Repos.base"},
            "scripts": {"Scripts", "Scripts.base"},
            "all": {"Activity.md", "Repos", "Repos.base", "Scripts", "Scripts.base", "GitHub Dashboard.md"},
        }
        for section, names in expected.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output"
                self.assertEqual(self.run_refresh(output, section), 0)
                self.assertEqual({path.name for path in output.iterdir()}, names)

    def test_section_defaults_to_all_and_stays_under_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "selected output"
            with mock.patch.object(cli, "refresh", return_value=0) as refresh:
                self.assertEqual(cli.main(["refresh", "--output", str(output)], env={}), 0)
            context, section = refresh.call_args.args
            self.assertEqual(section, "all")
            self.assertEqual(context.output, output.resolve())
            self.assertFalse((root / "Activity.md").exists())
            self.assertFalse((SRC / "obsidian_code_atlas" / "Activity.md").exists())

    def test_missing_output_is_clear_nonzero(self):
        with self.assertRaises(SystemExit) as raised, \
             mock.patch("sys.stderr") as stderr:
            cli.main(["refresh", "activity"], env={})
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("output directory is required", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_missing_explicit_config_is_clear_nonzero(self):
        with tempfile.TemporaryDirectory() as temporary, \
             self.assertRaises(SystemExit) as raised, \
             mock.patch("sys.stderr") as stderr:
            cli.main(["refresh", "activity", "--output", temporary,
                      "--config", str(Path(temporary) / "missing.json")], env={})
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("configuration file does not exist",
                      "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_explicit_output_precedes_environment_and_supports_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            explicit = Path(temporary) / "explicit output with spaces"
            environment = Path(temporary) / "environment output"
            self.run_refresh(explicit, "activity", {"OBSIDIAN_CODE_ATLAS_OUTPUT": str(environment)})
            self.assertTrue((explicit / "Activity.md").is_file())
            self.assertFalse(environment.exists())
            self.assertFalse((ROOT / "Activity.md").exists())
            self.assertFalse((SRC / "obsidian_code_atlas" / "Activity.md").exists())

    def test_output_environment_is_used(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "from environment"
            with mock.patch.object(cli, "whoami", return_value="alice"), \
                 mock.patch.object(cli, "get_orgs", return_value=set()), \
                 mock.patch.object(cli, "get_owned_repos", return_value={}), \
                 mock.patch.object(cli, "search_commits", return_value=[]):
                cli.main(["refresh", "activity"], env={"OBSIDIAN_CODE_ATLAS_OUTPUT": str(output)})
            self.assertTrue((output / "Activity.md").is_file())

    def test_context_rejects_paths_outside_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = cli.Context(Path(temporary).resolve(), {})
            with self.assertRaisesRegex(ValueError, "escapes output"):
                context.path("..", "escaped.md")
        self.assertIsNone(cli._safe_archive_relative("root/../escaped.py"))
        self.assertIsNone(cli._safe_archive_relative("root//absolute.py"))

    def test_symlinked_managed_directory_fails_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            output.mkdir()
            (output / "Repos").symlink_to(root / "elsewhere", target_is_directory=True)
            with mock.patch.object(cli, "whoami", return_value="alice"), \
                 mock.patch.object(cli, "get_orgs", return_value=set()), \
                 mock.patch.object(cli, "get_owned_repos", return_value={}), \
                 mock.patch.object(cli, "search_commits", return_value=[]), \
                 mock.patch("sys.stderr") as stderr:
                result = cli.main(["refresh", "repos", "--output", str(output)], env={})
        self.assertEqual(result, 1)
        self.assertIn("Cannot write generated files",
                      "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_no_configuration_leaks_between_invocations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_config = root / "first.json"
            first_config.write_text(json.dumps({"script_extensions": {".zig": "Zig"}}), encoding="utf-8")
            seen = []
            with mock.patch.object(cli, "refresh", side_effect=lambda context, section: seen.append(context.languages) or 0):
                cli.main(["refresh", "scripts", "--output", str(root / "one"), "--config", str(first_config)], env={})
                cli.main(["refresh", "scripts", "--output", str(root / "two")], env={})
            self.assertIn(".zig", seen[0])
            self.assertNotIn(".zig", seen[1])

    def test_generated_dashboard_advertises_package_command(self):
        self.assertIn("obsidian-code-atlas refresh all --output", cli.HOME_CONTENT)


class TestEntrypoints(unittest.TestCase):
    def module(self, *arguments):
        environment = dict(os.environ, PYTHONPATH=str(SRC))
        return subprocess.run([sys.executable, "-m", "obsidian_code_atlas", *arguments],
                              env=environment, capture_output=True, text=True, check=False)

    def test_module_help_and_version(self):
        help_result = self.module("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("refresh", help_result.stdout)
        version_result = self.module("--version")
        self.assertEqual(version_result.returncode, 0, version_result.stderr)
        self.assertIn(__version__, version_result.stdout)

    def test_legacy_wrapper_uses_repository_directory_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout with spaces"
            checkout.mkdir()
            shutil.copy(ROOT / "gh_puller.py", checkout / "gh_puller.py")
            shutil.copytree(SRC, checkout / "src")
            fake_bin = Path(temporary) / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text("""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:2] == ["api", "/user"]:
    print("alice")
elif args[:2] == ["api", "/user/orgs"]:
    pass
elif args[:2] == ["repo", "list"]:
    print("[]")
elif args and args[0] == "api" and any("/search/commits" in arg for arg in args):
    print(json.dumps({"items": [], "total_count": 0}))
else:
    print("unexpected gh arguments: " + repr(args), file=sys.stderr)
    raise SystemExit(1)
""", encoding="utf-8")
            fake_gh.chmod(0o755)
            environment = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
            result = subprocess.run([sys.executable, str(checkout / "gh_puller.py"), "all"],
                                    cwd=temporary, env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((checkout / "Activity.md").is_file())
            self.assertTrue((checkout / "GitHub Dashboard.md").is_file())
            self.assertFalse((Path(temporary) / "Activity.md").exists())

            config = Path(temporary) / "custom config.json"
            config.write_text(
                json.dumps({"script_extensions": {".ex": "Elixir"}}), encoding="utf-8")
            configured = subprocess.run(
                [sys.executable, str(checkout / "gh_puller.py"), "scripts",
                 "--config", str(config)], cwd=temporary, env=environment,
                capture_output=True, text=True, check=False)
            self.assertEqual(configured.returncode, 0, configured.stderr)
            self.assertIn('name: "Elixir"',
                          (checkout / "Scripts.base").read_text(encoding="utf-8"))

    def test_legacy_wrapper_requires_a_section(self):
        result = subprocess.run([sys.executable, str(ROOT / "gh_puller.py")],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("required: command", result.stderr)


class TestShellScripts(unittest.TestCase):
    def test_shell_script_syntax(self):
        for name in ("refresh.sh", "setup.sh"):
            with self.subTest(name=name):
                result = subprocess.run(["bash", "-n", str(ROOT / name)], check=False)
                self.assertEqual(result.returncode, 0)


FAKE_CRONTAB = """#!/bin/bash
if [ "$1" = "-l" ]; then
    [ -s "$CRONTAB_STATE" ] || exit 1
    cat "$CRONTAB_STATE"
else
    cat "$1" > "$CRONTAB_STATE"
fi
"""


class TestCronInstaller(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        binary = root / "bin"
        binary.mkdir()
        crontab = binary / "crontab"
        crontab.write_text(FAKE_CRONTAB, encoding="utf-8")
        crontab.chmod(0o755)
        self.state = root / "crontab.state"
        self.state.write_text("", encoding="utf-8")
        self.environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                                CRONTAB_STATE=str(self.state))

    def run_setup(self, *arguments):
        result = subprocess.run(["bash", str(ROOT / "setup.sh"), *arguments], env=self.environment,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.state.read_text(encoding="utf-8")

    def test_install_is_idempotent_and_uninstall_preserves_unrelated_entries(self):
        self.state.write_text("*/5 * * * * /usr/bin/true\n", encoding="utf-8")
        self.run_setup("--cron")
        installed = self.run_setup("--cron")
        self.assertEqual(sum("refresh.sh" in line for line in installed.splitlines()), 1)
        self.assertIn("# obsidian-code-atlas auto-refresh", installed)
        removed = self.run_setup("--uninstall")
        self.assertNotIn("refresh.sh", removed)
        self.assertNotIn("gh_puller", removed)
        self.assertNotIn("obsidian-code-atlas", removed)
        self.assertIn("*/5 * * * * /usr/bin/true", removed)

    def test_uninstall_removes_legacy_entry(self):
        self.state.write_text("# gh_puller auto-refresh\n0 8 * * * /old/refresh.sh all\n", encoding="utf-8")
        removed = self.run_setup("--uninstall")
        self.assertNotIn("refresh.sh", removed)


if __name__ == "__main__":
    unittest.main()
