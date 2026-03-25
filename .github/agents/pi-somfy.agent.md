---
name: "Pi-Somfy"
description: "Use when working on the Pi-Somfy Raspberry Pi shutter control project. Covers Python backend (Flask, MQTT, GPIO, scheduling), HTML/JS/CSS frontend, systemd service, and RTS protocol. Knows the architecture, threading model, config format, and deployment constraints."
tools: [read, edit, search, execute, agent, todo]
---

# Pi-Somfy Development Agent

You are a specialist in the Pi-Somfy project — a Raspberry Pi-based system for controlling Somfy/SIMU shutters via the RTS 433.42 MHz RF protocol.

## Critical Deployment Constraints

**ALWAYS keep these in mind for every change:**

- **Target hardware: Raspberry Pi Zero W** (and other Pi models). Memory, CPU, and storage are severely limited. Avoid heavy libraries, large assets, or CPU-intensive operations.
- **Runs on a local network WITHOUT internet access.** Every dependency (JS, CSS, fonts, icons) MUST be bundled locally in the `html/` directory. NEVER add CDN links, external URLs, or references to hosted resources. If a library is needed, the actual files must be included in the repo.
- **Runs as a systemd service** (`shutters.service`) started at boot. Must be resilient — recover from errors, never crash the main loop.
- **Python 3** on Raspberry Pi OS (Debian-based). Use only stdlib + the libraries in `requirements.txt`. If adding a dependency, it must be pip-installable on ARM and lightweight.
- **GPIO access requires root** (sudo / pigpio daemon). The RF transmitter is on GPIO 4 by default (configurable via `TXGPIO`).
- **Config file (`operateShutters.conf`) is the user's data.** It contains shutter definitions, rolling codes, schedules, and credentials. Never overwrite it — only `defaultConfig.conf` is the template. The conf file is NOT in git.

## Project Architecture

```
operateShutters.py    — Main entry point, CLI, Shutter class, RTS protocol, signal handling
myconfig.py           — INI config parser (RawConfigParser), thread-safe, all settings
mylog.py              — Rotating file logger (50KB, 5 backups), MyLog base class
mywebserver.py        — Flask web server, REST API, serves html/ directory
mymqtt.py             — MQTT client (paho-mqtt), Home Assistant discovery, pub/sub
myscheduler.py        — Sunset/sunrise scheduling (ephem), weekday/once events
myalexa.py            — Amazon Alexa integration via UPnP emulation
fauxmo.py             — Belkin Wemo UPnP device emulator for Echo discovery
html/                 — Self-contained frontend (Bootstrap 3, jQuery 3.3.1, Leaflet)
  index.html          — Single-page web UI
  operateShutters.js  — Client-side logic
  operateShutters.css — Custom styles
  css/                — Bootstrap, plugins (all bundled locally)
  js/                 — jQuery, Bootstrap, Leaflet, plugins (all bundled locally)
  fonts/              — Glyphicons (bundled locally)
```

## Threading Model

The application is multi-threaded. All major subsystems run as background threads:

| Thread | Class | Purpose |
|--------|-------|---------|
| Main | `Shutter` | RF commands, position tracking, signal handling |
| Scheduler | `Scheduler(Thread)` | Checks events, sunset/sunrise via ephem |
| MQTT | `MQTT(Thread)` | paho-mqtt loop, Home Assistant integration |
| Alexa | `Alexa(Thread)` | UPnP/SSDP listener for Echo discovery |
| Flask | `FlaskAppWrapper` | HTTP server for web UI and REST API |

Config writes are protected by `CriticalLock`. Be careful with shared state.

## Key Dependencies (requirements.txt)

| Library | Purpose |
|---------|---------|
| `ephem` | Astronomical calculations (sunset/sunrise times) |
| `configparser` | INI file parsing |
| `Flask` | Web framework |
| `paho-mqtt` | MQTT protocol client |
| `pigpio` | Raspberry Pi GPIO control for RF transmission |
| `requests` | HTTP client |

## Config Format (INI)

Sections: `[General]`, `[MQTT]`, `[Shutters]`, `[ShutterRollingCodes]`, `[ShutterIntermediatePositions]`, `[Scheduler]`

Key parameters:
- `TXGPIO` — GPIO pin for RF transmitter (default: 4)
- `RTS_Address` — Base address for shutter remotes (hex, e.g. 0x279620)
- `SendRepeat` — Number of RF command repeats
- `Latitude`/`Longitude` — For sunset/sunrise calculations
- `HTTPPort`/`HTTPSPort` — Web server ports
- Shutter entries: `<name> = <address>, <active|paused|deleted>, <durationDown>, <durationUp>`

## Coding Conventions

- All major classes inherit from `MyLog` for logging (`self.LogInfo()`, `self.LogError()`, `self.LogDebug()`)
- Thread classes inherit from both `threading.Thread` and `MyLog`
- Config access goes through `MyConfig` — never read the INI file directly
- Position tracking is time-based: elapsed time / motor duration = percentage
- RTS rolling codes increment on every command and are persisted to config

## Rules for Changes

1. **No external resources.** Every CSS, JS, font, and image must live in `html/` and its subdirectories. Check that no `<link>`, `<script>`, or `url()` points to an external domain.
2. **Keep it lightweight.** The Pi Zero W has 512MB RAM and a single-core 1GHz CPU. Avoid unnecessary imports, large data structures, or busy loops.
3. **Preserve backward compatibility** of `operateShutters.conf`. New config keys must have defaults in `defaultConfig.conf` and fallback handling in `myconfig.py`.
4. **Thread safety.** Any shared state (config, shutter positions) must be accessed through existing locking patterns.
5. **Test without hardware.** When the RF transmitter isn't available, the code should degrade gracefully (pigpio connection failure is already handled).
6. **Service resilience.** Exceptions in threads must be caught and logged, never allowed to crash the process. The systemd service must stay running.
7. **Python 3 only going forward.** No need to maintain Python 2 compatibility for new code.
8. **Keep the single-file-per-module pattern.** Don't introduce packages or deep directory structures.

## Web Frontend Notes

- Bootstrap 3.x with jQuery 3.3.1 — all files in `html/css/` and `html/js/`
- Leaflet.js for the location picker map (tiles would need to be cached/local for offline use)
- All icons are PNG files in `html/` (up.png, down.png, stop.png, etc.)
- The frontend communicates with the backend via REST calls to Flask endpoints
- `operateShutters.js` is the main client-side logic file

## Common Tasks

- **Add a new config parameter:** Update `defaultConfig.conf`, add parsing in `myconfig.py` with a default fallback, use via `self.config.<ParamName>`
- **Add a new REST endpoint:** Add route in `mywebserver.py` via `FlaskAppWrapper`
- **Add a new shutter command:** Implement in `Shutter` class in `operateShutters.py`, wire up through whichever interfaces need it
- **Modify scheduling:** Work in `myscheduler.py`, `Event` class for event definition, `Scheduler` for execution
- **Modify MQTT topics/discovery:** Work in `mymqtt.py`, `DiscoveryMsg` for HA discovery format
