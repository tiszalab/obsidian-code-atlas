import json
import os
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
            output_config = output / "obsidian-code-atlas.json"
            environment_config = output / "environment.json"
            explicit = output / "explicit.json"
            for path in (output_config, environment_config, explicit):
                path.write_text("{}", encoding="utf-8")
            environment = {"OBSIDIAN_CODE_ATLAS_CONFIG": str(environment_config)}
            self.assertEqual(cli.select_config_path(output, str(explicit), environment), explicit.resolve())
            self.assertEqual(cli.select_config_path(output, None, environment), environment_config.resolve())
            self.assertEqual(cli.select_config_path(output, None, {}), output_config)
            output_config.unlink()
            self.assertIsNone(cli.select_config_path(output, None, {}))

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
            languages = cli.load_languages(output, env={"OBSIDIAN_CODE_ATLAS_CONFIG": ""})
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

    def test_load_config_normalizes_excluded_repos(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({
                "script_extensions": {".zig": "Zig"},
                "excluded_repos": ["Alice/Private", "  org/Archive  ", ""],
            }), encoding="utf-8")
            languages, excluded_repos = cli.load_config(Path(temporary), str(config), {})
        self.assertEqual(languages[".zig"], ("Zig", "zig"))
        self.assertEqual(excluded_repos, frozenset({"alice/private", "org/archive"}))

    def test_malformed_excluded_repos_warns(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"excluded_repos": "alice/private"}), encoding="utf-8")
            with mock.patch("sys.stderr") as stderr:
                _languages, excluded_repos = cli.load_config(Path(temporary), str(config), {})
        self.assertEqual(excluded_repos, frozenset())
        self.assertIn("excluded_repos must be a JSON array",
                      "".join(call.args[0] for call in stderr.write.call_args_list))


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


def _issue(number, **overrides):
    issue = {
        "number": number, "title": "Issue {}".format(number), "state": "open",
        "user": {"login": "alice"}, "labels": [{"name": "bug"}], "assignees": [],
        "milestone": None, "comments": 0, "created_at": "2024-01-02T03:04:05Z",
        "updated_at": "2024-02-03T04:05:06Z", "html_url": "https://github.com/alice/demo/issues/{}".format(number),
        "body": "Something is broken.",
    }
    issue.update(overrides)
    return issue


