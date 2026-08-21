import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from obsidian_code_atlas import cli, init, scheduler


FAKE_GH_AUTHENTICATED = """#!/usr/bin/env python3
import sys
if sys.argv[1:] == ["auth", "status"]:
    sys.exit(0)
sys.exit(1)
"""

FAKE_GH_UNAUTHENTICATED = """#!/usr/bin/env python3
import sys
if sys.argv[1:] == ["auth", "status"]:
    print("not logged in", file=sys.stderr)
    sys.exit(1)
sys.exit(1)
"""


class TestInitHelpers(unittest.TestCase):
    def test_resolve_output_rejects_path_outside_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "vault"
            vault.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(init.InitError, "inside the vault"):
                init.resolve_output_path(vault, str(outside))

    def test_resolve_output_joins_relative_name_to_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary) / "My Vault"
            vault.mkdir()
            output = init.resolve_output_path(vault, "Code Atlas")
            self.assertEqual(output, (vault / "Code Atlas").resolve())


class TestInitCommand(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {"HOME": str(self.root / "home"), "PATH": os.environ.get("PATH", "")}

    def _make_fake_gh(self, script=FAKE_GH_AUTHENTICATED):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(script, encoding="utf-8")
        fake_gh.chmod(0o755)
        self.environment["PATH"] = str(fake_bin) + os.pathsep + self.environment["PATH"]
        return fake_bin

    def _make_vault(self, name="My Vault"):
        vault = self.root / name
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        return vault

    def test_init_refuses_missing_obsidian_directory(self):
        vault = self.root / "Not a Vault"
        vault.mkdir()
        self._make_fake_gh()
        with self.assertRaisesRegex(init.InitError, ".obsidian"):
            init.run_init(vault, "Code Atlas", "none", False, None, False, True, self.environment)

    def test_init_accepts_missing_obsidian_with_force(self):
        vault = self.root / "Not a Vault"
        vault.mkdir()
        self._make_fake_gh()
        _, output, info, rc = init.run_init(
            vault, "Code Atlas", "none", False, None, True, True, self.environment
        )
        self.assertEqual(rc, 0)
        self.assertEqual(output, (vault / "Code Atlas").resolve())

    def test_init_creates_output_directory(self):
        vault = self._make_vault()
        self._make_fake_gh()
        _, output, info, rc = init.run_init(
            vault, "Code Atlas", "none", False, None, False, True, self.environment
        )
        self.assertEqual(rc, 0)
        self.assertTrue(output.is_dir())

    def test_init_idempotent_does_not_duplicate_output(self):
        vault = self._make_vault()
        self._make_fake_gh()
        init.run_init(vault, "Code Atlas", "none", False, None, False, True, self.environment)
        marker = (vault / "Code Atlas" / "keep.txt")
        marker.write_text("data", encoding="utf-8")
        init.run_init(vault, "Code Atlas", "none", False, None, False, True, self.environment)
        self.assertEqual(marker.read_text(encoding="utf-8"), "data")

    def test_init_supports_paths_with_spaces(self):
        vault = self._make_vault("My 100% Vault")
        self._make_fake_gh()
        _, output, _, _ = init.run_init(
            vault, "Code Atlas", "none", False, None, False, True, self.environment
        )
        self.assertTrue(output.is_dir())
        self.assertIn("My 100% Vault", str(output))

    def test_init_refuses_missing_gh(self):
        vault = self._make_vault()
        self.environment["PATH"] = ""
        with self.assertRaisesRegex(init.InitError, "not on PATH"):
            init.run_init(vault, "Code Atlas", "none", False, None, False, True, self.environment)

    def test_init_refuses_unauthenticated_gh(self):
        vault = self._make_vault()
        self._make_fake_gh(FAKE_GH_UNAUTHENTICATED)
        with self.assertRaisesRegex(init.InitError, "not authenticated"):
            init.run_init(vault, "Code Atlas", "none", False, None, False, True, self.environment)

    def test_init_refuses_missing_explicit_config(self):
        vault = self._make_vault()
        self._make_fake_gh()
        missing = self.root / "missing.json"
        with self.assertRaisesRegex(init.InitError, "configuration file does not exist"):
            init.run_init(vault, "Code Atlas", "none", False, missing, False, True, self.environment)


class TestGitignoreIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {"HOME": str(self.root / "home"), "PATH": os.environ.get("PATH", "")}
        self._make_fake_gh()

    def _make_fake_gh(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(FAKE_GH_AUTHENTICATED, encoding="utf-8")
        fake_gh.chmod(0o755)
        self.environment["PATH"] = str(fake_bin) + os.pathsep + self.environment["PATH"]

    def _git_init(self, path):
        result = subprocess.run(
            ["git", "init", "-q", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise unittest.SkipTest("git is not available: {}".format(result.stderr))

    def _run_init(self, vault, output="Code Atlas", track=False):
        return init.run_init(vault, output, "none", track, None, False, True, self.environment)

    def test_gitignore_added_when_vault_is_repo_root(self):
        vault = self.root / "My Vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        self._git_init(vault)
        self._run_init(vault)
        gitignore = vault / ".gitignore"
        text = gitignore.read_text(encoding="utf-8")
        self.assertIn("/Code Atlas/", text)
        self.assertIn("BEGIN obsidian-code-atlas:", text)
        self.assertIn("END obsidian-code-atlas:", text)

    def test_gitignore_uses_relative_path_when_repo_above_vault(self):
        repo = self.root / "repo"
        repo.mkdir()
        self._git_init(repo)
        vault = repo / "notes" / "My Vault"
        vault.mkdir(parents=True)
        (vault / ".obsidian").mkdir()
        self._run_init(vault)
        text = (repo / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/notes/My Vault/Code Atlas/", text)

    def test_gitignore_idempotent_across_reruns(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        self._git_init(vault)
        self._run_init(vault)
        self._run_init(vault)
        text = (vault / ".gitignore").read_text(encoding="utf-8")
        begin = text.count("BEGIN obsidian-code-atlas:")
        self.assertEqual(begin, 1)
        self.assertEqual(text.count("/Code Atlas/"), 1)

    def test_gitignore_preserves_unrelated_content(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        self._git_init(vault)
        original = "*.log\nsecrets.env\n"
        (vault / ".gitignore").write_text(original, encoding="utf-8")
        self._run_init(vault)
        text = (vault / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("*.log", text)
        self.assertIn("secrets.env", text)

    def test_existing_equivalent_rule_prevents_edit(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        self._git_init(vault)
        (vault / ".gitignore").write_text("/Code Atlas/\n", encoding="utf-8")
        _, _, info, _ = self._run_init(vault)
        self.assertIn("equivalent existing ignore rule", info["git_action"])
        text = (vault / ".gitignore").read_text(encoding="utf-8")
        self.assertNotIn("BEGIN obsidian-code-atlas", text)

    def test_track_generated_skips_gitignore(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        self._git_init(vault)
        _, _, info, _ = self._run_init(vault, track=True)
        self.assertEqual(info["git_action"], "no parent Git repository")
        self.assertFalse((vault / ".gitignore").exists())

    def test_negated_or_lookalike_rules_do_not_count_as_ignored(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        self._git_init(vault)
        (vault / ".gitignore").write_text(
            "/Code Atlas Backup/\n# /Code Atlas/\n!/Code Atlas/\n", encoding="utf-8")
        _, _, info, _ = self._run_init(vault)
        text = (vault / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("BEGIN obsidian-code-atlas:", text)
        self.assertIn("added managed block", info["git_action"])

    def test_output_at_worktree_root_is_reported_not_written_as_dot(self):
        repo = self.root / "atlas"
        repo.mkdir()
        (repo / ".obsidian").mkdir()
        self._git_init(repo)
        # --output "." puts the atlas at the worktree root, where no ignore
        # rule could apply.
        _, _, info, _ = self._run_init(repo, output=".")
        gitignore = repo / ".gitignore"
        if gitignore.is_file():
            self.assertNotIn("/./", gitignore.read_text(encoding="utf-8"))
        self.assertIn("worktree root", info["git_action"])

    def test_no_git_repository_does_nothing(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        _, _, info, _ = self._run_init(vault)
        self.assertEqual(info["git_action"], "no parent Git repository")
        self.assertFalse((vault / ".gitignore").exists())


class TestSchedulerDelegation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {"HOME": str(self.root / "home"), "PATH": os.environ.get("PATH", "")}
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(FAKE_GH_AUTHENTICATED, encoding="utf-8")
        fake_gh.chmod(0o755)
        self.environment["PATH"] = str(fake_bin) + os.pathsep + self.environment["PATH"]

    def test_init_delegates_launchd_install(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        with mock.patch.object(scheduler, "install_launchd") as install_launchd, \
             mock.patch.object(cli, "refresh", return_value=0):
            init.run_init(vault, "Code Atlas", "launchd", False, None, False, True, self.environment)
        self.assertTrue(install_launchd.called)
        call_args = install_launchd.call_args
        self.assertEqual(call_args.args[0], (vault / "Code Atlas").resolve())

    def test_init_delegates_cron_install(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        with mock.patch.object(scheduler, "install_cron") as install_cron, \
             mock.patch.object(cli, "refresh", return_value=0):
            init.run_init(vault, "Code Atlas", "cron", False, None, False, True, self.environment)
        self.assertTrue(install_cron.called)
        call_args = install_cron.call_args
        self.assertEqual(call_args.args[0], (vault / "Code Atlas").resolve())

    def test_init_passes_config_to_scheduler(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        config = (self.root / "config.json").resolve()
        config.write_text("{}", encoding="utf-8")
        with mock.patch.object(scheduler, "install_cron") as install_cron, \
             mock.patch.object(cli, "refresh", return_value=0):
            init.run_init(vault, "Code Atlas", "cron", False, config, False, True, self.environment)
        self.assertEqual(install_cron.call_args.args[1], config)


class TestInitRefreshFailure(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(FAKE_GH_AUTHENTICATED, encoding="utf-8")
        fake_gh.chmod(0o755)
        self.environment = {
            "HOME": str(self.root / "home"),
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        }
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / ".obsidian").mkdir()

    def test_failed_refresh_skips_scheduler_but_explains_why(self):
        with mock.patch.object(cli, "load_languages", return_value={}), \
             mock.patch.object(cli, "refresh", return_value=1), \
             mock.patch.object(scheduler, "install_launchd") as install_launchd:
            _, _, info, rc = init.run_init(
                self.vault, "Code Atlas", "launchd", False, None, False, False, self.environment)
        self.assertEqual(rc, 1)
        self.assertFalse(install_launchd.called)
        self.assertNotEqual(info["scheduler"], "none")
        self.assertIn("initial refresh failed", info["scheduler"])

    def test_missing_config_from_environment_is_a_clean_error(self):
        environment = dict(self.environment,
                           OBSIDIAN_CODE_ATLAS_CONFIG=str(self.root / "gone.json"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            cli.main(["init", str(self.vault), "--scheduler", "none"], env=environment)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("gone.json", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_unwritable_generated_path_is_a_clean_error(self):
        stderr = io.StringIO()
        with mock.patch.object(cli, "load_languages", return_value={}), \
             mock.patch.object(cli, "refresh",
                               side_effect=cli.ManagedPathError("escapes output directory")), \
             contextlib.redirect_stderr(stderr):
            rc = cli.main(["init", str(self.vault), "--scheduler", "none"], env=self.environment)
        self.assertEqual(rc, 1)
        self.assertIn("Cannot write generated files", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class TestDoctorCommand(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {"HOME": str(self.root / "home"), "PATH": os.environ.get("PATH", "")}

    def _make_fake_gh(self, script=FAKE_GH_AUTHENTICATED):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(script, encoding="utf-8")
        fake_gh.chmod(0o755)
        self.environment["PATH"] = str(fake_bin) + os.pathsep + self.environment["PATH"]

    def test_doctor_passes_for_valid_output(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        output = vault / "Code Atlas"
        output.mkdir()
        self._make_fake_gh()
        from obsidian_code_atlas import doctor
        rc = doctor.run_doctor(output, None, self.environment)
        self.assertEqual(rc, 0)

    def test_doctor_fails_when_output_missing(self):
        self._make_fake_gh()
        from obsidian_code_atlas import doctor
        rc = doctor.run_doctor(self.root / "missing", None, self.environment)
        self.assertEqual(rc, 1)

    def test_doctor_fails_when_gh_missing(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        output = vault / "Code Atlas"
        output.mkdir()
        self.environment["PATH"] = ""
        from obsidian_code_atlas import doctor
        rc = doctor.run_doctor(output, None, self.environment)
        self.assertEqual(rc, 1)

    def test_doctor_reports_warning_when_not_in_vault(self):
        self._make_fake_gh()
        output = self.root / "output"
        output.mkdir()
        from obsidian_code_atlas import doctor
        rc = doctor.run_doctor(output, None, self.environment)
        self.assertEqual(rc, 0)

    def test_doctor_does_not_expose_secrets(self):
        vault = self.root / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        output = vault / "Code Atlas"
        output.mkdir()
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            "if sys.argv[1:] == ['auth', 'status']:\n"
            "    print('token ghp_secret_token_xyz')\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        self.environment["PATH"] = str(fake_bin) + os.pathsep + self.environment["PATH"]
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from obsidian_code_atlas import doctor
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            doctor.run_doctor(output, None, self.environment)
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("ghp_secret_token_xyz", combined)


class TestInitCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(FAKE_GH_AUTHENTICATED, encoding="utf-8")
        fake_gh.chmod(0o755)
        self.environment = {
            "HOME": str(self.root / "home"),
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        }

    def test_cli_init_creates_output_directory(self):
        vault = self.root / "My Vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        with mock.patch.object(cli, "refresh", return_value=0):
            rc = cli.main([
                "init", str(vault), "--output", "Code Atlas",
                "--scheduler", "none", "--no-refresh",
            ], env=self.environment)
        self.assertEqual(rc, 0)
        self.assertTrue((vault / "Code Atlas").is_dir())

    def test_cli_no_gitignore_opts_out_and_track_generated_is_an_alias(self):
        for flag in ("--no-gitignore", "--track-generated"):
            with self.subTest(flag=flag):
                args = cli.build_parser().parse_args(
                    ["init", str(self.root), flag])
                self.assertFalse(args.gitignore)
        default = cli.build_parser().parse_args(["init", str(self.root)])
        self.assertTrue(default.gitignore)
        explicit = cli.build_parser().parse_args(["init", str(self.root), "--gitignore"])
        self.assertTrue(explicit.gitignore)

    def test_cli_no_gitignore_reaches_run_init(self):
        vault = self.root / "Tracked Vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        with mock.patch.object(init, "run_init",
                               return_value=(vault, vault / "Code Atlas",
                                             {"config": None, "git_action": "x",
                                              "scheduler": "none"}, 0)) as run_init:
            cli.main(["init", str(vault), "--no-gitignore", "--no-refresh"],
                     env=self.environment)
        self.assertTrue(run_init.call_args.kwargs["track_generated"])

    def test_cli_doctor_reports_success(self):
        vault = self.root / "My Vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        output = vault / "Code Atlas"
        output.mkdir()
        rc = cli.main(["doctor", "--output", str(output)], env=self.environment)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
