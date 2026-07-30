"""Canonical-invariant tests for HF_Locking.

One test per invariant H1-H6 from the T0.4 audit. These are NOT bug
regression tests — they pin load-bearing contracts that, if silently
changed, break BLACS lock-wait semantics or ZMQ delta merging.

Standalone-runnable (mirrors GUIs/rastering/tests/):
    conda activate labscript && python GUIs/HF_Locking/tests/test_lock_invariants.py
Also collectable by pytest.
"""
from __future__ import annotations

import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from conftest import (  # noqa: E402
    _Skip,
    _skip,
    require_workers,
    make_shared_state,
    make_wavemeter_worker_self,
    make_zmq_rep_worker_self,
    make_experiment_controller_self,
)


# H1 ----------------------------------------------------------------------

def test_H1_lock_constants_pinned():
    """LOCK_TOLERANCE, LOCK_TIMEOUT_S, LOCK_CONSECUTIVE are load-bearing for
    BLACS lock-wait. Drift detector: if anyone bumps the consecutive count
    without updating docs and BLACS PROGRAM_TIMEOUT_MS, this fails first."""
    w, _ = require_workers()
    assert w.LOCK_TOLERANCE == 5e-6, (
        "LOCK_TOLERANCE drifted from 5e-6 THz (5 MHz); got " + str(w.LOCK_TOLERANCE)
    )
    assert w.LOCK_TOLERANCE_BY_PORT == {4: 1e-6}, (
        "per-channel tolerance overrides drifted; TiSa_1 (ch4) is pinned at "
        "1e-6 THz (1 MHz); got " + str(w.LOCK_TOLERANCE_BY_PORT)
    )
    assert w.lock_tolerance(4) == 1e-6 and w.lock_tolerance(1) == 5e-6, (
        "lock_tolerance() must resolve overrides for ch4 and default elsewhere"
    )
    assert w.LOCK_TIMEOUT_S == 60.0, (
        "LOCK_TIMEOUT_S drifted from 60s (BLACS uses 120s PROGRAM_TIMEOUT_MS "
        "-- changing this requires BLACS-side update); got " + str(w.LOCK_TIMEOUT_S)
    )
    assert w.LOCK_CONSECUTIVE == 5, (
        "LOCK_CONSECUTIVE drifted from 5 (older docs said 2 -- those are "
        "stale; code is authoritative); got " + str(w.LOCK_CONSECUTIVE)
    )


# H2 ----------------------------------------------------------------------

def test_H2_requires_five_consecutive_in_tol():
    """`_wait_for_lock`: 4 in-tol, then 1 out-of-tol, then 4 in-tol must NOT
    lock — counter resets to zero on the out-of-tol sample. We drive the
    real method against a real SharedExperimentState by writing measurements
    between iterations. To avoid 60s wallclock, monkeypatch time.sleep.
    """
    w, wc = require_workers()
    state = make_shared_state()
    self_ = make_zmq_rep_worker_self(state, None)
    target = 348.666410

    # Sequence: 4 in-tol, 1 out, 4 in-tol (9 samples total, never 5 in a row).
    in_tol = target + 1e-6   # 1 MHz inside 5 MHz tol
    out_tol = target + 1e-5  # 10 MHz outside
    sequence = [in_tol] * 4 + [out_tol] + [in_tol] * 4

    idx = {"i": 0}

    def fake_sleep(_):
        i = idx["i"]
        if i >= len(sequence):
            # exhaust the loop by flipping _running off
            self_._running = False
            return
        state.update_measurement(1, {
            "valid": True, "freq_raw": sequence[i],
            "freq_display": sequence[i], "freq_plot": sequence[i],
        })
        idx["i"] += 1

    # Seed iteration 0 manually (loop reads BEFORE first sleep).
    state.update_measurement(1, {
        "valid": True, "freq_raw": sequence[0],
        "freq_display": sequence[0], "freq_plot": sequence[0],
    })
    idx["i"] = 1

    with mock.patch.object(time, "sleep", side_effect=fake_sleep):
        # Bind unbound method against duck-typed self.
        result = w.ZMQRepWorker._wait_for_lock(self_, 1, target)

    assert result is False, (
        "4-in / 1-out / 4-in must NOT lock -- the out-of-tol sample must "
        "reset the consecutive counter to 0 (H2: requires 5 in a row)."
    )


