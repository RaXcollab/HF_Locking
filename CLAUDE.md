# HF_Locking — Claude Code Project Instructions

## What This Is

PyQt5 GUI controlling a **High Finesse WS7-30** wavemeter via `wlmData.dll` (ctypes). Monitors and locks up to 8 laser channels. Communicates with BLACS/labscript via ZMQ for automated experiment control.

## Architecture

### Threading Model (CRITICAL)

| Thread | Role | DLL Access? |
|---|---|---|
| Main (GUI) | PyQt5 event loop, PULL-based refresh timers | Startup + shutdown ONLY |
| WavemeterWorker | All runtime DLL I/O (polling + write handlers) | YES (primary owner) |
| ZMQRepWorker | BLACS REQ/REP commands (port 3796) | NO — signals to Worker |
| ZMQPubWorker | Publishes measurements (port 3797) | NO — reads SharedState |

**DLL Thread Safety Rule:** `wlm_link` has NO mutex. The WavemeterWorker thread owns all DLL calls during runtime. Main thread DLL access is ONLY safe when the worker is not running (before `thread_wlm.start()` at startup, after `thread_wlm.wait()` at shutdown). Any new feature requiring DLL access during runtime MUST route through the worker thread via `QueuedConnection` signal. Violating this will corrupt data — the DLL may interleave calls across ports.

### Data Flow

- **Worker → SharedState:** Mutex-protected `SharedExperimentState` (single `QMutex`)
- **SharedState → GUI:** PULL model — GUI timers read snapshots (fast @ 30ms, slow @ 500ms)
- **GUI → Worker:** PUSH via `QueuedConnection` signals (thread-safe, non-blocking)
- **Write handlers:** Full DLL read-back + delta emit for immediate UI feedback

### Key Design Decisions

- **PULL model** (not PUSH) to avoid signal queue backlog causing UI freeze
- **Re-entrancy guard** (`_busy_fast`) on `_poll_fast()` — skips if previous poll still running
- **Pending guards** (1s) on UI inputs — prevents clobber before DLL confirms
- **Frequency normalization:** Handles `InfNothingChanged` (-7) sentinel gracefully
- **Config persistence:** JSON with atomic writes, read-before-write, user-approved restore dialog

## File Map

| File | Purpose |
|---|---|
| `main_wlm.py` | Main entry point. `ExperimentController` (QMainWindow), `_RestoreDialog`, channel config, signal wiring |
| `workers.py` | `SharedExperimentState`, `WavemeterWorker` (polling + write handlers), `ZMQPubWorker`, `ZMQRepWorker` |
| `display.py` | `ChannelControl` (per-channel UI: plots, setpoint, voltage, lock), `GlobalControl` (T/P/autocal/deviation/save) |
| `wlm_utils.py` | `wlm_link` class — all DLL wrappers (frequency, setpoint, PID, bounds, switching, etc.) |
| `config.py` | PID config persistence — `save_config`, `load_config`, `compare_configs`, `restore_settings` |
| `wlmConst.py` | DLL constants (read-only, ~500 constants). PID constants at lines 217-237 |
| `wlmData.py` | DLL function signatures via ctypes (read-only). PID signatures at lines 619-645 |
| `diagnostics.py` | Optional timing instrumentation (disabled by default, `ENABLED=False`) |
| `display_wide.py` | Wide-layout variant of display.py (simpler, no offset plots) |
| `main_wlm_wide.py` | Wide-layout variant — **incomplete**, lacks config persistence |

## Channel Configuration

```python
CHANNEL_NAMES = {
    1: "Ch_1",    2: "Ch_2",    3: "Vexlum",  4: "TiSa_1",
    5: "Ch_5",    6: "Ch_6",    7: "Ch7",      8: "Rb_Ref",
}
PORTS = range(1, 9)
```

## BLACS Integration

### ZMQ Protocol

**REP/REQ (port 3796)** — `ZMQRepWorker` handles:
- `HELLO` — connection check
- `PROGRAM_VALUE` — write setpoint, optionally wait for lock convergence (up to 60s)
- `CHECK_VALUE` — read current setpoint from `SharedExperimentState`

**PUB (port 3797)** — `ZMQPubWorker` broadcasts:
- `heartbeat` string (~10 Hz)
- `"{port} {freq_display}"` per port

### BLACS-Side Device Classes (in `~/labscript-suite/userlib/user_devices/`)

