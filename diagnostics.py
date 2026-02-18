# diagnostics.py
"""
Drop-in timing diagnostics for the WLM controller.
Writes a rolling CSV log + prints warnings when budgets are exceeded.

Usage:
    import diagnostics
    diagnostics.enable()          # call before starting threads
    diagnostics.disable()         # stop logging

The CSV is written to wlm_diagnostics.csv in the working directory.
Each row captures one event with a high-resolution timestamp.
"""

import time
import threading
import collections
import os

# ──────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────
ENABLED = False
LOG_FILE = "wlm_diagnostics.csv"
WARN_POLL_FAST_MS = 40.0      # warn if poll_fast body exceeds this
WARN_GUI_UPDATE_MS = 20.0     # warn if GUI handle_fast_update exceeds this
WARN_QUEUE_LATENCY_MS = 50.0  # warn if signal sits in queue longer than this
PRINT_EVERY_N = 50            # print a summary line every N fast polls

# ──────────────────────────────────────────────────────────
# Internal state
# ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_file = None
_counter = 0

# Rolling stats (last N samples)
_N = 200
_stats = {
    "poll_fast_ms":       collections.deque(maxlen=_N),
    "poll_fast_dll_ms":   collections.deque(maxlen=_N),  # just the DLL portion
    "gui_update_ms":      collections.deque(maxlen=_N),
    "queue_latency_ms":   collections.deque(maxlen=_N),
    "poll_slow_ms":       collections.deque(maxlen=_N),
}


def enable(log_file=None):
    global ENABLED, _file, LOG_FILE
    if log_file:
        LOG_FILE = log_file
    ENABLED = True
    _file = open(LOG_FILE, "w")
    _file.write("timestamp,event,thread,value_ms,detail\n")
    _file.flush()
    print(f"[DIAG] Diagnostics enabled -> {os.path.abspath(LOG_FILE)}")


def disable():
    global ENABLED, _file
    ENABLED = False
    if _file:
        _file.close()
        _file = None
    print("[DIAG] Diagnostics disabled.")


def _log(event, value_ms, detail=""):
    global _counter
    if not ENABLED or _file is None:
        return
    t = time.perf_counter()
    thr = threading.current_thread().name
    with _lock:
        _file.write(f"{t:.6f},{event},{thr},{value_ms:.3f},{detail}\n")
        _file.flush()


def _summary():
    """Print a one-line summary of rolling averages."""
    def _avg(key):
        d = _stats[key]
        return sum(d) / len(d) if d else 0.0
    def _mx(key):
        d = _stats[key]
        return max(d) if d else 0.0

    print(
        f"[DIAG] poll_fast: avg={_avg('poll_fast_ms'):.1f}ms  max={_mx('poll_fast_ms'):.1f}ms  "
        f"(dll: avg={_avg('poll_fast_dll_ms'):.1f}ms  max={_mx('poll_fast_dll_ms'):.1f}ms)  |  "
        f"gui: avg={_avg('gui_update_ms'):.1f}ms  max={_mx('gui_update_ms'):.1f}ms  |  "
        f"queue_lat: avg={_avg('queue_latency_ms'):.1f}ms  max={_mx('queue_latency_ms'):.1f}ms  |  "
        f"poll_slow: avg={_avg('poll_slow_ms'):.1f}ms  max={_mx('poll_slow_ms'):.1f}ms"
    )


# ──────────────────────────────────────────────────────────
# Instrumentation hooks (called from patched workers/controller)
# ──────────────────────────────────────────────────────────

def on_poll_fast_start():
    """Call at the top of _poll_fast. Returns a context dict."""
    if not ENABLED:
        return None
    return {"t_start": time.perf_counter(), "dll_total": 0.0}


def on_dll_call_start():
    if not ENABLED:
        return None
    return time.perf_counter()


def on_dll_call_end(t0):
    if not ENABLED or t0 is None:
        return 0.0
    return (time.perf_counter() - t0)


def on_poll_fast_end(ctx):
    """Call at the end of _poll_fast. Logs total time and DLL subtotal."""
    global _counter
    if not ENABLED or ctx is None:
        return None

    elapsed_ms = (time.perf_counter() - ctx["t_start"]) * 1000
    dll_ms = ctx["dll_total"] * 1000

    _stats["poll_fast_ms"].append(elapsed_ms)
    _stats["poll_fast_dll_ms"].append(dll_ms)
    _log("poll_fast", elapsed_ms, f"dll={dll_ms:.1f}ms")

    if elapsed_ms > WARN_POLL_FAST_MS:
        print(f"[DIAG] WARNING: poll_fast took {elapsed_ms:.1f}ms (budget: {WARN_POLL_FAST_MS}ms)")

    _counter += 1
    if _counter % PRINT_EVERY_N == 0:
        _summary()

    # Return the emit timestamp so the GUI side can measure queue latency
    return time.perf_counter()


def on_poll_slow_done(elapsed_s):
    if not ENABLED:
        return
    ms = elapsed_s * 1000
    _stats["poll_slow_ms"].append(ms)
    _log("poll_slow", ms)


def on_signal_emitted():
    """Call right before measurement_updated.emit(). Returns timestamp."""
    if not ENABLED:
        return None
    return time.perf_counter()


def on_gui_update_start(emit_time):
    """Call at the top of handle_fast_update. Returns context dict."""
    if not ENABLED:
        return None
    now = time.perf_counter()
    queue_lat_ms = (now - emit_time) * 1000 if emit_time else 0.0
    _stats["queue_latency_ms"].append(queue_lat_ms)

    if queue_lat_ms > WARN_QUEUE_LATENCY_MS:
        print(f"[DIAG] WARNING: signal queue latency = {queue_lat_ms:.1f}ms")

    _log("queue_latency", queue_lat_ms)
    return {"t_start": now}


def on_gui_update_end(ctx):
    """Call at the end of handle_fast_update."""
    if not ENABLED or ctx is None:
        return
    elapsed_ms = (time.perf_counter() - ctx["t_start"]) * 1000
    _stats["gui_update_ms"].append(elapsed_ms)
    _log("gui_update", elapsed_ms)

    if elapsed_ms > WARN_GUI_UPDATE_MS:
        print(f"[DIAG] WARNING: gui handle_fast_update took {elapsed_ms:.1f}ms")