class TestIssues(unittest.TestCase):
    REPOS = {"alice/demo": {"full_name": "alice/demo", "url": "https://github.com/alice/demo"}}

    def test_clean_text_flattens_and_truncates(self):
        self.assertEqual(cli.clean_text("  multi\nline\ttitle\x00 "), "multi line title")
        self.assertEqual(cli.clean_text(None), "")
        long = cli.clean_text("x" * 500, limit=10)
        self.assertEqual(len(long), 10)
        self.assertTrue(long.endswith("…"))

    def test_normalize_issue_sanitizes_every_frontmatter_field(self):
        raw = _issue(7, title="Fix: \"quotes\"\nand --- newlines",
                     labels=[{"name": "needs: triage\n"}, {"name": ""}, "junk"],
                     assignees=[{"login": "bob\x07"}], milestone={"title": "v1.0\r\n"},
                     comments="3", user=None)
        issue = cli.normalize_issue("alice/demo", raw)
        self.assertEqual(issue["title"], 'Fix: "quotes" and --- newlines')
        self.assertEqual(issue["labels"], ["needs: triage"])
        self.assertEqual(issue["assignees"], ["bob"])
        self.assertEqual(issue["milestone"], "v1.0")
        self.assertEqual(issue["comments"], 3)
        self.assertEqual(issue["author"], "")
        self.assertEqual(issue["created"], "2024-01-02")
        rendered = cli.frontmatter({"title": issue["title"], "labels": issue["labels"]})
        self.assertIn('title: "Fix: \\"quotes\\" and --- newlines"', rendered)
        self.assertNotIn("\n---\n", rendered[3:-4])

    def test_fetch_issues_paginates_and_drops_pull_requests(self):
        pages = [[_issue(n) for n in range(1, 101)],
                 [_issue(101), _issue(102, pull_request={"url": "x"})]]
        calls = []

        def fake_gh_json(args):
            calls.append(args[1])
            return pages.pop(0)

        with mock.patch.object(cli, "gh_json", side_effect=fake_gh_json):
            issues = cli.fetch_issues("alice/demo")
        self.assertEqual(len(issues), 101)
        self.assertEqual(len(calls), 2)
        self.assertIn("state=open", calls[0])
        self.assertIn("page=2", calls[1])
        self.assertNotIn(102, [issue["number"] for issue in issues])

    def test_fetch_issues_skips_repo_on_gh_failure(self):
        with mock.patch.object(cli, "gh_json", side_effect=RuntimeError("gh api failed:\nHTTP 404")), \
             mock.patch("sys.stdout"):
            self.assertEqual(cli.fetch_issues("alice/gone"), [])

    def test_build_issues_writes_numbered_notes_and_base(self):
        raw = [_issue(42, labels=[{"name": "bug"}, {"name": "help wanted"}],
                      assignees=[{"login": "alice"}], milestone={"title": "v2"}, comments=5,
                      body="---\nnot frontmatter\n---\r\nDetails here."),
               _issue(7, body="")]
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(cli, "gh_json", return_value=raw), mock.patch("sys.stdout"):
            output = Path(temporary).resolve()
            context = cli.Context(output, {})
            cli.build_issues(context, self.REPOS, "alice")
            note = (output / "Issues" / "alice-demo" / "42.md").read_text(encoding="utf-8")
            empty = (output / "Issues" / "alice-demo" / "7.md").read_text(encoding="utf-8")
            base = (output / "Issues.base").read_text(encoding="utf-8")
            names = sorted(path.name for path in (output / "Issues" / "alice-demo").iterdir())
        self.assertEqual(names, ["42.md", "7.md"])
        self.assertTrue(note.startswith("---\nsource: \"obsidian-code-atlas\"\ntags:\n  - \"gh/issue\"\n"))
        for line in ('repo: "alice/demo"', "number: 42", 'title: "Issue 42"', 'state: "open"',
                     '  - "bug"', '  - "help wanted"', '  - "alice"', 'milestone: "v2"',
                     "comments: 5", "created: 2024-01-02", "updated: 2024-02-03"):
            self.assertIn(line, note)
        self.assertIn("# #42 Issue 42", note)
        self.assertIn("## Description\n\n---\nnot frontmatter\n---\nDetails here.", note)
        self.assertNotIn("\r", note)
        self.assertIn("_No description._", empty)
        self.assertIn('file.hasTag("gh/issue")', base)
        self.assertIn('note.assignees.contains("alice")', base)
        self.assertIn('name: "Recently updated"', base)

    def test_build_issues_replaces_previous_output(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(cli, "gh_json", return_value=[]), mock.patch("sys.stdout"):
            output = Path(temporary).resolve()
            stale = output / "Issues" / "alice-demo" / "999.md"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale", encoding="utf-8")
            cli.build_issues(cli.Context(output, {}), self.REPOS, "alice")
            self.assertFalse(stale.exists())
            self.assertTrue((output / "Issues.base").is_file())


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
            "issues": {"Issues", "Issues.base"},
            "all": {"Activity.md", "Repos", "Repos.base", "Scripts", "Scripts.base",
                    "Issues", "Issues.base", "GitHub Dashboard.md"},
        }
        for section, names in expected.items():
            with self.subTest(section=section), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "output"
                self.assertEqual(self.run_refresh(output, section), 0)
                self.assertEqual({path.name for path in output.iterdir()}, names)

    def test_refresh_excludes_named_repos_from_all_outputs(self):
        included = {"full_name": "alice/included"}
        excluded = {"full_name": "Alice/Private"}
        commits = [
            {"repository": {"full_name": "alice/included"}},
            {"repository": {"full_name": "Alice/Private"}},
        ]
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(cli, "whoami", return_value="alice"), \
             mock.patch.object(cli, "get_orgs", return_value=set()), \
             mock.patch.object(cli, "get_owned_repos", return_value={
                 "alice/included": included, "Alice/Private": excluded}), \
             mock.patch.object(cli, "search_commits", return_value=commits), \
             mock.patch.object(cli, "build_activity") as build_activity, \
             mock.patch.object(cli, "build_repos") as build_repos, \
             mock.patch.object(cli, "build_scripts"), \
             mock.patch.object(cli, "build_issues"), \
             mock.patch.object(cli, "write_home"), mock.patch("sys.stdout"):
            context = cli.Context(Path(temporary).resolve(), {}, frozenset({"alice/private"}))
            self.assertEqual(cli.refresh(context, "all"), 0)
        self.assertEqual(build_activity.call_args.args[1], [commits[0]])
        self.assertEqual(build_repos.call_args.args[1], {"alice/included": included})

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

if __name__ == "__main__":
    unittest.main()
