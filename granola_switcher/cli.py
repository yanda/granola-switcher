from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from granola_switcher import __version__, raycast


APP_NAME = "Granola"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ARCHIVE_SUFFIX_RE = re.compile(r"-\d{8}-\d{6}-[0-9a-f]{8}$")
RUNTIME_NAMES = {"SingletonCookie", "SingletonLock", "SingletonSocket"}
DEFAULT_QUIT_TIMEOUT = 0.75
DEFAULT_KILL_TIMEOUT = 1.0
QUIT_POLL_INTERVAL = 0.05
DEFAULT_LOCK_TIMEOUT = 10.0
MAX_PROFILE_ARCHIVES = 2
REPLACED_COPIES_DIRNAME = "replaced-profile-copies"
LAST_LIVE_BACKUP_NAME = "last-live-before-switch"
NO_ACTIVE_MARKER_ERROR = (
    "No active profile marker exists yet. Capture the currently signed-in "
    "Granola account first, or rerun with --discard-current."
)


class SwitcherError(RuntimeError):
    pass


_timings: list[tuple[str, float]] = []


@contextlib.contextmanager
def timed(label: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        _timings.append((label, time.perf_counter() - start))


def print_timings() -> None:
    if os.environ.get("GRANOLA_SWITCHER_TIMINGS") != "1":
        return
    for label, duration in _timings:
        print(f"timing {label}: {duration * 1000:.0f} ms", file=sys.stderr)


class CrossDeviceMoveError(SwitcherError):
    pass


@dataclass(frozen=True)
class Paths:
    live_dir: Path
    data_dir: Path

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def active_file(self) -> Path:
        return self.data_dir / "active-profile"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "switch.lock"


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def default_live_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_NAME


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "granola-switcher"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def validate_profile_name(name: str) -> str:
    if not PROFILE_NAME_RE.match(name):
        raise SwitcherError(
            "Profile names must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens."
        )
    return name


def profile_root(paths: Paths, name: str) -> Path:
    return paths.profiles_dir / validate_profile_name(name)


def profile_state_dir(paths: Paths, name: str) -> Path:
    return profile_root(paths, name) / APP_NAME


def profile_metadata_path(paths: Paths, name: str) -> Path:
    return profile_root(paths, name) / "profile.json"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def switch_lock(paths: Paths, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    with paths.lock_file.open("w", encoding="utf-8") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SwitcherError(
                        "Another granola-switcher operation is already running; retry in a moment."
                    )
                time.sleep(0.1)
        yield


def run(command: list[str], *, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    stdout = subprocess.DEVNULL if quiet else subprocess.PIPE
    stderr = subprocess.DEVNULL if quiet else subprocess.PIPE
    return subprocess.run(command, text=True, stdout=stdout, stderr=stderr, check=check)


def osascript(script: str, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return run(["osascript", "-e", script], check=check)


def app_is_running(app_name: str = APP_NAME) -> bool:
    # pgrep is the ground truth here: osascript can fail (missing binary,
    # blocked automation permission) and a failure must never make a running
    # app look quit, or a switch would proceed under live LevelDB locks.
    return bool(app_process_ids(app_name))


def process_ids_by_exact_name(name: str) -> list[int]:
    result = run(["pgrep", "-x", name], check=False)
    if result.returncode not in (0, 1):
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def process_ids_by_pattern(pattern: str, *, exclude: tuple[str, ...] = ()) -> list[int]:
    result = run(["pgrep", "-fl", pattern], check=False)
    if result.returncode not in (0, 1):
        return []
    pids: list[int] = []
    current_pid = os.getpid()
    for line in (result.stdout or "").splitlines():
        pid_text, _, command = line.strip().partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        if any(marker in command for marker in exclude):
            continue
        pids.append(pid)
    return pids


def app_process_ids(app_name: str = APP_NAME) -> list[int]:
    bundle_pattern = f"/Applications/{app_name}.app"
    # chrome_crashpad_handler is detached (ppid 1), ignores SIGTERM, and can
    # outlive the app by design. It holds no account-state locks, so counting
    # it as "the app" would stall every switch until the SIGKILL fallback.
    helpers = process_ids_by_pattern(bundle_pattern, exclude=("crashpad_handler",))
    return sorted({*process_ids_by_exact_name(app_name), *helpers})


def terminate_processes(pids: list[int], *, sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def wait_for_app_exit(app_name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not app_is_running(app_name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(QUIT_POLL_INTERVAL)


def quit_app(
    app_name: str = APP_NAME,
    *,
    timeout: float = DEFAULT_QUIT_TIMEOUT,
    kill_timeout: float = DEFAULT_KILL_TIMEOUT,
) -> None:
    with timed("quit-app"):
        _quit_app(app_name, timeout=timeout, kill_timeout=kill_timeout)


def _quit_app(app_name: str, *, timeout: float, kill_timeout: float) -> None:
    if not app_is_running(app_name):
        return
    osascript(f'if application "{app_name}" is running then tell application "{app_name}" to quit')
    if wait_for_app_exit(app_name, timeout):
        return

    # Granola has no external graceful-quit path (measured 2026-07): the
    # AppleScript quit event returns success but has no effect, SIGTERM only
    # kills helper processes which the main process then respawns, SIGINT is
    # ignored, and SIGKILL exits in ~60ms. Escalating through TERM would stall
    # every switch on a timeout that can never help, so go straight to KILL
    # once the grace window for the quit event has passed.
    terminate_processes(app_process_ids(app_name), sig=signal.SIGKILL)
    if wait_for_app_exit(app_name, kill_timeout):
        return
    raise SwitcherError(f"{app_name} is still running; quit it and retry.")


def open_app(app_name: str = APP_NAME, *, wait: float = 5.0) -> None:
    with timed("open-launch"):
        run(["open", "-a", app_name], check=True, quiet=True)
        deadline = time.monotonic() + wait
        relaunch_at = time.monotonic() + 1.0
        while not app_is_running(app_name):
            now = time.monotonic()
            if now >= deadline:
                return
            if now >= relaunch_at:
                # LaunchServices can no-op an open that races the app's previous
                # quit; ask again until the process actually exists.
                run(["open", "-a", app_name], check=True, quiet=True)
                relaunch_at = now + 1.0
            time.sleep(QUIT_POLL_INTERVAL)
    # `open -a` does not reliably take focus when invoked from a background
    # process such as Raycast. Activate without waiting: the AppleEvent blocks
    # until the Electron app is fully scriptable (~0.5s), which the user does
    # not need to sit through. Best effort — the launch already succeeded even
    # if automation permission is denied.
    with timed("open-activate"):
        subprocess.Popen(
            ["osascript", "-e", f'tell application "{app_name}" to activate'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def sanitize_runtime_files(root: Path) -> None:
    for name in RUNTIME_NAMES:
        remove_path(root / name)
    for lock_file in root.rglob("LOCK"):
        if lock_file.is_file() or lock_file.is_symlink():
            lock_file.unlink(missing_ok=True)


def copy_dir(src: Path, dst: Path) -> None:
    # cp -c clones via clonefile(2) on APFS, making same-volume copies nearly
    # instant and free on disk. Fall back to ditto, then a pure-Python copy.
    if sys.platform == "darwin":
        result = run(["cp", "-Rc", str(src), str(dst)], check=False)
        if result.returncode == 0:
            return
        remove_path(dst)
    if shutil.which("ditto"):
        run(["ditto", str(src), str(dst)], check=True)
    else:
        shutil.copytree(src, dst, symlinks=True)


def copy_tree(src: Path, dst: Path, *, sanitize: bool = False) -> None:
    if not src.exists():
        raise SwitcherError(f"Missing source directory: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    remove_path(tmp)
    try:
        copy_dir(src, tmp)
        if sanitize:
            sanitize_runtime_files(tmp)
        remove_path(dst)
        tmp.rename(dst)
    except Exception:
        remove_path(tmp)
        raise


def existing_device(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe.stat().st_dev


def same_device(left: Path, right: Path) -> bool:
    return existing_device(left) == existing_device(right)


def move_path(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError as error:
        if error.errno == errno.EXDEV:
            raise CrossDeviceMoveError(
                "Fast switching needs the live Granola folder and switcher profiles on the same volume."
            ) from error
        raise


def backup_name(name: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{validate_profile_name(name)}-{stamp}-{uuid.uuid4().hex[:8]}"


def prune_profile_archives(paths: Paths, name: str, *, keep: int = MAX_PROFILE_ARCHIVES) -> list[Path]:
    root = paths.backups_dir / REPLACED_COPIES_DIRNAME
    if not root.exists():
        return []
    archives = [
        child
        for child in root.iterdir()
        if child.is_dir()
        and ARCHIVE_SUFFIX_RE.search(child.name)
        and ARCHIVE_SUFFIX_RE.sub("", child.name) == name
    ]
    archives.sort(key=lambda path: (path.stat().st_mtime, path.name))
    stale = archives[:-keep] if keep > 0 else archives
    for path in stale:
        remove_path(path)
    return stale


def archive_existing_profile_store(paths: Paths, name: str) -> Path | None:
    state_dir = profile_state_dir(paths, name)
    if not state_dir.exists():
        return None
    archive = paths.backups_dir / REPLACED_COPIES_DIRNAME / backup_name(name)
    move_path(state_dir, archive)
    prune_profile_archives(paths, name)
    return archive


def move_live_to_last_backup(paths: Paths) -> Path | None:
    if not paths.live_dir.exists():
        return None
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    backup_path = paths.backups_dir / LAST_LIVE_BACKUP_NAME
    remove_path(backup_path)
    move_path(paths.live_dir, backup_path)
    return backup_path


def restore_last_live(paths: Paths) -> Path | None:
    backup = paths.backups_dir / LAST_LIVE_BACKUP_NAME
    if not backup.exists():
        raise SwitcherError(f"No last-live backup to restore: {backup}")

    aside = None
    if paths.live_dir.exists():
        aside = paths.backups_dir / REPLACED_COPIES_DIRNAME / backup_name("live-before-restore")
        move_path(paths.live_dir, aside)
    try:
        move_path(backup, paths.live_dir)
    except Exception:
        if aside and aside.exists() and not paths.live_dir.exists():
            move_path(aside, paths.live_dir)
        raise

    prune_profile_archives(paths, "live-before-restore")
    # The backup came from a --no-save or --discard-current switch, so which
    # profile it holds is unknown; clear the marker rather than guess.
    paths.active_file.unlink(missing_ok=True)
    return aside


def replace_live_from_profile(profile_dir: Path, live_dir: Path, backups_dir: Path) -> Path | None:
    if not profile_dir.exists():
        raise SwitcherError(f"Profile state does not exist: {profile_dir}")

    live_parent = live_dir.parent
    live_parent.mkdir(parents=True, exist_ok=True)
    tmp_live = live_parent / f".{live_dir.name}.switching-{os.getpid()}-{uuid.uuid4().hex}"
    old_live = live_parent / f".{live_dir.name}.previous-{os.getpid()}-{uuid.uuid4().hex}"
    remove_path(tmp_live)
    remove_path(old_live)

    try:
        copy_dir(profile_dir, tmp_live)
        sanitize_runtime_files(tmp_live)

        if live_dir.exists():
            live_dir.rename(old_live)
        tmp_live.rename(live_dir)
    except Exception:
        remove_path(tmp_live)
        if old_live.exists() and not live_dir.exists():
            old_live.rename(live_dir)
        raise

    if not old_live.exists():
        return None

    backups_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backups_dir / LAST_LIVE_BACKUP_NAME
    remove_path(backup_path)
    old_live.rename(backup_path)
    return backup_path


def permissive_json(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def parse_nested_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def account_emails(live_dir: Path) -> list[str]:
    path = live_dir / "stored-accounts.json"
    if not path.exists():
        return []
    try:
        payload = permissive_json(path)
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(payload, dict):
        accounts = parse_nested_json(payload.get("accounts", []))
    else:
        accounts = payload

    if isinstance(accounts, str):
        accounts = parse_nested_json(accounts)
    if not isinstance(accounts, list):
        return []

    emails: list[str] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        candidates = [
            account.get("email"),
            account.get("accountEmail"),
            account.get("userEmail"),
        ]
        user_info = parse_nested_json(account.get("userInfo"))
        if isinstance(user_info, dict):
            candidates.extend([user_info.get("email"), user_info.get("user_email")])
            person = user_info.get("person")
            if isinstance(person, dict):
                candidates.append(person.get("email"))
        for candidate in candidates:
            if isinstance(candidate, str) and "@" in candidate and candidate not in emails:
                emails.append(candidate)
    return emails


def active_profile(paths: Paths) -> str | None:
    value = read_text(paths.active_file)
    return value if value else None


def profile_display_name(name: str) -> str:
    return {
        "personal": "Personal",
        "work": "Work",
    }.get(name, name.replace("-", " ").replace("_", " ").title())


def set_active_profile(paths: Paths, name: str) -> None:
    write_text_atomic(paths.active_file, validate_profile_name(name) + "\n")


def profile_has_state(paths: Paths, name: str) -> bool:
    return profile_state_dir(paths, name).exists()


def touch_profile_metadata(paths: Paths, name: str) -> None:
    meta_path = profile_metadata_path(paths, name)
    metadata = read_json(meta_path)
    metadata.setdefault("name", name)
    metadata.setdefault("label", name)
    metadata["captured_at"] = now_iso()
    write_json_atomic(meta_path, metadata)


def ensure_can_switch(paths: Paths, target: str, *, save_current: bool, discard_current: bool) -> str | None:
    target = validate_profile_name(target)
    current = active_profile(paths)
    if current == target:
        return current
    if not profile_has_state(paths, target):
        raise SwitcherError(f"Unknown profile: {target}. Capture it first.")
    if save_current and not current and paths.live_dir.exists() and not discard_current:
        raise SwitcherError(NO_ACTIVE_MARKER_ERROR)
    return current


def heal_live_dir(paths: Paths) -> bool:
    """Recover from a crash that left the live dir missing but the active profile stored."""
    if paths.live_dir.exists():
        return False
    current = active_profile(paths)
    if not current:
        return False
    state_dir = profile_state_dir(paths, current)
    if not state_dir.exists():
        return False
    if same_device(state_dir, paths.live_dir.parent):
        move_path(state_dir, paths.live_dir)
    else:
        replace_live_from_profile(state_dir, paths.live_dir, paths.backups_dir)
    return True


def list_profiles(paths: Paths) -> list[str]:
    profiles: set[str] = set()
    current = active_profile(paths)
    if current:
        profiles.add(current)
    if not paths.profiles_dir.exists():
        return sorted(profiles)
    for child in paths.profiles_dir.iterdir():
        if child.is_dir() and (profile_state_dir(paths, child.name).exists() or profile_metadata_path(paths, child.name).exists()):
            profiles.add(child.name)
    return sorted(profiles)


def save_profile(
    paths: Paths,
    name: str,
    *,
    email: str | None = None,
    label: str | None = None,
    provider: str | None = None,
    mark_active: bool = True,
) -> None:
    name = validate_profile_name(name)
    if not paths.live_dir.exists():
        raise SwitcherError(f"Granola support folder does not exist: {paths.live_dir}")

    root = profile_root(paths, name)
    state_dir = profile_state_dir(paths, name)
    root.mkdir(parents=True, exist_ok=True)
    archive_existing_profile_store(paths, name)
    copy_tree(paths.live_dir, state_dir, sanitize=True)

    existing = read_json(profile_metadata_path(paths, name))
    detected = account_emails(paths.live_dir)
    metadata = {
        **existing,
        "name": name,
        "label": label or existing.get("label") or name,
        "email": email or existing.get("email") or (detected[0] if detected else None),
        "provider": provider or existing.get("provider"),
        "captured_at": now_iso(),
        "source": str(paths.live_dir),
    }
    write_json_atomic(profile_metadata_path(paths, name), metadata)
    if mark_active:
        set_active_profile(paths, name)


def switch_profile(
    paths: Paths,
    target: str,
    *,
    save_current: bool = True,
    discard_current: bool = False,
    open_after: bool = True,
) -> Path | None:
    target = validate_profile_name(target)
    current = ensure_can_switch(paths, target, save_current=save_current, discard_current=discard_current)
    if current == target:
        if open_after:
            open_app()
        return None

    with timed("swap-state"):
        if same_device(paths.live_dir.parent, paths.profiles_dir):
            backup = switch_profile_by_move(paths, target, current=current, save_current=save_current)
        else:
            if save_current and current:
                save_profile(paths, current, mark_active=False)
            backup = replace_live_from_profile(
                profile_state_dir(paths, target), paths.live_dir, paths.backups_dir
            )
    set_active_profile(paths, target)
    if open_after:
        open_app()
    return backup


def switch_profile_by_move(
    paths: Paths,
    target: str,
    *,
    current: str | None,
    save_current: bool,
) -> Path | None:
    target_dir = profile_state_dir(paths, target)
    if not target_dir.exists():
        raise SwitcherError(f"Profile state does not exist: {target_dir}")
    if not paths.live_dir.exists():
        raise SwitcherError(f"Granola support folder does not exist: {paths.live_dir}")

    with timed("sanitize"):
        sanitize_runtime_files(paths.live_dir)
        sanitize_runtime_files(target_dir)

    if save_current and current:
        current_dir = profile_state_dir(paths, current)
        archive_existing_profile_store(paths, current)
        try:
            move_path(paths.live_dir, current_dir)
            move_path(target_dir, paths.live_dir)
        except Exception:
            if not paths.live_dir.exists() and current_dir.exists():
                move_path(current_dir, paths.live_dir)
            raise
        touch_profile_metadata(paths, current)
        return None

    backup = move_live_to_last_backup(paths)
    try:
        move_path(target_dir, paths.live_dir)
    except Exception:
        if backup and backup.exists() and not paths.live_dir.exists():
            move_path(backup, paths.live_dir)
        raise
    return backup


def backup_summary(paths: Paths) -> str:
    root = paths.backups_dir / REPLACED_COPIES_DIRNAME
    archived = sum(1 for child in root.iterdir() if child.is_dir()) if root.exists() else 0
    last_live = paths.backups_dir / LAST_LIVE_BACKUP_NAME
    copies = "copy" if archived == 1 else "copies"
    return f"{archived} archived {copies}, last-live backup {'present' if last_live.exists() else 'none'}"


def print_status(paths: Paths) -> None:
    current = active_profile(paths)
    live_emails = account_emails(paths.live_dir) if paths.live_dir.exists() else []
    print(f"Granola support: {paths.live_dir}")
    print(f"Switcher data:   {paths.data_dir}")
    print(f"Active profile:  {current or '(not set)'}")
    print(f"Live account:    {', '.join(live_emails) if live_emails else '(unknown)'}")
    print(f"Backups:         {backup_summary(paths)}")
    print()
    profiles = list_profiles(paths)
    if not profiles:
        print("No captured profiles yet.")
        return
    print("Profiles:")
    for name in profiles:
        meta = read_json(profile_metadata_path(paths, name))
        marker = "*" if name == current else " "
        location = "live" if name == current else ("stored" if profile_has_state(paths, name) else "missing")
        label = meta.get("label") or name
        email = meta.get("email") or "(unknown email)"
        provider = f" / {meta['provider']}" if meta.get("provider") else ""
        captured = meta.get("captured_at") or "unknown capture time"
        print(f" {marker} {name}: {label} <{email}>{provider}, {location}, captured {captured}")


def print_selected(paths: Paths) -> None:
    current = active_profile(paths)
    if not current:
        print("No Granola profile selected")
        return
    print(profile_display_name(current))


def automation_status() -> str:
    if not shutil.which("osascript"):
        return "UNAVAILABLE (osascript not found; will fall back to SIGTERM)"
    if not app_is_running():
        return "unchecked (Granola not running)"
    result = osascript(f'tell application "{APP_NAME}" to get name')
    if result.returncode == 0:
        return "OK"
    stderr = (result.stderr or "").strip()
    if "-1743" in stderr:
        return "BLOCKED (allow in System Settings > Privacy & Security > Automation)"
    return f"unknown ({stderr[:80]})"


def print_doctor(paths: Paths) -> None:
    app_path = Path("/Applications/Granola.app")
    print(f"Granola app:     {app_path} {'OK' if app_path.exists() else 'MISSING'}")
    print(f"Granola support: {paths.live_dir} {'OK' if paths.live_dir.exists() else 'MISSING'}")
    print(f"Switcher data:   {paths.data_dir}")
    print(f"python:          {sys.executable} ({sys.version.split()[0]})")
    print(f"ditto:           {shutil.which('ditto') or '(not found; will use Python copy)'}")
    print(f"osascript:       {shutil.which('osascript') or '(not found)'}")
    print(f"open:            {shutil.which('open') or '(not found)'}")
    print(f"pgrep:           {shutil.which('pgrep') or '(not found)'}")
    print(f"automation:      {automation_status()}")
    fast_switch = "OK" if same_device(paths.live_dir.parent, paths.profiles_dir) else "COPY FALLBACK"
    print(f"fast switch:     {fast_switch}")
    current = active_profile(paths)
    if not paths.live_dir.exists() and current and profile_has_state(paths, current):
        print(
            f"recovery:        live folder missing; stored '{current}' can be restored "
            "(run: granola-switcher doctor --fix)"
        )


def print_setup(paths: Paths) -> None:
    live = account_emails(paths.live_dir) if paths.live_dir.exists() else []
    live_hint = live[0] if live else "the account currently open in Granola"
    personal_email = read_json(profile_metadata_path(paths, "personal")).get("email") or "you@personal.example"
    work_email = read_json(profile_metadata_path(paths, "work")).get("email") or "you@work.example"
    print("Suggested setup:")
    print()
    print("1. Capture the account currently open in Granola.")
    print(f"   Detected live account: {live_hint}")
    print("   If this is personal:")
    print(f'   granola-switcher capture personal --email {personal_email} --label "Personal" --provider google')
    print("   If this is work:")
    print(f'   granola-switcher capture work --email {work_email} --label "Work" --provider microsoft')
    print()
    print("2. Open Granola, sign out, sign into the other account, then capture it.")
    print(f'   granola-switcher capture work --email {work_email} --label "Work" --provider microsoft')
    print(f'   granola-switcher capture personal --email {personal_email} --label "Personal" --provider google')
    print()
    print("3. Switch with:")
    print("   granola-switcher switch work")
    print("   granola-switcher switch personal")
    print()
    print("4. For keyboard switching, run `granola-switcher raycast-install` and add the")
    print("   printed folder in Raycast: Settings > Extensions > Script Commands > Add Directories.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast local profile switcher for Granola on macOS.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--live-dir", default=os.environ.get("GRANOLA_LIVE_DIR", str(default_live_dir())))
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("GRANOLA_SWITCHER_DATA_DIR", str(default_data_dir())),
        help="Where captured Granola profiles are stored.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local Granola and macOS tool paths.")
    doctor.add_argument(
        "--fix",
        action="store_true",
        help="Repair a crash-interrupted switch by restoring the stored active profile to the live folder.",
    )
    subparsers.add_parser("selected", help="Show only the active Granola profile name.")
    subparsers.add_parser("setup", help="Print first-time setup steps.")
    subparsers.add_parser("status", help="Show active profile and captured profiles.")

    capture = subparsers.add_parser("capture", help="Capture the currently signed-in Granola state.")
    capture.add_argument("name")
    capture.add_argument("--email")
    capture.add_argument("--label")
    capture.add_argument("--provider", choices=["google", "microsoft", "other"])
    capture.add_argument("--no-quit", action="store_true", help="Do not quit Granola before copying state.")
    capture.add_argument("--no-mark-active", action="store_true", help="Capture without marking this profile active.")

    switch = subparsers.add_parser("switch", help="Switch Granola to a captured profile.")
    switch.add_argument("name")
    switch.add_argument("--no-save", action="store_true", help="Do not save the current active profile first.")
    switch.add_argument(
        "--discard-current",
        action="store_true",
        help="Allow switching before an active profile marker exists. A last-live backup is still kept.",
    )
    switch.add_argument("--no-open", action="store_true", help="Do not reopen Granola after switching.")
    switch.add_argument("--no-quit", action="store_true", help="Do not quit Granola before switching.")

    restore = subparsers.add_parser(
        "restore", help="Restore the last-live-before-switch backup into Granola's support folder."
    )
    restore.add_argument("--no-quit", action="store_true", help="Do not quit Granola before restoring.")
    restore.add_argument("--no-open", action="store_true", help="Do not reopen Granola after restoring.")

    open_parser = subparsers.add_parser("open", help="Open Granola.")
    open_parser.add_argument(
        "--profile",
        help="Switch to this profile first if it is not already active (quits Granola if needed).",
    )

    raycast_install = subparsers.add_parser(
        "raycast-install", help="Install Raycast script commands that call this CLI."
    )
    raycast_install.add_argument(
        "--dir",
        help="Directory to write the script commands into (default: <data-dir>/raycast).",
    )
    raycast_install.add_argument(
        "--profiles",
        default="work,personal",
        help="Comma-separated profile names to generate switch commands for (default: work,personal).",
    )

    return parser


def paths_from_args(args: argparse.Namespace) -> Paths:
    return Paths(live_dir=expand_path(args.live_dir), data_dir=expand_path(args.data_dir))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = paths_from_args(args)

    try:
        if args.command == "doctor":
            if args.fix:
                with switch_lock(paths):
                    if heal_live_dir(paths):
                        print("Recovered live Granola folder from the stored active profile.")
                    else:
                        print("No recovery needed.")
            print_doctor(paths)
            return 0
        if args.command == "selected":
            print_selected(paths)
            return 0
        if args.command == "setup":
            print_setup(paths)
            return 0
        if args.command == "raycast-install":
            names = [name.strip() for name in args.profiles.split(",") if name.strip()]
            if not names:
                raise SwitcherError("No profile names given for --profiles.")
            profiles = [(validate_profile_name(name), profile_display_name(name)) for name in names]
            target = expand_path(args.dir) if args.dir else paths.data_dir / "raycast"
            raycast.install_scripts(target, profiles)
            print(f"Raycast script commands written to: {target}")
            print("In Raycast: Settings > Extensions > Script Commands > Add Directories, then add that folder.")
            return 0
        if args.command == "status":
            print_status(paths)
            return 0
        if args.command == "open":
            if args.profile:
                target = validate_profile_name(args.profile)
                with switch_lock(paths):
                    if heal_live_dir(paths):
                        print("Recovered live Granola folder from the stored active profile.")
                    if active_profile(paths) == target:
                        open_app()
                    else:
                        if app_is_running():
                            quit_app()
                        switch_profile(paths, target, open_after=True)
            else:
                open_app()
            return 0

        with switch_lock(paths):
            if args.command == "capture":
                validate_profile_name(args.name)
                if not args.no_quit:
                    quit_app()
                save_profile(
                    paths,
                    args.name,
                    email=args.email,
                    label=args.label,
                    provider=args.provider,
                    mark_active=not args.no_mark_active,
                )
                print(f"Captured Granola profile: {args.name}")
                return 0
            if args.command == "switch":
                if heal_live_dir(paths):
                    print("Recovered live Granola folder from the stored active profile.")
                # Validate before quitting so a bad profile name does not
                # needlessly close a running Granola.
                ensure_can_switch(
                    paths,
                    args.name,
                    save_current=not args.no_save,
                    discard_current=args.discard_current,
                )
                if not args.no_quit:
                    quit_app()
                backup = switch_profile(
                    paths,
                    args.name,
                    save_current=not args.no_save,
                    discard_current=args.discard_current,
                    open_after=not args.no_open,
                )
                print(f"Switched Granola profile: {args.name}")
                if backup:
                    print(f"Previous live state backup: {backup}")
                return 0
            if args.command == "restore":
                if not args.no_quit:
                    quit_app()
                aside = restore_last_live(paths)
                print("Restored last-live backup into Granola's support folder.")
                if aside:
                    print(f"Previous live state archived: {aside}")
                print("Active profile marker cleared; capture or switch with --discard-current to set it.")
                if not args.no_open:
                    open_app()
                return 0
    except SwitcherError as error:
        print(f"granola-switcher: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() if isinstance(error.stderr, str) and error.stderr.strip() else error.cmd
        print(f"granola-switcher: command failed: {detail}", file=sys.stderr)
        return 1
    finally:
        print_timings()

    parser.error(f"Unhandled command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
