import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = importlib.util.spec_from_file_location("gh_puller", ROOT / "gh_puller.py")
gh_puller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gh_puller)


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
        for ext in (".ts", ".tsx", ".rs", ".pl", ".go", ".rb"):
            self.assertIn(ext, gh_puller.LANG)

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
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            cfg_path = f.name

        old_env = os.environ.get("GH_PULLER_CONFIG")
        os.environ["GH_PULLER_CONFIG"] = cfg_path
        try:
            langs = gh_puller._load_languages()
            self.assertEqual(langs[".ex"], ("Elixir", "elixir"))
            self.assertNotIn(".yml", langs)
        finally:
            if old_env is None:
                os.environ.pop("GH_PULLER_CONFIG", None)
            else:
                os.environ["GH_PULLER_CONFIG"] = old_env
            Path(cfg_path).unlink()


class TestScriptsBase(unittest.TestCase):
    def test_build_scripts_base_contains_views(self):
        base = gh_puller._build_scripts_base({
            ".py": ("Python", "python"),
            ".rs": ("Rust", "rust"),
        })
        self.assertIn("All scripts", base)
        self.assertIn('name: Python', base)
        self.assertIn('note.ext == ".py"', base)
        self.assertIn('name: Rust', base)
        self.assertIn('note.ext == ".rs"', base)

    def test_build_scripts_base_avoids_duplicate_view_names(self):
        base = gh_puller._build_scripts_base({
            ".pl": ("Perl", "perl"),
            ".pm": ("Perl", "perl"),
        })
        # One gets the bare label, the second gets the extension in parentheses.
        self.assertIn("name: Perl", base)
        self.assertIn("name: Perl (.pm)", base)


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


if __name__ == "__main__":
    unittest.main()
