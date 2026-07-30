"""Shared fixtures for HF_Locking canonical-invariant tests.

Style mirrors `GUIs/rastering/tests/test_command_queue.py` (duck-typed `self`,
real-method invocation against the production class) with two upgrades:

  1. `unittest.mock.create_autospec(Class, instance=True)` instead of bare
     SimpleNamespace — a misspelled attribute raises AttributeError instead
     of being silently absorbed. Catches typos in invariants too.
  2. Factories live here, not duplicated per file.

These tests run with NO hardware, NO Qt event loop, NO ZMQ socket bind. The
PyQt5 import is the only requirement; if it fails (CI box without PyQt) the
whole suite SKIPs cleanly via `_skip()`.
"""
from __future__ import annotations

import os
import sys
import types
from unittest import mock

# HF_Locking modules live one level up from tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The v2 protocol tests importorskip("zmq_v2"), so a checkout without the
# parent's userlib/external_gui_lib/zmq_v2.py exits 0 having tested nothing.
# ZMQ_V2_REQUIRED=1 (CI, pre-merge gates) makes the absence a collection error.
if os.environ.get("ZMQ_V2_REQUIRED"):
    import zmq_v2  # noqa: F401,E402  -- fail loudly, don't skip


# ----- environment-gated skip (pytest OR standalone runner) --------------

class _Skip(Exception):
    """Standalone-runner skip signal (pytest uses pytest.skip)."""


def _skip(msg):
    try:
        import pytest  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        raise _Skip(msg)
    pytest.skip(msg)


# ----- module import guard (PyQt5 + wlmConst required) -------------------

try:
    import workers  # noqa: E402  -- HF_Locking/workers.py
    import wlmConst  # noqa: E402
    _IMPORT_ERR = None
except Exception as e:  # pragma: no cover
    workers = None  # type: ignore
    wlmConst = None  # type: ignore
    _IMPORT_ERR = e


def require_workers():
    """Call at the top of every test; SKIPs cleanly if PyQt5/wlmConst absent."""
    if _IMPORT_ERR is not None:
        _skip("workers.py not importable (needs PyQt5): " + repr(_IMPORT_ERR))
    return workers, wlmConst


# ----- factories ---------------------------------------------------------

def make_shared_state():
    """Real SharedExperimentState — pure-Python, mutex-only, no DLL.

    Construction is hardware-free (just a QMutex + dict init).
    Using the real object means update_status/get_measurement semantics
    (incl. delta merge) are exercised exactly as in production.
    """
    w, _ = require_workers()
    return w.SharedExperimentState()


def make_wlm_stub():
    """spec-guarded WLM stub: misspelled attr → AttributeError, not silent.

    `spec_set=True` blocks both reads AND writes of unknown attrs.
    """
    import wlm_utils  # noqa: PLC0415
    stub = mock.create_autospec(wlm_utils.wlm_link, instance=True, spec_set=True)
    return stub


def make_wavemeter_worker_self():
    """Duck-typed `self` carrying ONLY what `_normalize_frequency` and
    `handle_switcher_write` touch — _last_good_freq dict, HARD_INVALID set,
    state (SharedExperimentState), wlm (stub), signals (Mock)."""
    w, wc = require_workers()
    return types.SimpleNamespace(
        _last_good_freq={p: None for p in w.PORTS},
        _lock_enabled={p: False for p in w.PORTS},
        HARD_INVALID=w.WavemeterWorker.HARD_INVALID,
        state=make_shared_state(),
        wlm=make_wlm_stub(),
        log_message=mock.Mock(name="log_message"),
        status_updated=mock.Mock(name="status_updated"),
    )


def make_zmq_rep_worker_self(state, meas_seq):
    """Duck-typed `self` for `_wait_for_lock`. `meas_seq` is an iterable of
    (valid, f_raw, f_display) tuples; the test drives the loop by writing
    them into the real SharedExperimentState before each iteration.

    `_running=True`, `isInterruptionRequested` returns False (no abort).
    """
    w, _ = require_workers()
    return types.SimpleNamespace(
        state=state,
        _running=True,
        isInterruptionRequested=lambda: False,
        log_message=mock.Mock(name="log_message"),
    )


def make_experiment_controller_self():
    """Duck-typed `self` for `handle_slow_update` — ONLY needs _status_cache
    and a per-channel mock with update_slow(). Avoids QMainWindow init."""
    w, _ = require_workers()
    return types.SimpleNamespace(
        _status_cache={p: {} for p in w.PORTS},
        channels={p: mock.Mock(name="ch" + str(p)) for p in w.PORTS},
        global_ctrl=mock.Mock(name="global_ctrl"),
    )
