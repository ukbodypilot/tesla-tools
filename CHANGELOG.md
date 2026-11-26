# Changelog

All notable changes to tesla-tools. The version is the git tag.

## [Unreleased]

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
