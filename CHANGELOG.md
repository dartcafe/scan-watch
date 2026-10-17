# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.2] - 2026-10-17

### Fixed

- Docker: `SCAN_WATCH_SNMP_COMMUNITY` from `.env` now reaches the container.

## [1.0.1] - 2026-10-17

### Added

- Prebuilt Docker image on Docker Hub: `dartcafedocker/scan-watch`.

## [1.0.0] - 2026-10-17

### Added

- `RESTART=no` in `.env`: the container does not start again after a reboot.
- Canon DR-S150 (experimental): after an automatic pause, inserting paper starts
  scanning again by itself. Needs SNMP v1 on the scanner and `SNMP community` in
  the configuration.
- The web page shows the scanner's driver when it has one of its own.

### Fixed

- Settings fixed in `.env` or by arguments are shown read-only when editing a
  profile, and a profile is shown as active even if it holds another value for them.

## [0.1.0] - 2026-10-13

First release.
