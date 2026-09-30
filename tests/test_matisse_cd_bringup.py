"""tools/matisse_cd_bringup.py -- gated lab bring-up of Matisse CounterDrift.

Written test-first against a simulated Matisse (CounterDrift + laser) and a
simulated clock, so the SAFETY logic is pinned before it ever meets hardware:

  B1  air/vacuum/neither classification of MC wavelength vs HF frequency
  B2  settle-time metric (first of N consecutive in-tolerance samples)
  B3  phase B happy path: activate at current freq, step, step back, and
      CounterDrift is deactivated at the end
  B4  phase B with a LabVIEW decimal-separator misparse ("799.446" -> 799):
      guard trips, CounterDrift deactivated, reason names the separator
  B5  phase B refused (no writes) when MC reports AIR wavelength
  B6  CounterDrift deactivated even when the HF signal disappears mid-test
  B7  step larger than max_step_mhz refused before any write
  B8  phase C reports a settle time per step
"""
from __future__ import annotations

import math
import os
import sys

import pytest

pytest.importorskip("PyQt5")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import matisse_cd_bringup as bu  # noqa: E402  -- in-repo tool: missing = error, not skip

C = 299792.458
F0 = 375.0000123            # THz, free-running start


class SimLaser:
    """Matisse Commander client + laser + HF frequency source in one."""

    def __init__(self, f0=F0, tau_s=0.5, slew_mhz_s=500.0, misparse=False, air=False,
                 wlm_range_mhz=None):
        self.f = f0
        self.f_start = f0
        # Beyond this detuning the WS7 loses the line; HF_Locking's PUB then keeps
        # publishing the LAST GOOD value (workers._normalize_frequency) -> frozen.
        self.wlm_range_mhz = wlm_range_mhz
        self._last_pub = f0
        self._n = 0
        self.target = None
        self.active = False
        self.tau = tau_s
        self.slew = slew_mhz_s * 1e-6          # THz/s
        self.misparse = misparse
        self.air = air
        self.calls = []
        self.connected = False
        self.signal = True

    # -- client API used by the tool --
    def connect(self):
        self.connected = True

    def close(self, graceful=True):
        self.connected = False

    def cd_open(self):
        self.calls.append(("open",))

    def cd_setpoint_nm(self, s):
        self.calls.append(("setpoint", s))
        nm = float(s.split(".")[0]) if self.misparse else float(s.replace(",", "."))
        self.target = C / nm

    def cd_activate(self, on):
        self.calls.append(("activate", on))
        self.active = bool(on)

    def get_wavelength_nm(self):
        lam = C / self.f
        return lam / 1.000275 if self.air else lam

    # -- HF frequency source --
    def read_freq(self):
        if not self.signal:
            return None
        self._n += 1
        jitter = 2e-9 * (1 if self._n % 2 else -1)      # real readings jitter (~kHz)
        if self.wlm_range_mhz is not None and abs(self.f - self.f_start) * 1e6 > self.wlm_range_mhz:
            return self._last_pub                        # frozen last-good value
        self._last_pub = self.f + jitter
        return self._last_pub

    # -- physics --
    def advance(self, dt):
        if self.active and self.target is not None:
            want = (self.target - self.f) * (1 - math.exp(-dt / self.tau))
            lim = self.slew * dt
            self.f += max(-lim, min(lim, want))


class SimClock:
    def __init__(self, laser):
        self.t = 0.0
        self.laser = laser

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += dt
        self.laser.advance(dt)


def make(laser=None, **kw):
    laser = laser or SimLaser()
    clk = SimClock(laser)
    params = dict(hold_s=5.0, settle_timeout_s=30.0, sample_dt_s=0.05)
    params.update(kw)
    b = bu.BringUp(laser, laser.read_freq, clock=clk.now, sleep=clk.sleep, log=lambda *a: None,
                   **params)
    return b, laser, clk


# ------------------------------------------------------------------ B1 / B2

def test_B1_classify_convention():
    lam_vac = C / F0
    assert bu.classify_convention(F0, lam_vac)["verdict"] == "vacuum"
    assert bu.classify_convention(F0, lam_vac / 1.000275)["verdict"] == "air"
    assert bu.classify_convention(F0, 780.241)["verdict"] == "neither"


def test_B2_settle_time():
    tgt = 375.0
    tol = 1.0                                           # MHz
    s = [(0.0, tgt + 50e-6), (0.1, tgt + 0.5e-6), (0.2, tgt + 2e-6),       # 1 in, then out
         (0.3, tgt + 0.2e-6), (0.4, tgt), (0.5, tgt - 0.3e-6)]             # 3 in a row
    assert bu.settle_time(s, 0.0, tgt, tol, n_consec=3) == pytest.approx(0.3)
    assert bu.settle_time(s, 0.0, tgt, tol, n_consec=4) is None


