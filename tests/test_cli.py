import contextlib
import fcntl
import io
import json
import shutil
import signal
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from granola_switcher import __version__
from granola_switcher.raycast import install_scripts
from granola_switcher.cli import (
    main,
    Paths,
    SwitcherError,
    account_emails,
    heal_live_dir,
    list_profiles,
    open_app,
    profile_display_name,
    profile_metadata_path,
    profile_state_dir,
    quit_app,
    restore_last_live,
    save_profile,
    switch_lock,
    switch_profile,
)


def make_live_state(root: Path, email: str, marker: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "stored-accounts.json").write_text(
        json.dumps(
            {
                "accounts": json.dumps(
                    [
                        {
                            "email": email,
                            "tokens": "secret-token-that-status-must-not-print",
                            "userInfo": json.dumps({"email": email}),
                        }
                    ]
                )
            }
        ),
        encoding="utf-8",
    )
    (root / "marker.txt").write_text(marker, encoding="utf-8")
    (root / "SingletonLock").unlink(missing_ok=True)
    (root / "SingletonLock").symlink_to("stale-lock")
    leveldb = root / "Local Storage" / "leveldb"
    leveldb.mkdir(parents=True, exist_ok=True)
    (leveldb / "LOCK").write_text("", encoding="utf-8")


