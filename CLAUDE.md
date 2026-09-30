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
| MatisseCDWorker ×N | One QThread per Matisse (`MatisseCDGroup`), TCP to Matisse Commander, optional | NO — signals `request_hf_lock` to Worker |

**Worker threads** are bound with `workers.bind_worker_thread` (finished->quit is a
DirectConnection -- a queued quit deadlocks against the GUI thread blocked in
`wait()`) and stopped with `workers.stop_worker_thread`; `closeEvent` skips the
exit config save if the WLM thread did not finish (test: `tests/test_wlm_thread_shutdown.py`).

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
| `matisse_cd.py` | Optional Matisse CounterDrift offload: `MatisseCommanderClient` (TCP), `MatisseCDWorker`, `MatisseCDPanel`, config `matisse_cd_config.json` (gitignored) |

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

### Matisse CounterDrift (digital lock, `matisse_cd.py`, 2026-09-29)

Alternative to the analog WS7-PID -> Matisse feedback: Matisse Commander's
wavemeter plugin (CounterDrift) holds the laser; HF_Locking only moves its
setpoint. Toggle per laser in the "Matisse CounterDrift" panel; the HF channel
of each TiSa is user-set there (persisted). `ENABLE_MATISSE_CD` in `main_wlm.py`
turns the whole feature off. Front end is THz everywhere; nm only on the MC wire.

- **Setpoint path:** Set F / ZMQ `PROGRAM_VALUE` -> `handle_setpoint_write` (WS7
  course setpoint as before) -> `setpoint_committed(port, THz)` ->
  CounterDrift `Setpoint <vacuum nm>`. CHECK_VALUE, plots, wait_for_lock unchanged.
- **Mutual exclusion:** activation sets status `cd_active` first, then switches
  the WS7 PID for that port OFF via `request_hf_lock`; `handle_lock_toggle`
  refuses to re-arm while `cd_active`. Slow poll warns if the WS7 lock is
  re-enabled externally.
- **ZMQ wait_for_lock** gate is `(lock_enabled and deviation_mode) or cd_active`;
  convergence still judged on the HF measurement with `lock_tolerance(port)`.
- **Interlocks:** activation refused unless MC's `MCP_WM_GET_WAVELENGTH` agrees
  with c/f_HF within `max_mismatch_mhz` (air/vacuum, wrong switch channel) AND
  |f_HF - WS7 setpoint| <= `max_activation_offset_mhz`. The port is claimed
  (`CDRegistry`; `cd_active` = OR over lasers) BEFORE lock state is read fresh;
  a failed activation restores the HF lock it switched off.
- **Setpoint delivery:** a failed forward stays pending, is retried every poll,
  and the panel shows "SP NOT DELIVERED".
- **Watchdog:** |f_HF - SP| > `runaway_mhz` for `runaway_s` -> CD deactivated.
- **Error replies:** `Error`/`Err` anywhere, `!...`, or DSP `N,"..."` with N != 0.
  Transport errors close the socket (no stale-reply desync); writes retry once,
  wavelength poll and plugin open (`open_timeout_s`=120) never retry.
- Fight state (CD active AND WS7 lock on): channel button turns orange and stays
  clickable to switch the HF lock OFF.
- **OFF must be confirmed:** if `Activate false` is not acked (MC hung), the port
  stays claimed (`off_unconfirmed`, HF lock blocked / not restored, panel "CD OFF?
  UNCONFIRMED") and OFF is retried each poll; connection refused (MC not running)
  counts as OFF. A client `abort()` (shutdown) is terminal: no reconnect/retry.
- Watchdog cannot act while MC is unreachable (it could not deactivate anyway).
- Adversarial reviews: 2026-09-29 (Opus) -> `tests/test_matisse_review_fixes.py`;
  2026-09-30 (Fable, b98c91d..20b1a78) -> `tests/test_matisse_review2_fixes.py` + F1
  in `tests/test_matisse_cd_bringup.py`.
- Closing HF_Locking leaves CD running in MC; the flag is persisted and blocks
  the HF lock on next start until the user Deactivates.
- Tests: `tests/test_matisse_cd.py` (module, fake MC TCP server),
  `tests/test_matisse_integration.py` (real SharedState/WavemeterWorker/
  ChannelControl + `wire_into_hf`, written test-first; I1-I3/W1-W2 verified
  RED against pre-CD `workers.py`/`display.py`), I4 in `test_zmq_v2_protocol.py`.
  Wiring lives in `matisse_cd.wire_into_hf` (all QueuedConnection) -- extend there.
- **Lab bring-up:** `tools/matisse_cd_bringup.py` -- A (read-only: vacuum/air/
  wrong-channel check), B `--go` (activate at current f, one step + back: proves
  the decimal separator), C `--go` (settle times per step -> tune `runaway_s`,
  `LOCK_TIMEOUT_S`; hold std). Runaway/no-signal guards; CD always switched OFF at
  exit. Logic pinned against a simulated laser in `tests/test_matisse_cd_bringup.py`.
  NOTE: HF_Locking's PUB repeats the LAST GOOD value when the WS7 loses the line,
  so the tool treats an identical reading for `--frozen-s` (3 s) as signal lost;
  phase A measures the longest normal identical-reading run to validate that.
- Wire: LabVIEW length-prefixed framing + `#SERVER ` prefix (from a
  collaborator's `matisse_cd_controller.py`). Probe: `python matisse_cd.py --probe host port`.
- **UNVERIFIED on our hardware:** CD setpoint is vacuum nm; LabVIEW decimal
  separator (`decimal_sep`); `MCP_WM_GET_WAVELENGTH` returns nm per collaborator
  (air/vac unstated; behavior with plugin closed unknown); TiSa-2 MC
  server port (default 30001); CD loop bandwidth/capture range vs analog PID;
  whether MC ever replies with "error" wording on success; `socket.shutdown`
  unblocking a recv in another thread on Windows (verified on Linux only).

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