# ------------------------------------------------------------------ B3

def test_B3_phase_b_happy_path_ends_deactivated():
    b, laser, clk = make()
    r = b.phase_b(step_mhz=20.0)
    assert r["ok"], r
    assert r["settle_s"] is not None and r["settle_s"] < 10
    sp = [c[1] for c in laser.calls if c[0] == "setpoint"]
    assert len(sp) == 3                                 # f0, f0+step, back to f0
    assert float(sp[0]) == pytest.approx(C / F0, abs=1e-7)
    assert laser.calls[-1] == ("activate", False)
    assert not laser.active


# ------------------------------------------------------------------ B4

def test_B4_decimal_misparse_trips_guard_and_deactivates():
    laser = SimLaser(misparse=True)
    b, laser, clk = make(laser, abort_mhz=1000.0, abort_grace_s=2.0)
    r = b.phase_b(step_mhz=20.0)
    assert not r["ok"]
    assert "decimal" in r["reason"].lower()
    assert laser.calls[-1] == ("activate", False)
    assert not laser.active
    assert abs(laser.f - F0) * 1e6 < 3000, "guard let the laser run away"


# ------------------------------------------------------------------ B5

def test_B5_refuses_when_mc_reports_air():
    b, laser, clk = make(SimLaser(air=True))
    r = b.phase_b(step_mhz=20.0)
    assert not r["ok"]
    assert "air" in r["reason"].lower()
    assert [c for c in laser.calls if c[0] != "open"] == []


# ------------------------------------------------------------------ B6

def test_B6_signal_loss_aborts_and_deactivates():
    laser = SimLaser()
    b, laser, clk = make(laser, no_signal_s=1.0)
    real_sleep = clk.sleep

    def sleep(dt):
        real_sleep(dt)
        if clk.t > 2.0:
            laser.signal = False
    b._sleep = sleep
    r = b.phase_b(step_mhz=20.0)
    assert not r["ok"]
    assert "signal" in r["reason"].lower()
    assert laser.calls[-1] == ("activate", False)


# ------------------------------------------------------------------ B7

def test_B7_step_clamp_before_any_write():
    b, laser, clk = make(max_step_mhz=100.0)
    r = b.phase_b(step_mhz=500.0)
    assert not r["ok"]
    assert laser.calls == []


# ------------------------------------------------------------------ B8

def test_B8_phase_c_settle_per_step():
    b, laser, clk = make(SimLaser(tau_s=0.3, slew_mhz_s=200.0))
    r = b.phase_c(steps_mhz=(20.0, 100.0))
    assert r["ok"], r
    assert set(r["settle_s"]) == {20.0, 100.0}
    # 100 MHz at 200 MHz/s slew cannot settle faster than 0.5 s
    assert r["settle_s"][100.0] >= 0.5
    assert laser.calls[-1] == ("activate", False)


# ------------------------------------------------------------------ F1 (Fable review A1)

def test_F1_frozen_pub_value_counts_as_no_signal_and_deactivates():
    """HF_Locking PUB never goes to 0 on a lost line -- it repeats the last good
    value. A misparsed setpoint that drives the laser out of the WS7's range
    must still trip a guard (was: CD driven for the full settle timeout)."""
    laser = SimLaser(misparse=True, slew_mhz_s=2000.0, wlm_range_mhz=200.0)
    b, laser, clk = make(laser, frozen_s=2.0)
    r = b.phase_b(step_mhz=20.0)
    assert not r["ok"]
    assert "frozen" in r["reason"].lower() or "signal" in r["reason"].lower()
    assert laser.calls[-1] == ("activate", False)
    assert clk.t < 10.0, f"guard took {clk.t:.1f} s"
    assert abs(laser.f - F0) * 1e6 < 8000


def test_F1_jittering_reading_is_not_frozen():
    b, laser, clk = make(frozen_s=0.5)
    assert b.phase_b(step_mhz=20.0)["ok"]


def test_F1_phase_a_reports_longest_unchanged_reading():
    """Phase A measures how long a normal HF reading can stay identical, so
    --frozen-s can be set safely above it on the real WS7 switching cycle."""
    class Slow(SimLaser):
        def read_freq(self):                       # updates every 0.5 s only
            self._n += 1
            return self.f + 1e-9 * int(clk.t / 0.5)
    laser = Slow()
    b, laser, clk = make(laser, frozen_s=3.0)
    r = b.phase_a(probe_s=3.0)
    assert r["max_unchanged_s"] == pytest.approx(0.5, abs=0.1)
    assert r["frozen_s_ok"] is True
    laser2 = Slow()
    b2, laser2, clk = make(laser2, frozen_s=0.6)   # < 2x the 0.5 s update interval
    r2 = b2.phase_a(probe_s=3.0)
    assert r2["frozen_s_ok"] is False
