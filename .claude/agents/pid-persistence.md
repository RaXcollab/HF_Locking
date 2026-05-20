# Task: PID Config Persistence for HF_Locking GUI

> **STATUS** (verify before treating as live spec): drafted 2026-02-25. No `pid_config.json` file found in `GUIs/HF_Locking/` as of 2026-05-19 audit, suggesting the spec was not implemented. Either (a) task is still open and this is the live design, or (b) abandoned. Confirm with user before doing major work against this spec.

## Background

The HF_Locking GUI controls a High Finesse WS/7-30 wavemeter via DLL calls (`wlmData.dll`). After a PC restart on Feb 25, 2026, all custom PID tuning (gains, polarity, sensitivity, setpoints) was lost because:

1. The WS7 native app stores config in `wlm_ws7.ini` (via Windows VirtualStore at `C:\Users\radmo\AppData\Local\VirtualStore\Program Files (x86)\HighFinesse\Wavelength Meter WS7 8407\`), but it does NOT reliably persist PID gains set via the DLL at runtime.
2. The HF_Locking Python GUI has **zero config persistence** — all values are read from the WLM hardware on startup and lost on shutdown.
3. The existing `get_pid_settings()` function in `wlm_utils.py:156-160` is **stubbed** — it calls the DLL but doesn't return the result.

## Goal

Add config persistence so that all per-channel PID settings are saved to a JSON file and can be restored after a restart. Follow the **read-before-write** principle: never blind-write from JSON — always read current DLL state first and compare.

## DLL Interface to Complete (`wlm_utils.py`)

### Available DLL Functions

```
GetPIDSetting(PS, Port, iSet, dSet)   — reads a PID setting
SetPIDSetting(PS, Port, iSet, dSet)   — writes a PID setting
GetLaserControlSetting(PS, Port, iSet, dSet, sSet) — reads laser control settings (bounds, etc.)
SetLaserControlSetting(PS, Port, iSet, dSet, sSet) — writes laser control settings
GetPIDCourseNum(Port, PIDC)           — reads setpoint (already implemented)
SetPIDCourseNum(Port, PIDC)           — writes setpoint (already implemented)
```

### Constants from `wlmConst.py`

**PID gains** (use `GetPIDSetting` / `SetPIDSetting`, value in `dSet`):
| Constant | Value | Parameter |
|---|---|---|
| `cmiPID_P` | 1034 | Proportional gain |
| `cmiPID_I` | 1035 | Integral gain |
| `cmiPID_D` | 1036 | Derivative gain |
| `cmiPID_T` | 1033 | T constant |
| `cmiPID_dt` | 1060 | dt |

**Integer settings** (use `GetPIDSetting` / `SetPIDSetting`, value in `iSet`):
| Constant | Value | Parameter |
|---|---|---|
| `cmiDeviationPolarity` | 1038 | Polarity (+1 normal, -1 inverted) |
| `cmiDeviationSensitivityDim` | 1040 | Sensitivity dimension |
| `cmiDeviationSensitivityEx` | 1039 | Extended sensitivity |
| `cmiDeviationUnit` | 1041 | Unit (0=nm, 2=THz) |
| `cmiDeviationChannel` | 1063 | Signal source channel assignment |
| `cmiPIDUseTa` | 1031 | Use Ta |
| `cmiPIDConstdt` | 1059 | Const dt |
| `cmiPID_AutoClearHistory` | 1061 | Auto clear history |
| `cmiPID_ClearHistoryOnRangeExceed` | 1069 | Clear on range exceed |

**Double settings** (use `GetPIDSetting` / `SetPIDSetting`, value in `dSet`):
| Constant | Value | Parameter |
|---|---|---|
| `cmiDeviationSensitivityFactor` | 1037 | Sensitivity factor |

**Bounds** (use `GetLaserControlSetting` — already implemented in `get_deviation_bounds` at line 237):
| Constant | Value | Parameter |
|---|---|---|
| `cmiDeviationBoundsMin` | 1042 | Output bounds min |
| `cmiDeviationBoundsMax` | 1043 | Output bounds max |
| `cmiDeviationRefMid` | 1044 | Reference at mid |
| `cmiDeviationRefAt` | 1045 | Reference at |

### Existing Stubbed Function (line 156-160)

```python
def get_pid_settings(self):
    intval=ctypes.c_long(0)
    doubleval=ctypes.c_double(0)
    wlmData.dll.GetPIDSetting(wlmConst.cmiPID_P,1,intval,doubleval)
    # NO RETURN STATEMENT — never used
```

Replace with a complete implementation that reads all settings for a given port.

## Config Persistence (`config.py` — new file)

Create `HF_Locking/pid_config.json` with per-channel dicts containing ALL settings:
- PID gains (P, I, D, T, dt)
- Polarity, sensitivity (factor + dim + ex), unit
- Bounds (min, max), reference points
- Setpoint (course value)
- Channel assignment
- ErrSig thresholds, Exceeding settings (if readable via DLL)
- Timestamp of when config was saved

Functions needed:
- `save_config(wlm, ports)` — read all settings from WLM via DLL, write to JSON
- `load_config()` — read JSON, return dict (or None)
- `restore_config(wlm, ports)` — load JSON, compare with current DLL state, write back

## Integration — Read-Before-Write Principle

**Critical safety rule:** Only restore settings that we can BOTH read and verify via the DLL. Never blind-write from JSON — always read the current WLM live state first, compare with saved config, and show differences. This prevents overriding settings intentionally changed in the WS7 native app while the Python GUI was not running.

### Startup flow
1. Read all PID settings from WLM via DLL (current live state)
2. Read saved config from JSON (last known state from previous session)
3. Compare them — identify which values differ
4. Log all differences
5. Show differences to user (console or dialog) — let user decide whether to restore
6. Only write back values the user explicitly approves

### Shutdown flow
- Save current WLM state (read via DLL at shutdown time, NOT from memory cache) to JSON
- This ensures the JSON always reflects the actual hardware state

### What NOT to do
- Don't auto-restore on startup without user confirmation
- Don't cache values in memory and save those — always read from DLL before saving
- Don't restore settings that the DLL can't read back (no way to verify)

## Reference: Current Channel Configuration

**Channel 3 (Vexlum):** Signal=3, Unit=0 (nm), P=0.16, I=0.84, D=0.034, Sensitivity=-3, Polarity=1, Bounds=-10000/9999.69, Course=632.99 nm (HeNe default — setpoint was set via DLL only, NOT in INI)

**Channel 4 (TiSa_1):** Signal=0, Unit=2 (THz), P=0.16, I=0.84, D=0.034, Sensitivity=-2, Polarity=-1 (inverted), Bounds=-10000/9999.69, Course=348.666410 THz

Note: PID gains above are factory defaults from the INI. User confirms gains WERE custom — they were lost because the WS7 app didn't persist DLL-set values.

## BLACS Setpoint History (from h5 archives)

| Date | TiSa_1 (ch4) | Vexlum (ch3) |
|---|---|---|
| Feb 2 | 348.666415 THz | 420.89965 THz |
| Feb 20 | 348.666408 THz | 420.899795 THz |
| Feb 24 (last good) | 348.661086 THz | 420.899794 THz |

These setpoints are still in the BLACS saved state h5 file and can be used as reference.

## Key Files

- `wlm_utils.py` — DLL wrapper, needs `get_all_pid_settings()` and `set_all_pid_settings()`
- `wlmConst.py` — DLL constants (read-only, do not modify)
- `wlmData.py` — DLL function signatures (read-only, already has `GetPIDSetting`/`SetPIDSetting`)
- `workers.py` — Main polling engine, integrate save/load here or in `main_wlm.py`
- `config.py` — New file for config persistence
