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

    def test_legacy_entry_is_removed_only_for_matching_output(self):
        one_refresh = self.one / "refresh.sh"
        two_refresh = self.two / "refresh.sh"
        self.state.contents = (
            "# gh_puller auto-refresh\n0 8 * * * '{}' all\n".format(one_refresh) +
            "0 8 * * * \"{}\" all # obsidian-code-atlas auto-refresh\n".format(two_refresh)
        )
        self.assertIn(str(one_refresh), scheduler.find_cron_entry(self.state.contents, self.one))
        self.assertTrue(scheduler.uninstall_cron(self.one))
        self.assertNotIn(str(one_refresh), self.state.contents)
        self.assertIn(str(two_refresh), self.state.contents)

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
