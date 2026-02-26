"""PID config persistence for HF_Locking.

Saves/loads all per-channel PID settings to/from pid_config.json.
Follows read-before-write: never blind-write from JSON — always
read current DLL state, compare, and let the user decide.
"""

import json
import os
import logging
from datetime import datetime

import wlmConst

logger = logging.getLogger("pid_config")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pid_config.json")

# ---------------------------------------------------------------------------
# Setting registries: map human-readable names -> wlmConst constants
# ---------------------------------------------------------------------------

# Double settings read via GetPIDSetting (value lives in dSet)
PID_DOUBLE_SETTINGS = {
    "P":                 wlmConst.cmiPID_P,
    "I":                 wlmConst.cmiPID_I,
    "D":                 wlmConst.cmiPID_D,
    "T":                 wlmConst.cmiPID_T,
    "dt":                wlmConst.cmiPID_dt,
    "SensitivityFactor": wlmConst.cmiDeviationSensitivityFactor,
}

# Integer settings read via GetPIDSetting (value lives in iSet)
PID_INT_SETTINGS = {
    "Polarity":                   wlmConst.cmiDeviationPolarity,
    "SensitivityDim":             wlmConst.cmiDeviationSensitivityDim,
    "SensitivityEx":              wlmConst.cmiDeviationSensitivityEx,
    "Unit":                       wlmConst.cmiDeviationUnit,
    "DeviationChannel":           wlmConst.cmiDeviationChannel,
    "UseTa":                      wlmConst.cmiPIDUseTa,
    "Constdt":                    wlmConst.cmiPIDConstdt,
    "AutoClearHistory":           wlmConst.cmiPID_AutoClearHistory,
    "ClearHistoryOnRangeExceed":  wlmConst.cmiPID_ClearHistoryOnRangeExceed,
}

# Double settings read via GetLaserControlSetting (value lives in dSet)
LC_DOUBLE_SETTINGS = {
    "BoundsMin": wlmConst.cmiDeviationBoundsMin,
    "BoundsMax": wlmConst.cmiDeviationBoundsMax,
    "RefAt":     wlmConst.cmiDeviationRefAt,
}

# Integer settings read via GetLaserControlSetting (value lives in iSet)
# Per WS7 manual p.131: cmiDeviationRefMid returns in iVal (1=centered, 0=explicit)
LC_INT_SETTINGS = {
    "RefMid":    wlmConst.cmiDeviationRefMid,
}

# Tolerance for floating-point comparison
FLOAT_TOLERANCE = 1e-9

# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def read_live_state(wlm, ports):
    """Read all PID settings from the DLL for all ports.

    Returns dict: {port_int: {setting_name: value, ...}, ...}
    """
    state = {}
    for port in ports:
        p_settings = {}

        for name, const in PID_DOUBLE_SETTINGS.items():
            try:
                _i, d = wlm.get_pid_setting(const, port)
                p_settings[name] = d
            except Exception as e:
                logger.warning(f"Failed to read {name} on port {port}: {e}")

        for name, const in PID_INT_SETTINGS.items():
            try:
                i, _d = wlm.get_pid_setting(const, port)
                p_settings[name] = i
            except Exception as e:
                logger.warning(f"Failed to read {name} on port {port}: {e}")

        for name, const in LC_DOUBLE_SETTINGS.items():
            try:
                _i, d = wlm.get_laser_control_setting(const, port)
                p_settings[name] = d
            except Exception as e:
                logger.warning(f"Failed to read {name} on port {port}: {e}")

        for name, const in LC_INT_SETTINGS.items():
            try:
                i, _d = wlm.get_laser_control_setting(const, port)
                p_settings[name] = i
            except Exception as e:
                logger.warning(f"Failed to read {name} on port {port}: {e}")

        try:
            p_settings["Setpoint"] = wlm.get_pid_course_num(port)
        except Exception:
            p_settings["Setpoint"] = None

        state[port] = p_settings

    return state