- `RemoteControl` — Base device class for all remote GUI integration
  - `RemoteAnalogOut` — writable output channel
  - `RemoteAnalogMonitor` — read-only monitor channel
- `LaserLockDevice(RemoteControl)` — Pure subclass, maps to `LaserLockTab` with paired setpoint+monitor layout, frequency error display, lock quality indicators (100 MHz threshold)
- `RemoteControlWorker` — BLACS worker subprocess: `program_manual`, `transition_to_buffered` (with `wait_for_lock`), `check_remote_values`, HDF5 monitor snapshots
- `RemoteControlTab` — BLACS tab: spinbox widgets, PUB-SUB heartbeat/data subscriber threads, reconnect logic
- `RemoteCommunication` — ZMQ REQ socket manager with timeout handling and socket reset

### BLACS Communication Contract (`BLACS_COMMUNICATION_CONTRACT.md`)

- General timeout: 5s (`DEFAULT_TIMEOUT_MS`)
- Buffered mode with lock-wait: 120s (`PROGRAM_TIMEOUT_MS`)
- BLACS reads setpoints via `CHECK_VALUE` from `SharedExperimentState` (DLL readback), not GUI text boxes
- ZMQ-originated writes do NOT trigger the GUI pending guard
- `handle_setpoint_write` updates `SharedExperimentState` BEFORE emitting signal — no stale-read on slow refresh

### Verified Facts (from BLACS expert audit)

- Pending guard (display.py) is a **non-issue** for remote writes — only triggers on local "Set F" clicks
- Status delta merge is a **non-issue** — SharedState updated before signal emit
- Lock-wait timing: 100ms poll absorbs ~1ms queued signal latency — first poll sees new setpoint
- `LOCK_CONSECUTIVE` requires **2** consecutive readings (not 3)
- Silent rejection of setpoints < 1.0 THz is low-risk — BLACS spinbox limits enforce valid ranges

## PID Config Persistence

Settings saved per channel to `pid_config.json` (gitignored):
- **PID gains:** P, I, D, T, dt (double via `GetPIDSetting`)
- **Deviation:** Polarity, SensitivityFactor/Dim/Ex, Unit, Channel, UseTa, Constdt, AutoClearHistory, ClearHistoryOnRangeExceed (int via `GetPIDSetting`)
- **Bounds (double):** BoundsMin, BoundsMax, RefAt (double via `GetLaserControlSetting`)
- **Bounds (int):** RefMid (integer via `GetLaserControlSetting` — 1=centered, 0=explicit per WS7 manual p.131)
- **Setpoint:** course value (via `GetPIDCourseNum`)

Setting registries defined in `config.py` (`PID_DOUBLE_SETTINGS`, `PID_INT_SETTINGS`, `LC_DOUBLE_SETTINGS`, `LC_INT_SETTINGS`).

### PID Formula (from WS7 manual p.49)

`output = S * [P*error + I'*integral(error) + D'*derivative(error)]` where:
- When `UseTa=1`: `I' = I/ta`, `D' = D*ta` (recommended: `ta = 2*dt`)
- When `UseTa=0`: `I' = I`, `D' = D`
- Recommended starting values: P=0.16, I=0.84, D=0.03

### Hardware Reference

- WS7 manual: `Manual WS7 NeLAC (1).pdf` in project root
- WS7 native app persists settings between sessions via INI, but DLL-set values at runtime may NOT be saved back to INI — this is why `config.py` exists

## Coding Conventions

- Python 3, PyQt5, pyqtgraph for plots
- DLL calls use ctypes (`c_long`, `c_double`, `byref`)
- Signals use `@pyqtSlot` decorators and `QueuedConnection` for cross-thread
- Setpoint string format: comma decimal separator for DLL (`"348,666410000"`)
- Console logging: `[CONFIG]`, `[WLM]`, `[ZMQ PUB]`, `[ZMQ REP]` prefixes
- No unit tests — verification is manual against live hardware

## Known TODOs in Code

- `wlm_utils.py`: `set_deviation_bounds()` not implemented (line 254)
- `wlm_utils.py`: `GetAutoCalSetting` not implemented (line 71)
- `main_wlm_wide.py`: Missing config persistence, incomplete signal connections — not production-ready
- `diagnostics.py`: Disabled (`ENABLED=False`) — available for performance tuning
