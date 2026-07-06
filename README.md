# Granola Switcher

Fast local account switching for the [Granola](https://www.granola.ai) Mac app.

Granola stores its signed-in account state in `~/Library/Application Support/Granola`. This utility captures that folder once per account, then switches accounts in about a second by quitting Granola, rotating the active profile directory with the target profile directory, and reopening Granola.

> **Disclaimer**: This is an unofficial tool, not affiliated with or endorsed by Granola. Granola is a trademark of its owner. The switcher relocates Granola's local auth/session state on your own machine; use at your own risk. The switcher's data folder (`~/Library/Application Support/granola-switcher`) contains live login credentials for your captured accounts — never commit, sync, or share it.

## Install

```sh
brew install yanda/tap/granola-switcher
```

Or from a source checkout: clone this repo and use `./bin/granola-switcher` anywhere the commands below say `granola-switcher`.

## Setup

Check your local paths first:

```sh
granola-switcher doctor
granola-switcher status
```

Capture the account currently open in Granola. If the current account is personal:

```sh
granola-switcher capture personal --email you@personal.example --label "Personal" --provider google
```

Then open Granola, sign out, sign into the work account, and capture it:

```sh
granola-switcher capture work --email you@work.example --label "Work" --provider microsoft
```

If the current account is work, do those two capture commands in the opposite order.

## Switch

```sh
granola-switcher switch work
granola-switcher switch personal
```

`switch` saves the currently active profile before restoring the target profile. On the normal same-disk setup it uses fast directory moves instead of copying the whole Granola folder, so a full switch takes about a second plus Granola's own relaunch time. Set `GRANOLA_SWITCHER_TIMINGS=1` to print per-phase timings to stderr.

After a fast switch, `status` shows one profile as `live` and the other as `stored`. That is expected: the active profile lives in Granola's normal support folder, and the inactive profile waits under the switcher data folder.

Granola has no working graceful-quit path (the AppleScript quit event is accepted but ignored, and SIGTERM only kills helper processes that the main process respawns), so the switcher sends the quit event, waits a short grace window, then force-quits.

## Raycast

```sh
granola-switcher raycast-install
```

This writes script commands (`Granola: Work`, `Granola: Personal`, `Granola: Selected Account`) into the switcher's data folder and prints the path. Then, once, in Raycast: **Settings > Extensions > Script Commands > Add Directories**, and add that folder. Use `--profiles` to generate commands for different profile names, or `--dir` to write somewhere else. Re-run the command any time to regenerate the scripts.

From a source checkout you can instead add this repo's `raycast/` folder directly as a Raycast script directory.

## First run and permissions

The first time the switcher quits Granola from a given app, macOS shows an Automation permission prompt ("… wants to control Granola"). The permission is per calling app: your terminal and Raycast each prompt once. Allow it in **System Settings > Privacy & Security > Automation**. If the prompt is dismissed or denied, switching still works — the quit request is skipped and the switcher falls back to force-quitting, which is its normal path anyway. `granola-switcher doctor` reports automation status.

## Backups and recovery

If you switch with `--no-save`, the previous live state is kept at:

```text
~/Library/Application Support/granola-switcher/backups/last-live-before-switch
```

Bring that backup back with:

```sh
granola-switcher restore
```

`restore` swaps the backup into Granola's support folder, archives whatever was live, and clears the active profile marker (capture or `switch --discard-current` to set it again).

Re-captures and first-switch duplicates are archived under `backups/replaced-profile-copies`; only the two newest archives per profile are kept, so backups stay bounded.

If a switch is ever interrupted mid-flight (crash, power loss), the next `switch` repairs the live folder automatically; `granola-switcher doctor --fix` does the same on demand.

## Useful commands

```sh
granola-switcher setup
granola-switcher selected
granola-switcher status
granola-switcher capture personal --email you@personal.example --label "Personal" --provider google
granola-switcher capture work --email you@work.example --label "Work" --provider microsoft
granola-switcher switch personal
granola-switcher switch work
granola-switcher restore
granola-switcher raycast-install
granola-switcher doctor --fix
```

If you need to switch before the active profile marker exists, use:

```sh
granola-switcher switch work --discard-current
```

Prefer capturing the currently signed-in account first; that keeps both account states recoverable.

## Development

```sh
git clone https://github.com/yanda/granola-switcher
cd granola-switcher
make test
./bin/granola-switcher doctor
```

## License

MIT — see [LICENSE](LICENSE).