def save_config(wlm, ports):
    """Read all PID settings from DLL and save to JSON file.

    Always reads from DLL (never from memory cache).
    """
    state = read_live_state(wlm, ports)

    data = {
        "saved_at": datetime.now().isoformat(),
        "ports": {str(p): settings for p, settings in state.items()},
    }

    # Atomic write: write to temp file, then rename
    tmp_path = CONFIG_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
        logger.info(f"Config saved to {CONFIG_PATH}")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def load_config():
    """Load saved config from JSON file.

    Returns dict with 'saved_at' and 'ports' keys, or None if no file exists.
    """
    if not os.path.exists(CONFIG_PATH):
        logger.info("No saved config found.")
        return None

    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        data["ports"] = {int(k): v for k, v in data["ports"].items()}
        logger.info(f"Config loaded from {CONFIG_PATH} (saved at {data.get('saved_at', 'unknown')})")
        return data
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return None


def compare_configs(live, saved_ports):
    """Compare live DLL state with saved config.

    Args:
        live: dict from read_live_state() {port: {name: value}}
        saved_ports: dict from load_config()["ports"] {port: {name: value}}

    Returns:
        dict: {port: [(setting_name, live_value, saved_value), ...]}
        Only includes settings that differ.
    """
    diffs = {}
    for port in live:
        if port not in saved_ports:
            continue
        port_diffs = []
        for name, live_val in live[port].items():
            if name not in saved_ports[port]:
                continue
            saved_val = saved_ports[port][name]

            if live_val is None or saved_val is None:
                continue

            if isinstance(live_val, float) or isinstance(saved_val, float):
                if abs(float(live_val) - float(saved_val)) > FLOAT_TOLERANCE:
                    port_diffs.append((name, live_val, saved_val))
            else:
                if int(live_val) != int(saved_val):
                    port_diffs.append((name, live_val, saved_val))

        if port_diffs:
            diffs[port] = port_diffs

    return diffs


def restore_settings(wlm, port, settings_to_restore):
    """Write a dict of {name: value} settings back to the DLL for one port.

    Returns dict of {name: return_code}.
    """
    results = {}

    for name, value in settings_to_restore.items():
        try:
            if name in PID_DOUBLE_SETTINGS:
                rc = wlm.set_pid_setting(PID_DOUBLE_SETTINGS[name], port, dSet=float(value))
                results[name] = rc
            elif name in PID_INT_SETTINGS:
                rc = wlm.set_pid_setting(PID_INT_SETTINGS[name], port, iSet=int(value))
                results[name] = rc
            elif name in LC_DOUBLE_SETTINGS:
                rc = wlm.set_laser_control_setting(LC_DOUBLE_SETTINGS[name], port, dSet=float(value))
                results[name] = rc
            elif name in LC_INT_SETTINGS:
                rc = wlm.set_laser_control_setting(LC_INT_SETTINGS[name], port, iSet=int(value))
                results[name] = rc
            elif name == "Setpoint" and value is not None:
                wlm.set_pid_course_num(port, float(value))
                results[name] = 0
        except Exception as e:
            logger.error(f"Failed to restore {name} on port {port}: {e}")
            results[name] = -999

    return results


def format_diff_summary(diffs, channel_names):
    """Format differences into a human-readable string for display.

    Args:
        diffs: output of compare_configs()
        channel_names: dict {port: name}

    Returns:
        str: Multi-line summary
    """
    if not diffs:
        return "No differences found between saved config and current WLM state."

    lines = []
    for port, port_diffs in sorted(diffs.items()):
        ch_name = channel_names.get(port, f"Ch {port}")
        lines.append(f"\n--- {ch_name} (Port {port}) ---")
        for name, live_val, saved_val in port_diffs:
            if isinstance(live_val, float):
                lines.append(f"  {name}: current={live_val:.9g}  saved={saved_val:.9g}")
            else:
                lines.append(f"  {name}: current={live_val}  saved={saved_val}")

    return "\n".join(lines)
