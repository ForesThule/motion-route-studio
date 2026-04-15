# Motion Route Studio

[![CI](https://github.com/ForesThule/emulator-move-simulator/actions/workflows/ci.yml/badge.svg)](https://github.com/ForesThule/emulator-move-simulator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-0f172a.svg)](./LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-b55434.svg)](https://www.python.org/)

Interactive GPS route simulation for `Android Emulator` and `iOS Simulator`.

`Motion Route Studio` lets you build a route on a map, shape how speed changes over time, and stream the resulting location updates into simulators with either a polished local web UI or a CLI workflow.

For Android it uses `adb emu geo fix`. For iOS it uses `xcrun simctl location set`.

## Highlights

- map-first local web UI with route editing and live preview
- CLI mode for repeatable scripts and automation
- support for both `Android Emulator` and booted `iOS Simulator`
- speed control by duration or average route speed
- separate start and stop curves
- per-segment speed overrides
- periodic whole-route speed modulation by curve, frequency, and amplitude
- dry-run mode for safe validation before sending anything to a simulator

## Why It Exists

Most simulator location tools can jump between points, but they do not feel great when you want believable movement. This project focuses on the missing layer between a static route and a realistic run:

- acceleration
- deceleration
- speed changes on specific route segments
- repeatable previews before execution

## Supported Curves

- `linear`
- `ease-in`
- `ease-out`
- `ease-in-out`
- `smoothstep`
- `smootherstep`
- `sine`

## Requirements

- `Python 3.9+`
- `adb` in `PATH` for Android support
- `xcrun` / Xcode Command Line Tools for iOS support
- a running `Android Emulator` or booted `iOS Simulator`
- a browser for the local web UI at `http://127.0.0.1:<port>`

## Quick Start

Show available curves:

```bash
python3 android_motion_emulator.py --list-curves
```

Launch the local UI:

```bash
python3 android_motion_emulator.py --gui
```

By default the UI is available at `http://127.0.0.1:8765`.

If the port is busy:

```bash
python3 android_motion_emulator.py --gui --port 8877
```

## UI Workflow

1. Open the local address printed in the terminal.
2. Click `Обновить список` and choose the platform.
3. Pick an active device, or enter a manual device ID.
4. Add route points by clicking on the map.
5. Choose `По длительности` or `По скорости`.
6. Open advanced speed settings if you need segment-level or modulation control.
7. Click `Предпросмотр` or `Запустить`.

## CLI Examples

Preview a route with separate acceleration and deceleration curves:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 90 \
  --interval 1 \
  --start-curve ease-in \
  --stop-curve ease-out \
  --start-share 0.25 \
  --stop-share 0.20 \
  --dry-run
```

Calculate a route by average speed:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --speed-kmh 24 \
  --interval 1 \
  --start-curve ease-in \
  --stop-curve ease-out \
  --start-share 0.25 \
  --stop-share 0.20 \
  --dry-run
```

Set speed on a specific segment:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 30 \
  --interval 2 \
  --segment-speed 1:12 \
  --dry-run
```

Add periodic speed modulation:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --speed-kmh 24 \
  --interval 2 \
  --variation-curve sine \
  --variation-frequency 0.25 \
  --variation-amplitude 20 \
  --dry-run
```

Run the route in a specific Android emulator:

```bash
python3 android_motion_emulator.py \
  --serial emulator-5554 \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 90 \
  --interval 1 \
  --start-curve smoothstep \
  --stop-curve ease-out \
  --start-share 0.30 \
  --stop-share 0.20
```

Run the same route in iOS Simulator:

```bash
python3 android_motion_emulator.py \
  --platform ios \
  --device-id 80131754-C016-41AB-8B65-824304B91EDD \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 90 \
  --interval 1 \
  --start-curve smoothstep \
  --stop-curve ease-out \
  --start-share 0.30 \
  --stop-share 0.20
```

## Motion Profile Model

- `start-curve` shapes the acceleration phase
- `stop-curve` shapes the braking phase
- `start-share` defines how much of the route timeline is reserved for the start
- `stop-share` defines how much of the route timeline is reserved for the finish
- the remaining section runs at the base motion level

When segment overrides or global modulation are enabled, the final duration is calculated from the actual resulting speed profile and may differ from the original base `duration`.

## What The UI Includes

- localized map with no API key required
- platform switch for `Android / iOS`
- active device listing
- manual device ID override
- route point list with manual coordinate editing
- point reordering
- duration, average speed, and interval controls
- separate start and stop curves
- per-segment speed configuration
- periodic whole-route speed modulation
- preview output and live execution log

## Development

Run the basic checks:

```bash
python3 -m py_compile android_motion_emulator.py
python3 -m unittest discover -s tests -p "test_*.py"
```

Or use the convenience targets:

```bash
make check
make test
```

## Repository Layout

```text
.
├── android_motion_emulator.py
├── tests/
├── .github/
├── README.md
├── CONTRIBUTING.md
├── SECURITY.md
└── LICENSE
```

## Limitations

- `geo fix` sets coordinates, not true GNSS telemetry
- realism depends on update interval and route density
- when multiple Android emulators are running, explicit `--serial` is safest
- very small `--interval` values may create visible load on `adb`
- the map uses OpenStreetMap tiles and should not be used for bulk prefetching
- only booted iOS simulators are shown in the UI
- `altitude` applies only to Android Emulator

## Contributing

Bug reports and pull requests are welcome. Start with [CONTRIBUTING.md](./CONTRIBUTING.md).

## Security

If you find a security issue, please use the guidance in [SECURITY.md](./SECURITY.md).

## License

This project is available under the [MIT License](./LICENSE).
