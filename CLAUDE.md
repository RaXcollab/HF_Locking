# HF_Locking — Claude Code Project Instructions

## What This Is

PyQt5 GUI controlling a **High Finesse WS7-30** wavemeter via `wlmData.dll` (ctypes). Monitors and locks up to 8 laser channels. Communicates with BLACS/labscript via ZMQ for automated experiment control.

## How to Run

- Conda env **`guis`** (NOT `labscript`):
  `source ~/miniconda/etc/profile.d/conda.sh && conda activate guis && python main_wlm.py`

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
- **SharedState → GUI:** PULL model — GUI timers read snapshots (fast @ 33ms/~30FPS, slow @ 500ms)
- **GUI → Worker:** PUSH via `QueuedConnection` signals (thread-safe, non-blocking)
- **Write handlers:** Full DLL read-back + delta emit for immediate UI feedback

### Key Design Decisions

- **PULL model** (not PUSH) to avoid signal queue backlog causing UI freeze
- **Re-entrancy guard** (`_busy_fast`) on `_poll_fast()` — skips if previous poll still running
- **Pending guards** (1s) on UI inputs — prevents clobber before DLL confirms
- **Frequency normalization:** Handles `InfNothingChanged` (-7) sentinel gracefully
- **Config persistence:** JSON with atomic writes, read-before-write, user-approved restore dialog
- **Plot x-axis uses cycle-shift** — do NOT use raw `% 60` (breaks clipToView). See `update_fast()` in display.py.
- **clipToView enabled** — x-data must stay monotonic or clipping breaks. Y-autoscale must scope to visible window only.

## File Map

| File | Purpose |
|---|---|
| `main_wlm.py` | Main entry point. `ExperimentController` (QMainWindow), `_RestoreDialog`, channel config, signal wiring |
| `workers.py` | `SharedExperimentState`, `WavemeterWorker` (polling + write handlers), `ZMQPubWorker`, `ZMQRepWorker` |
| `display.py` | `ChannelControl` (per-channel UI: plots, setpoint, voltage, lock), `GlobalControl` (T/P/autocal/deviation/save) |
| `wlm_utils.py` | `wlm_link` class — all DLL wrappers (frequency, setpoint, PID, bounds, switching, etc.) |
| `config.py` | PID config persistence + WLM app config backup (`backup_wlm_config`) |
| `wlmConst.py` | DLL constants (read-only, ~500 constants). PID constants at lines 217-237 |
| `wlmData.py` | DLL function signatures via ctypes (read-only). PID signatures at lines 619-645 |
| `diagnostics.py` | Optional timing instrumentation (disabled by default, `ENABLED=False`) |

## Channel Configuration

```python
CHANNEL_NAMES = {
    1: "TiSa_1",  2: "Ch_2",    3: "Vexlum",  4: "Ch_4",
    5: "Ch_5",    6: "Ch_6",    7: "Ch7",      8: "Rb_Ref",
}
# TiSa_1 moved ch4 -> ch1 on 2026-07-29 (crosstalk). Channel-move checklist:
# ~/labscript-suite/docs/wavemeter-channel-move.md
PORTS = range(1, 9)
```

## BLACS Integration

- **Matisse channels (port 1 TiSa_1 — was port 4 until 2026-07-29 — and port 6 TiSa-2):** remote freq control is via the Matisse **Network Server SCPI** (`SCAN:NOW`/`REFERENCECELL:NOW`, LabVIEW length-prefixed framing) — **NOT UI automation** (LabVIEW canvas exposes 0 UIA/Win32 controls). Probe: `tools/matisse_scpi_probe.py`. Findings + unverified list: `docs/matisse-c-external-locking.md` (2026-07-15).

### ZMQ Protocol

**v2 protocol** (2026-05-23): REQ-REP envelope is JSON with `id`/`status`
enum/`error.{code,message,retryable}` — see canonical spec
[`docs/remotecontrol-zmq-protocol-v2.md`](../../docs/remotecontrol-zmq-protocol-v2.md).
`ZMQRepWorker(QThread)` owns the QThread loop; an inner
`_LaserLockV2Server(RemoteControlServerBase)` (imported from parent's
`userlib/external_gui_lib/zmq_v2.py`) dispatches via `@handler` methods.

**REP/REQ (port 3796)** — actions:
- `HELLO` — connection check; advertises
  `capabilities=["heartbeat", "monitors", "wait_for_lock"]`. No
  `connections` key (single-instance server).
- `PROGRAM_VALUE` — write setpoint. `wait_for_lock` lives in v2 `args`
  dict (NOT top-level per Q2). On timeout returns v2 `TIMEOUT` status
  with `error.retryable=True`. Silent lock-bypass (wait=True but
  `lock_enabled`/`deviation_mode` False) is logged as WARNING.
- `CHECK_VALUE` — read current setpoint from `SharedExperimentState`.
  Uninitialized port returns `UNKNOWN_CONNECTION` /
  `setpoint_not_initialized` (NOT 0.0).
- Port range validated (`1`..`8`); out-of-range returns
  `UNKNOWN_CONNECTION` / `port_out_of_range`.

**PUB (port 3797)** — `ZMQPubWorker` broadcasts:
- `heartbeat` string (~10 Hz)
- `"{port} {freq_display}"` per port (legacy bare-integer topic; kept
  for spec-cascade avoidance per Q2 vs Q4 resolution — NOT migrated to
  the spec §4.1 `{conn}_{param}_monitor` form because BLACS-side
  `RemoteAnalogMonitor` declares `connection=<int>` and the labscript
  connection-table cascade is out of scope).

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
- `LOCK_CONSECUTIVE` requires **5** consecutive in-tol readings (per `LOCK_CONSECUTIVE` in `workers.py`; tol default `LOCK_TOLERANCE=5e-6 THz = 5 MHz`, per-channel overrides in `LOCK_TOLERANCE_BY_PORT` — TiSa_1 ch1 = 1e-6 THz = 1 MHz, resolved via `lock_tolerance(port)`; timeout `LOCK_TIMEOUT_S=60`)
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
- WLM install dir: `C:\Program Files (x86)\HighFinesse\Wavelength Meter WS7 8407\`
- WLM app config: `wlm_ws7.ini` (all settings), `WLM8407ST.stn` (calibration), `history.8407` (cal history)
- "Backup WLM" button copies these 3 files to `wlm_backups/<timestamp>/` — restore is manual

## Coding Conventions

- Python 3, PyQt5, pyqtgraph for plots
- DLL calls use ctypes (`c_long`, `c_double`, `byref`)
- Signals use `@pyqtSlot` decorators and `QueuedConnection` for cross-thread
- Setpoint string format: comma decimal separator for DLL (`"348,666410000"`)
- Console logging: `[CONFIG]`, `[WLM]`, `[ZMQ PUB]`, `[ZMQ REP]` prefixes
- Unit tests: `pytest tests -q` in the `guis` env (mock-based — no hardware, no Qt loop, no ZMQ binds; see `tests/conftest.py`). Hardware behavior is still verified manually against the live wavemeter

## Known TODOs in Code

- `diagnostics.py`: Disabled (`ENABLED=False`) — available for performance tuning