class GranolaSwitcherTests(unittest.TestCase):
    def test_account_emails_handles_nested_granola_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp) / "Granola"
            make_live_state(live, "person@example.com", "personal")

            self.assertEqual(account_emails(live), ["person@example.com"])

    def test_profile_display_name_prefers_account_names(self) -> None:
        self.assertEqual(profile_display_name("personal"), "Personal")
        self.assertEqual(profile_display_name("work"), "Work")
        self.assertEqual(profile_display_name("backup-account"), "Backup Account")

    def test_capture_sanitizes_runtime_locks_and_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "Granola"
            data = root / "switcher"
            make_live_state(live, "person@example.com", "personal")
            paths = Paths(live_dir=live, data_dir=data)

            save_profile(paths, "personal", label="Personal", provider="google")

            self.assertEqual(list_profiles(paths), ["personal"])
            self.assertFalse((profile_state_dir(paths, "personal") / "SingletonLock").exists())
            self.assertFalse((profile_state_dir(paths, "personal") / "Local Storage" / "leveldb" / "LOCK").exists())
            metadata = json.loads(profile_metadata_path(paths, "personal").read_text(encoding="utf-8"))
            self.assertEqual(metadata["email"], "person@example.com")
            self.assertEqual(metadata["provider"], "google")
            self.assertEqual((data / "active-profile").read_text(encoding="utf-8").strip(), "personal")

    def test_switch_saves_current_profile_and_restores_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "Granola"
            data = root / "switcher"
            paths = Paths(live_dir=live, data_dir=data)

            make_live_state(live, "person@example.com", "personal-v1")
            save_profile(paths, "personal", label="Personal", provider="google")

            make_live_state(live, "person@work.example", "work-v1")
            save_profile(paths, "work", label="Work", provider="microsoft")

            (live / "marker.txt").write_text("work-v2", encoding="utf-8")
            switch_profile(paths, "personal", open_after=False)

            self.assertEqual((live / "marker.txt").read_text(encoding="utf-8"), "personal-v1")
            self.assertEqual((profile_state_dir(paths, "work") / "marker.txt").read_text(encoding="utf-8"), "work-v2")
            self.assertEqual((data / "active-profile").read_text(encoding="utf-8").strip(), "personal")
            self.assertFalse(profile_state_dir(paths, "personal").exists())
            self.assertEqual(list_profiles(paths), ["personal", "work"])

            switch_profile(paths, "work", open_after=False)

            self.assertEqual((live / "marker.txt").read_text(encoding="utf-8"), "work-v2")
            self.assertEqual((profile_state_dir(paths, "personal") / "marker.txt").read_text(encoding="utf-8"), "personal-v1")
            self.assertFalse(profile_state_dir(paths, "work").exists())

    def test_no_save_switch_keeps_last_live_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "Granola"
            data = root / "switcher"
            paths = Paths(live_dir=live, data_dir=data)

            make_live_state(live, "person@example.com", "personal-live")
            save_profile(paths, "personal", label="Personal", provider="google")

            make_live_state(live, "person@work.example", "work-live")
            save_profile(paths, "work", label="Work", provider="microsoft")

            make_live_state(live, "nobody@example.com", "unsaved-live")
            backup = switch_profile(paths, "personal", save_current=False, open_after=False)

            self.assertIsNotNone(backup)
            self.assertEqual((live / "marker.txt").read_text(encoding="utf-8"), "personal-live")
            self.assertEqual((data / "backups" / "last-live-before-switch" / "marker.txt").read_text(encoding="utf-8"), "unsaved-live")

    def test_recapture_keeps_at_most_two_archives_per_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "Granola"
            data = root / "switcher"
            paths = Paths(live_dir=live, data_dir=data)

            for index in range(1, 5):
                make_live_state(live, "person@example.com", f"p{index}")
                save_profile(paths, "personal", label="Personal", provider="google")
                time.sleep(0.02)

            archive_root = data / "backups" / "replaced-profile-copies"
            archives = [child for child in archive_root.iterdir() if child.is_dir()]
            self.assertEqual(len(archives), 2)
            self.assertTrue(all(child.name.startswith("personal-") for child in archives))
            markers = {(child / "marker.txt").read_text(encoding="utf-8") for child in archives}
            self.assertEqual(markers, {"p2", "p3"})
            self.assertEqual(
                (profile_state_dir(paths, "personal") / "marker.txt").read_text(encoding="utf-8"), "p4"
            )

    def test_restore_last_live_swaps_backup_in_and_clears_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "Granola"
            data = root / "switcher"
            paths = Paths(live_dir=live, data_dir=data)

            make_live_state(live, "person@example.com", "personal-live")
            save_profile(paths, "personal", label="Personal", provider="google")
            make_live_state(live, "person@work.example", "work-live")
            save_profile(paths, "work", label="Work", provider="microsoft")
            make_live_state(live, "nobody@example.com", "unsaved-live")
            switch_profile(paths, "personal", save_current=False, open_after=False)

            aside = restore_last_live(paths)

            self.assertEqual((live / "marker.txt").read_text(encoding="utf-8"), "unsaved-live")
            self.assertIsNotNone(aside)
            self.assertEqual((aside / "marker.txt").read_text(encoding="utf-8"), "personal-live")
            self.assertFalse((data / "backups" / "last-live-before-switch").exists())
            self.assertFalse((data / "active-profile").exists())

    def test_restore_last_live_requires_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = Paths(live_dir=root / "Granola", data_dir=root / "switcher")
            with self.assertRaises(SwitcherError):
                restore_last_live(paths)

    def test_heal_live_dir_recovers_missing_live_from_stored_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live = root / "Granola"
            data = root / "switcher"
            paths = Paths(live_dir=live, data_dir=data)

            make_live_state(live, "person@example.com", "personal-v1")
            save_profile(paths, "personal", label="Personal", provider="google")
            shutil.rmtree(live)

            self.assertTrue(heal_live_dir(paths))
            self.assertEqual((live / "marker.txt").read_text(encoding="utf-8"), "personal-v1")
            self.assertFalse(profile_state_dir(paths, "personal").exists())
            self.assertFalse(heal_live_dir(paths))

    def test_switch_lock_times_out_instead_of_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = Paths(live_dir=root / "Granola", data_dir=root / "switcher")
            paths.data_dir.mkdir(parents=True, exist_ok=True)
            with paths.lock_file.open("w", encoding="utf-8") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                start = time.monotonic()
                with self.assertRaises(SwitcherError):
                    with switch_lock(paths, timeout=0.3):
                        pass
                self.assertLess(time.monotonic() - start, 2.0)

    def test_quit_app_is_noop_when_app_not_running(self) -> None:
        with mock.patch("granola_switcher.cli.app_process_ids", return_value=[]), mock.patch(
            "granola_switcher.cli.osascript"
        ) as fake_osascript, mock.patch("granola_switcher.cli.os.kill") as fake_kill:
            quit_app(timeout=0.05)
        fake_osascript.assert_not_called()
        fake_kill.assert_not_called()

    def test_quit_app_graceful_quit_sends_no_signals(self) -> None:
        calls = {"count": 0}

        def fake_pids(app_name: str = "Granola") -> list[int]:
            calls["count"] += 1
            return [4242] if calls["count"] == 1 else []

        with mock.patch("granola_switcher.cli.app_process_ids", side_effect=fake_pids), mock.patch(
            "granola_switcher.cli.osascript"
        ) as fake_osascript, mock.patch("granola_switcher.cli.os.kill") as fake_kill:
            quit_app(timeout=0.5)
        fake_osascript.assert_called_once()
        fake_kill.assert_not_called()

    def test_open_app_activates_once_running(self) -> None:
        with mock.patch("granola_switcher.cli.run") as fake_run, mock.patch(
            "granola_switcher.cli.app_is_running", return_value=True
        ), mock.patch("granola_switcher.cli.subprocess.Popen") as fake_popen:
            open_app()
        fake_run.assert_called_once()
        fake_popen.assert_called_once()
        self.assertEqual(
            fake_popen.call_args.args[0],
            ["osascript", "-e", 'tell application "Granola" to activate'],
        )

    def test_open_app_skips_activate_when_launch_never_lands(self) -> None:
        with mock.patch("granola_switcher.cli.run") as fake_run, mock.patch(
            "granola_switcher.cli.app_is_running", return_value=False
        ), mock.patch("granola_switcher.cli.subprocess.Popen") as fake_popen:
            open_app(wait=0.05)
        fake_run.assert_called()
        fake_popen.assert_not_called()

    def test_app_process_ids_ignores_crashpad_handler(self) -> None:
        import subprocess as sp

        def fake_run(command, **kwargs):
            if "-x" in command:
                return sp.CompletedProcess(command, 1, stdout="", stderr="")
            stdout = (
                "101 /Applications/Granola.app/Contents/Frameworks/Electron Framework.framework"
                "/Helpers/chrome_crashpad_handler --database=...\n"
                "202 /Applications/Granola.app/Contents/Frameworks/Granola Helper.app"
                "/Contents/MacOS/Granola Helper --type=gpu-process\n"
            )
            return sp.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with mock.patch("granola_switcher.cli.run", side_effect=fake_run):
            from granola_switcher.cli import app_process_ids

            self.assertEqual(app_process_ids(), [202])

    def test_version_flag_prints_version(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(__version__, buffer.getvalue())

    def test_raycast_install_writes_scripts_and_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "raycast"
            install_scripts(target, [("work", "Work"), ("personal", "Personal")])

            names = sorted(child.name for child in target.iterdir())
            self.assertEqual(
                names,
                [
                    "granola-switcher-status.sh",
                    "granola.png",
                    "switch-granola-personal.sh",
                    "switch-granola-work.sh",
                ],
            )
            script = (target / "switch-granola-work.sh").read_text(encoding="utf-8")
            self.assertIn("# @raycast.title Granola: Work", script)
            self.assertIn("# @raycast.icon granola.png", script)
            self.assertIn("/opt/homebrew/bin/granola-switcher", script)
            self.assertIn("/usr/local/bin/granola-switcher", script)
            self.assertIn('exec "$GS" switch work', script)
            status = (target / "granola-switcher-status.sh").read_text(encoding="utf-8")
            self.assertIn('exec "$GS" selected', status)
            for name in names:
                if name.endswith(".sh"):
                    self.assertTrue((target / name).stat().st_mode & stat.S_IXUSR)
            self.assertGreater((target / "granola.png").stat().st_size, 0)

            # Re-running regenerates cleanly.
            install_scripts(target, [("work", "Work"), ("personal", "Personal")])
            self.assertEqual(sorted(child.name for child in target.iterdir()), names)

    def test_raycast_install_command_uses_data_dir_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "switcher"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(["--data-dir", str(data), "raycast-install", "--profiles", "work"])
            self.assertEqual(code, 0)
            self.assertTrue((data / "raycast" / "switch-granola-work.sh").exists())
            self.assertFalse((data / "raycast" / "switch-granola-personal.sh").exists())
            self.assertIn(str(data / "raycast"), buffer.getvalue())

    def test_quit_app_falls_back_to_sigkill_then_raises(self) -> None:
        sent: list[int] = []

        with mock.patch("granola_switcher.cli.app_process_ids", return_value=[4242]), mock.patch(
            "granola_switcher.cli.osascript"
        ), mock.patch("granola_switcher.cli.os.kill", side_effect=lambda pid, sig: sent.append(sig)):
            with self.assertRaises(SwitcherError):
                quit_app(timeout=0.06, kill_timeout=0.06)
        self.assertEqual(sent, [signal.SIGKILL])


if __name__ == "__main__":
    unittest.main()
