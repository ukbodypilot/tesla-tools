# Changelog

All notable changes to tesla-tools. The version is the git tag.

## [Unreleased]

- **Fix: a false "parked and UNLOCKED" alert, and a red lock lamp, right after parking.** The read
  that sees Park can land before walk-away lock has locked the doors. An unlocked, empty, parked
  reading now gets a second read 3 minutes later (`UNLOCK_CONFIRM`), and only a car still unlocked
  then is alerted; a car that falls asleep first is left alone. Events log both outcomes.
- **Lamps say how old their reading is.** Past 10 minutes, the lock lamp shows the reading's time
  ("Unlocked 11:55") and its tooltip the full date: readings can be hours old by design.
- **Away-from-home link in alert emails** (`away_link_file`, `away_link_path`): the radio gateway's
  current short tunnel link to this page, read per email.
- **Clearer "remote access is off" page.** It explains that a tunnel link counts as outside even on
  home Wi-Fi, and where to set the password.

## [1.0.0] -- 2026-11-26

First tagged release.

- **A sleep-safe Tesla dashboard inside the free API credit.** Reads the car only while it is already
  awake, never keeps it awake, and wakes it at most once a day; a monthly budget guard stops all
  billed calls before the cap.
- **Charging and Supercharging history**, with savings against petrol.
- **Alerts & Service**: the car's alert history, service visits, software updates and nearby
  Superchargers, each fetched on its own slow interval with its own cost metric.
- **Wake & read works**: requests send a JSON Content-Type (Tesla answered 406 without it), the wake
  slot survives restarts, an asleep car shows Sentry off, and the button waits for the new reading.
- **Status lights**: Asleep has its own blue light; the plug light reads "Plugged".
- **The rig-panel house style** shared with radio-gateway, lan-inventory and Family Locator, and an
  optional Apps menu (`apps = [...]` in `config.toml`; empty = no menu).
