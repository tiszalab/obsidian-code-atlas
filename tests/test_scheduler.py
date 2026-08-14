import contextlib
import io
import plistlib
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

from obsidian_code_atlas import cli, scheduler


class CronState:
    def __init__(self, contents=""):
        self.contents = contents
        self.writes = 0

    def read(self):
        return self.contents

    def write(self, contents):
        self.contents = contents
        self.writes += 1


class TestCronScheduler(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.one = (self.root / "Vault one" / "Code Atlas").resolve()
        self.two = (self.root / "Vault two" / "Code Atlas").resolve()
        self.state = CronState("MAILTO=user@example.com\n*/5 * * * * /usr/bin/true\n")
        self.read_patch = mock.patch.object(scheduler, "read_crontab", side_effect=self.state.read)
        self.write_patch = mock.patch.object(scheduler, "write_crontab", side_effect=self.state.write)
        self.read_patch.start()
        self.write_patch.start()
        self.addCleanup(self.read_patch.stop)
        self.addCleanup(self.write_patch.stop)

    def test_install_is_idempotent_and_preserves_unrelated_entries(self):
        first = scheduler.install_cron(self.one, executable="/opt/atlas/bin/python3")
        scheduler.install_cron(self.one, executable="/opt/atlas/bin/python3")
        self.assertEqual(self.state.writes, 1)
        self.assertEqual(self.state.contents.count(scheduler.cron_marker(self.one)), 1)
        self.assertIn("MAILTO=user@example.com\n*/5 * * * * /usr/bin/true\n", self.state.contents)
        self.assertIn(first, self.state.contents)

    def test_two_outputs_coexist_and_uninstall_removes_only_selected_job(self):
        scheduler.install_cron(self.one, executable="/opt/atlas/bin/python3")
        scheduler.install_cron(self.two, executable="/opt/atlas/bin/python3")
        self.assertIn(scheduler.cron_marker(self.one), self.state.contents)
        self.assertIn(scheduler.cron_marker(self.two), self.state.contents)
        self.assertTrue(scheduler.uninstall_cron(self.one))
        self.assertNotIn(scheduler.cron_marker(self.one), self.state.contents)
        self.assertIn(scheduler.cron_marker(self.two), self.state.contents)
        self.assertFalse(scheduler.uninstall_cron(self.one))

    def test_legacy_entry_is_removed_regardless_of_checkout_path(self):
        # The pre-CLI scheduler (setup.sh) points refresh.sh at the checkout
        # it was installed from, which is unrelated to any --output vault.
        # Migrating a job for `self.two` must still clean it up even though
        # its path has nothing to do with either vault.
        checkout_refresh = self.root / "src" / "obsidian-code-atlas" / "refresh.sh"
        two_line = scheduler.cron_line(self.two, executable="/opt/atlas/bin/python3")
        self.state.contents = (
            "# gh_puller auto-refresh\n0 8 * * * '{}' all\n".format(checkout_refresh) +
            two_line + "\n"
        )
        self.assertIn(str(checkout_refresh), scheduler.find_cron_entry(self.state.contents, self.one))
        self.assertTrue(scheduler.uninstall_cron(self.one))
        self.assertNotIn(str(checkout_refresh), self.state.contents)
        self.assertIn(scheduler.cron_marker(self.two), self.state.contents)

    def test_marker_text_inside_unrelated_comment_is_preserved(self):
        unrelated = "# keep {} because this is explanatory text\n".format(scheduler.cron_marker(self.one))
        self.state.contents = unrelated
        scheduler.install_cron(self.one, executable="/opt/atlas/bin/python3")
        self.assertIn(unrelated, self.state.contents)
        self.assertIsNone(scheduler.find_cron_entry(unrelated, self.one))

    def test_shell_metacharacters_are_quoted_and_config_is_explicit(self):
        output = (self.root / "Vault $HOME; echo bad" / "Code Atlas").resolve()
        config = (self.root / "config $(touch bad).json").resolve()
        line = scheduler.cron_line(output, config, executable="/opt/Atlas Python/bin/python3")
        self.assertIn("'/opt/Atlas Python/bin/python3'", line)
        self.assertIn("'{}'".format(output), line)
        self.assertIn("'{}'".format(config), line)
        self.assertIn("refresh all --output", line)
        self.assertNotIn(str(ROOT), line)

    def test_cron_line_sets_a_path_including_homebrew_and_caller_path(self):
        line = scheduler.cron_line(self.one, executable="/opt/atlas/bin/python3",
                                   environment={"PATH": "/opt/pyenv/bin"})
        self.assertIn("PATH=", line)
        self.assertIn("/opt/homebrew/bin", line)
        self.assertIn("/usr/local/bin", line)
        self.assertIn("/opt/pyenv/bin", line)
        self.assertIn("/usr/bin", line)
        # The PATH assignment must come before the command it applies to.
        self.assertLess(line.index("PATH="), line.index("/opt/atlas/bin/python3"))

    def test_cron_line_escapes_percent_so_cron_does_not_truncate_it(self):
        output = (self.root / "100% Notes" / "Code Atlas").resolve()
        line = scheduler.cron_line(output, executable="/opt/atlas/bin/python3")
        self.assertNotIn("%", line.replace("\\%", ""))
        self.assertIn("100\\% Notes", line)


class TestLaunchdScheduler(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.output = (self.root / "Vault & notes" / "Code Atlas").resolve()

    def test_payload_round_trips_through_plistlib_with_argument_array(self):
        config = (self.root / "config & settings.json").resolve()
        payload = scheduler.launchd_payload(
            self.output, config, executable="/opt/Atlas Python/bin/python3", environment={"PATH": "/usr/bin:/bin"})
        encoded = plistlib.dumps(payload)
        parsed = plistlib.loads(encoded)
        self.assertEqual(parsed["ProgramArguments"], [
            "/opt/Atlas Python/bin/python3", "-m", "obsidian_code_atlas", "refresh", "all",
            "--output", str(self.output), "--config", str(config),
        ])
        self.assertEqual(parsed["StartCalendarInterval"], {"Hour": 8, "Minute": 0})
        self.assertTrue(parsed["RunAtLoad"])
        self.assertEqual(Path(parsed["StandardOutPath"]).parent, self.output)
        self.assertNotIn("site-packages", parsed["StandardOutPath"])

    def test_outputs_have_distinct_labels_and_plist_paths(self):
        other = (self.root / "Other Vault" / "Code Atlas").resolve()
        self.assertNotEqual(scheduler.scheduler_identifier(self.output), scheduler.scheduler_identifier(other))
        self.assertNotEqual(scheduler.launchd_path(self.output, self.home),
                            scheduler.launchd_path(other, self.home))

    def test_install_writes_valid_plist_and_uses_launchctl(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", return_value=completed) as run:
            path = scheduler.install_launchd(
                self.output, self.home, executable="/opt/atlas/bin/python3", environment={"PATH": "/usr/bin"})
        with path.open("rb") as handle:
            parsed = plistlib.load(handle)
        self.assertEqual(parsed["Label"], scheduler.scheduler_identifier(self.output))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[-1].args[0][0:2], ["launchctl", "bootstrap"])

    def test_uninstall_removes_only_selected_launchagent(self):
        other = (self.root / "Other Vault" / "Code Atlas").resolve()
        selected_path = scheduler.launchd_path(self.output, self.home)
        other_path = scheduler.launchd_path(other, self.home)
        selected_path.parent.mkdir(parents=True)
        selected_path.write_bytes(plistlib.dumps(scheduler.launchd_payload(self.output)))
        other_path.write_bytes(plistlib.dumps(scheduler.launchd_payload(other)))
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertTrue(scheduler.uninstall_launchd(self.output, self.home))
        self.assertFalse(selected_path.exists())
        self.assertTrue(other_path.exists())

    def test_launchctl_failure_is_reported(self):
        bootout = subprocess.CompletedProcess([], 0, "", "")
        bootstrap = subprocess.CompletedProcess([], 5, "", "permission denied")
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", side_effect=[bootout, bootstrap]), \
             self.assertRaisesRegex(scheduler.SchedulerError, "permission denied"):
            scheduler.install_launchd(self.output, self.home)

    def test_launchd_rejects_non_macos_with_cron_recommendation(self):
        with mock.patch.object(scheduler.platform, "system", return_value="Linux"), \
             self.assertRaisesRegex(scheduler.SchedulerError, "use --cron"):
            scheduler.install_launchd(self.output, self.home)
        self.assertFalse(scheduler.launchd_path(self.output, self.home).exists())

    def test_failed_bootstrap_does_not_leave_a_stale_plist(self):
        bootout = subprocess.CompletedProcess([], 0, "", "")
        bootstrap = subprocess.CompletedProcess([], 5, "", "permission denied")
        path = scheduler.launchd_path(self.output, self.home)
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", side_effect=[bootout, bootstrap]), \
             self.assertRaises(scheduler.SchedulerError):
            scheduler.install_launchd(self.output, self.home)
        self.assertFalse(path.exists())

    def test_uninstall_reports_real_bootout_failures_and_keeps_the_plist(self):
        path = scheduler.launchd_path(self.output, self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps(scheduler.launchd_payload(self.output)))
        bootout_failure = subprocess.CompletedProcess([], 5, "", "Input/output error")
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", return_value=bootout_failure), \
             self.assertRaisesRegex(scheduler.SchedulerError, "Input/output error"):
            scheduler.uninstall_launchd(self.output, self.home)
        self.assertTrue(path.exists())

    def _write_legacy_plist(self, label):
        legacy_path = self.home / "Library" / "LaunchAgents" / "{}.plist".format(label)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_payload = {
            "Label": label,
            "ProgramArguments": ["/bin/bash", str(self.root / "checkout" / "refresh.sh"), "all"],
        }
        legacy_path.write_bytes(plistlib.dumps(legacy_payload))
        return legacy_path

    def test_install_purges_a_legacy_setup_sh_launchagent(self):
        legacy_path = self._write_legacy_plist("com.obsidian-code-atlas.deadbeef")
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", return_value=completed):
            scheduler.install_launchd(self.output, self.home, executable="/opt/atlas/bin/python3")
        self.assertFalse(legacy_path.exists())

    def test_uninstall_purges_a_legacy_gh_puller_launchagent(self):
        legacy_path = self._write_legacy_plist("com.ghpuller.gh_puller.deadbeef")
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", return_value=completed):
            self.assertTrue(scheduler.uninstall_launchd(self.output, self.home))
        self.assertFalse(legacy_path.exists())

    def test_legacy_purge_ignores_our_own_launchagents(self):
        other = (self.root / "Other Vault" / "Code Atlas").resolve()
        other_path = scheduler.launchd_path(other, self.home)
        other_path.parent.mkdir(parents=True)
        other_path.write_bytes(plistlib.dumps(scheduler.launchd_payload(other)))
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(scheduler.platform, "system", return_value="Darwin"), \
             mock.patch.object(scheduler, "_run", return_value=completed):
            scheduler.install_launchd(self.output, self.home, executable="/opt/atlas/bin/python3")
        self.assertTrue(other_path.exists())


class TestSchedulerCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = (self.root / "My Vault" / "Code Atlas").resolve()
        self.environment = {"HOME": str(self.root / "home")}

    def test_install_rejects_selecting_both_scheduler_types(self):
        with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr"):
            cli.main(["scheduler", "install", "--cron", "--launchd",
                      "--output", str(self.output)], env=self.environment)
        self.assertEqual(raised.exception.code, 2)

    def test_status_reports_installed_and_missing_jobs(self):
        launchd = scheduler.launchd_payload(self.output, executable="/opt/atlas/bin/python3")
        cron = scheduler.cron_line(self.output, executable="/opt/atlas/bin/python3")
        stdout = io.StringIO()
        with mock.patch.object(scheduler, "scheduler_status", return_value=(launchd, cron)), \
             contextlib.redirect_stdout(stdout):
            result = cli.main(["scheduler", "status", "--output", str(self.output)], env=self.environment)
        self.assertEqual(result, 0)
        self.assertIn("launchd: installed", stdout.getvalue())
        self.assertIn("cron: installed", stdout.getvalue())
        self.assertIn(scheduler.scheduler_identifier(self.output), stdout.getvalue())

        stdout = io.StringIO()
        with mock.patch.object(scheduler, "scheduler_status", return_value=(None, None)), \
             contextlib.redirect_stdout(stdout):
            result = cli.main(["scheduler", "status", "--output", str(self.output)], env=self.environment)
        self.assertEqual(result, 1)
        self.assertIn("launchd: not installed", stdout.getvalue())
        self.assertIn("cron: not installed", stdout.getvalue())

    def test_cron_install_passes_absolute_output_and_config(self):
        config = self.root / "settings.json"
        config.write_text("{}", encoding="utf-8")
        with mock.patch.object(scheduler, "install_cron", return_value="job") as install:
            self.assertEqual(cli.main([
                "scheduler", "install", "--cron", "--output", str(self.output),
                "--config", str(config),
            ], env=self.environment), 0)
        self.assertEqual(install.call_args.args, (self.output, config.resolve()))

    def test_install_does_not_bake_in_auto_discovered_config(self):
        # A config file that only exists because it happens to sit inside the
        # output directory must not be hard-coded into the scheduled job: if
        # it is later renamed or removed, the job should keep working off the
        # built-in defaults, same as an interactive refresh would.
        self.output.mkdir(parents=True)
        (self.output / "obsidian-code-atlas.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(scheduler, "install_cron", return_value="job") as install:
            self.assertEqual(cli.main([
                "scheduler", "install", "--cron", "--output", str(self.output),
            ], env=self.environment), 0)
        self.assertEqual(install.call_args.args, (self.output, None))

    def test_install_filesystem_failure_is_clear_nonzero(self):
        output_file = self.root / "not a directory"
        output_file.write_text("occupied", encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = cli.main(["scheduler", "install", "--cron", "--output", str(output_file)],
                              env=self.environment)
        self.assertEqual(result, 1)
        self.assertIn("output path is not a directory", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