# H2b ---------------------------------------------------------------------

def test_H2b_tisa1_port4_locks_at_1mhz_not_5mhz():
    """TiSa_1 (port 4) uses the tighter 1 MHz tolerance in `_wait_for_lock`:
    a sample 2 MHz off setpoint (inside the 5 MHz default, outside TiSa_1's
    1 MHz) must NOT lock on port 4 but MUST lock on a default port."""
    w, wc = require_workers()
    target = 348.666410

    def run(port, offset_thz):
        state = make_shared_state()
        self_ = make_zmq_rep_worker_self(state, None)
        freq = target + offset_thz
        meas = {"valid": True, "freq_raw": freq,
                "freq_display": freq, "freq_plot": freq}
        calls = {"n": 0}

        def fake_sleep(_):
            calls["n"] += 1
            if calls["n"] >= 12:  # > LOCK_CONSECUTIVE — enough to lock or prove it never will
                self_._running = False
                return
            state.update_measurement(port, dict(meas))

        state.update_measurement(port, dict(meas))  # seed iteration 0
        with mock.patch.object(time, "sleep", side_effect=fake_sleep):
            return w.ZMQRepWorker._wait_for_lock(self_, port, target)

    assert run(4, 2e-6) is False, (
        "2 MHz off setpoint must NOT lock on TiSa_1 (ch4 tol is 1 MHz)"
    )
    assert run(4, 0.5e-6) is True, (
        "0.5 MHz off setpoint must lock on TiSa_1 (inside 1 MHz tol)"
    )
    assert run(1, 4e-6) is True, (
        "4 MHz off setpoint must still lock on default channels (5 MHz tol)"
    )


# H3 ----------------------------------------------------------------------

def test_H3_inf_nothing_changed_with_last_good_does_not_reset():
    """`_normalize_frequency` policy: InfNothingChanged (-7) is "no new sample".

    Sub-case A: with last_good present -> returns valid=True, plot=last_good
                (does NOT poison the counter at the normalize layer).
    Sub-case B: with no last_good -> returns valid=False (cold start).

    Note: `_wait_for_lock` separately filters f_raw == InfNothingChanged
    (workers.py:608). The H3 invariant pins normalize-layer behavior; the
    lock-loop guard is tested implicitly by H2's `valid=True` precondition.
    """
    w, wc = require_workers()
    self_ = make_wavemeter_worker_self()

    # Sub-case A: last_good exists.
    self_._last_good_freq[1] = 348.666410
    raw, disp, plot, valid = w.WavemeterWorker._normalize_frequency(
        self_, 1, wc.InfNothingChanged
    )
    assert valid is True
    assert disp == 348.666410 and plot == 348.666410, (
        "InfNothingChanged with last_good must reuse last_good for display+plot"
    )
    assert raw == wc.InfNothingChanged, "raw is preserved as the sentinel"

    # Sub-case B: cold start, no last_good.
    self_._last_good_freq[2] = None
    raw2, disp2, plot2, valid2 = w.WavemeterWorker._normalize_frequency(
        self_, 2, wc.InfNothingChanged
    )
    assert valid2 is False
    assert disp2 is None and plot2 is None, (
        "InfNothingChanged with no last_good -> invalid, no display value"
    )


# H4 ----------------------------------------------------------------------

def test_H4_switcher_off_drops_stale_cache():
    """When a channel leaves the switcher cycle (use=False), the cached
    last_good_freq must be cleared so subsequent polls (which return
    HARD_INVALID for disabled ports) yield freq_display=None and ZMQ
    broadcasts a clean 0.0 sentinel instead of the pre-toggle value.
    """
    w, _ = require_workers()
    self_ = make_wavemeter_worker_self()
    self_._last_good_freq[3] = 460.123456  # populate cache from prior poll

    # set_switcher_signal returns nothing; get_switcher_signal returns (use, show)
    self_.wlm.set_switcher_signal.return_value = None
    self_.wlm.get_switcher_signal.return_value = (0, 0)

    w.WavemeterWorker.handle_switcher_write(self_, 3, False, False)

    assert self_._last_good_freq[3] is None, (
        "leaving the switcher cycle must clear last_good (workers.py:381)"
    )

    # And re-enabling must NOT auto-clear; a fresh valid reading repopulates.
    self_._last_good_freq[4] = 123.456
    self_.wlm.get_switcher_signal.return_value = (1, 1)
    w.WavemeterWorker.handle_switcher_write(self_, 4, True, True)
    assert self_._last_good_freq[4] == 123.456, (
        "re-enabling switcher must NOT touch last_good (only disable clears)"
    )


# H5 ----------------------------------------------------------------------

def test_H5_shared_state_snapshot_visible_to_zmq_loop():
    """Tab/worker share a SharedExperimentState instance. A write via
    update_measurement on one reference must be visible via get_measurement
    on another — no ZMQ round-trip, no copy semantics.

    (Mirrors the pattern documented in MEMORY's "Tab-worker shared dict for
    PUB-SUB cache" — SharedState is the same pattern with a QMutex.)
    """
    w, _ = require_workers()
    state = make_shared_state()

    # Reference A (would be the worker thread)
    state.update_measurement(1, {"freq_display": 348.6, "valid": True})
    # Reference B (would be the ZMQRepWorker thread, holding same `self.state`)
    snap = state.get_measurement(1)

    assert snap["freq_display"] == 348.6 and snap["valid"] is True

    # Snapshot must be a copy or read-only view — mutating it must NOT
    # corrupt the cache. (Defensive contract for downstream consumers.)
    try:
        snap["freq_display"] = -999.0
    except TypeError:
        pass  # read-only mapping is also acceptable
    snap2 = state.get_measurement(1)
    assert snap2["freq_display"] == 348.6, (
        "snapshot mutation leaked into shared cache: " + str(snap2)
    )


# H6 ----------------------------------------------------------------------

def test_H6_status_delta_merge_preserves_prior_keys():
    """`handle_slow_update` does `_status_cache[port].update(status_delta)`
    — delta semantics. A subsequent partial delta must NOT wipe keys
    written by an earlier delta. This is what lets BLACS lock-wait see a
    stable setpoint across multiple write-handler callbacks.
    """
    w, _ = require_workers()

    # Import lazily so the test SKIPs (not errors) if main_wlm is missing deps.
    try:
        import main_wlm  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        _skip("main_wlm not importable: " + repr(e))

    self_ = make_experiment_controller_self()

    # First delta: full status seed.
    full = {
        "use": True, "show": True, "setpoint": 348.666410,
        "bound_min": 348.0, "bound_max": 349.0, "lock_enabled": False,
    }
    main_wlm.ExperimentController.handle_slow_update(self_, 1, full)

    # Second delta: only lock_enabled flipped on. Prior 5 keys must persist.
    main_wlm.ExperimentController.handle_slow_update(
        self_, 1, {"lock_enabled": True}
    )

    cache = self_._status_cache[1]
    assert cache["setpoint"] == 348.666410, (
        "H6 violation: delta merge wiped setpoint; got " + str(cache)
    )
    assert cache["use"] is True and cache["bound_max"] == 349.0
    assert cache["lock_enabled"] is True, "the actual delta must apply"

    # And update_slow on the channel must have been called with the MERGED
    # cache, not the raw delta — this is how the UI sees full state.
    last_call_arg = self_.channels[1].update_slow.call_args[0][0]
    assert last_call_arg["setpoint"] == 348.666410


# ----- standalone runner -------------------------------------------------

if __name__ == "__main__":
    failures = skipped = 0
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    for name, fn in tests:
        try:
            fn()
            print("PASS " + name)
        except _Skip as e:
            skipped += 1
            print("SKIP " + name + ": " + str(e))
        except BaseException as e:  # noqa: BLE001
            if type(e).__name__ == "Skipped":  # pytest.skip from inside
                skipped += 1
                print("SKIP " + name + ": " + str(e))
                continue
            failures += 1
            print("FAIL " + name + ": " + type(e).__name__ + ": " + str(e))
    total = len(tests)
    print(str(total - failures - skipped) + "/" + str(total) + " passed "
          "(" + str(skipped) + " skipped, " + str(failures) + " failed)")
    sys.exit(1 if failures else 0)
