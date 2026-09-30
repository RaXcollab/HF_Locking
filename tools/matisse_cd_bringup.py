#!/usr/bin/env python
"""Gated lab bring-up of Matisse CounterDrift for HF_Locking (matisse_cd.py).

Answers the hardware questions the unit tests cannot:
  A  read-only: does Matisse Commander's wavelength agree with HF_Locking's
     frequency, and in which convention (vacuum / air / neither = wrong channel)?
  B  --go: activate CounterDrift AT the current frequency, hold, make ONE small
     step and back. Laser following the step => the setpoint string (decimal
     separator) is parsed correctly. Deactivates at the end.
  C  --go: step response for several step sizes (settle times -> choose
     matisse_cd runaway_s and BLACS LOCK_TIMEOUT_S) + hold stability. Deactivates.

    python tools/matisse_cd_bringup.py A --laser TiSa_1
    python tools/matisse_cd_bringup.py B --laser TiSa_1 --go
    python tools/matisse_cd_bringup.py C --laser TiSa_1 --go --steps 20 100 500

Lab state required for B/C (printed again before any write):
  - HF_Locking running (frequency is READ from its PUB feed, tcp://127.0.0.1:3797)
  - HF (WS7 PID) lock OFF for this laser's channel
  - In HF_Locking's Matisse panel: CD NOT active and "Connect" UNTICKED for this
    laser (this tool opens its own Matisse Commander connection)
GUARDS: every step clamped to --max-step-mhz; if the laser is more than
--abort-mhz (+ the current step) from target for --abort-grace-s, the HF
reading is FROZEN (identical) for --frozen-s, or missing for --no-signal-s,
the run aborts. CounterDrift is switched
OFF in a finally block on every exit path -- the laser is then UNLOCKED.
"""
import argparse
import csv
import math
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from matisse_cd import (  # noqa: E402
    C_NM_THZ, MatisseCommanderClient, MatisseError, format_nm, load_config, thz_to_nm,
)

N_AIR_APPROX = 1.000275   # standard air near 800 nm; T/P/humidity move it ~1e-6 -> used only to CLASSIFY


def classify_convention(f_hf_thz, lam_nm, tol_mhz=500.0, air_tol_mhz=5000.0):
    """Compare MC's wavelength with the HF frequency under both conventions."""
    if not lam_nm or lam_nm <= 0:
        return {"verdict": "neither", "mism_vac_mhz": float("inf"), "mism_air_mhz": float("inf")}
    vac = (f_hf_thz - C_NM_THZ / lam_nm) * 1e6
    air = (f_hf_thz - C_NM_THZ / (lam_nm * N_AIR_APPROX)) * 1e6
    if abs(vac) <= tol_mhz:
        verdict = "vacuum"
    elif abs(air) <= air_tol_mhz:
        verdict = "air"
    else:
        verdict = "neither"
    return {"verdict": verdict, "mism_vac_mhz": vac, "mism_air_mhz": air}


def settle_time(samples, t_start, target_thz, tol_mhz, n_consec=5):
    """Seconds from t_start to the FIRST sample of the first run of n_consec
    consecutive in-tolerance samples; None if never settled."""
    run_start, count = None, 0
    for t, f in samples:
        if abs(f - target_thz) * 1e6 < tol_mhz:
            if count == 0:
                run_start = t
            count += 1
            if count >= n_consec:
                return run_start - t_start
        else:
            count = 0
    return None


class BringUpAbort(RuntimeError):
    pass


class BringUp:
    def __init__(self, client, read_freq, clock=time.monotonic, sleep=time.sleep, log=print,
                 decimals=8, decimal_sep=".", abort_mhz=300.0, abort_grace_s=1.0,
                 no_signal_s=5.0, frozen_s=3.0, max_step_mhz=1000.0, tol_mhz=1.0, n_consec=5,
                 settle_timeout_s=60.0, hold_s=10.0, sample_dt_s=0.1, max_mismatch_mhz=500.0):
        self.client = client
        self.read_freq = read_freq
        self._clock, self._sleep, self._log = clock, sleep, log
        self.decimals, self.decimal_sep = decimals, decimal_sep
        self.abort_mhz, self.abort_grace_s, self.no_signal_s = abort_mhz, abort_grace_s, no_signal_s
        # HF_Locking's PUB repeats the LAST GOOD value when the WS7 loses the line
        # (workers._normalize_frequency), so "no signal" usually looks like a
        # frozen number, not a missing one. Real readings jitter.
        self.frozen_s = frozen_s
        self._last_f, self._last_change_t, self._frozen = None, None, False
        self.max_step_mhz, self.tol_mhz, self.n_consec = max_step_mhz, tol_mhz, n_consec
        self.settle_timeout_s, self.hold_s, self.sample_dt_s = settle_timeout_s, hold_s, sample_dt_s
        self.max_mismatch_mhz = max_mismatch_mhz
        self.samples = []          # (t, f_thz, target_thz) for the CSV log
        self._target = None
        self._step_mhz = 0.0

    # ---- primitives ----
    def _valid_freq(self):
        f = self.read_freq()
        if f is None or f <= 1.0:
            return None
        now = self._clock()
        if f != self._last_f or self._last_change_t is None:
            self._last_f, self._last_change_t, self._frozen = f, now, False
        elif now - self._last_change_t > self.frozen_s:
            self._frozen = True
            return None
        return f

    def _wait_freq(self):
        t0 = self._clock()
        while True:
            f = self._valid_freq()
            if f is not None:
                return f
            if self._clock() - t0 > self.no_signal_s:
                raise BringUpAbort(f"no HF signal for {self.no_signal_s} s")
            self._sleep(self.sample_dt_s)

    def _setpoint(self, f_thz, step_mhz=0.0):
        s = format_nm(thz_to_nm(f_thz), self.decimals, self.decimal_sep)
        self.client.cd_setpoint_nm(s)
        self._target, self._step_mhz = f_thz, abs(step_mhz)
        self._log(f"  CounterDrift setpoint -> {s} nm ({f_thz:.7f} THz)")

    def _track(self, duration_s, until_settled=False):
        """Sample the HF frequency against the current target with the guards
        on. Returns (samples, settle_s)."""
        t0 = self._clock()
        last_valid, far_since, out = t0, None, []
        while self._clock() - t0 < duration_s:
            t = self._clock()
            f = self._valid_freq()
            if self._frozen:
                raise BringUpAbort(
                    f"HF reading frozen for > {self.frozen_s} s -- WS7 lost the line (HF_Locking "
                    f"repeats the last good value): laser probably ran away. Check the "
                    f"decimal separator (now '{self.decimal_sep}') and the laser.")
            if f is None:
                if t - last_valid > self.no_signal_s:
                    raise BringUpAbort(f"no HF signal for > {self.no_signal_s} s")
            else:
                last_valid = t
                out.append((t, f))
                self.samples.append((t, f, self._target))
                err = abs(f - self._target) * 1e6
                if err > self.abort_mhz + self._step_mhz:
                    far_since = t if far_since is None else far_since
                    if t - far_since > self.abort_grace_s:
                        raise BringUpAbort(
                            f"laser {err:.0f} MHz from target for > {self.abort_grace_s} s -- "
                            f"running away. Most likely LabVIEW misparsed the setpoint "
                            f"(decimal separator; now '{self.decimal_sep}', try the other), "
                            f"or CounterDrift reads another channel / convention.")
                else:
                    far_since = None
                if until_settled:
                    st = settle_time(out, t0, self._target, self.tol_mhz, self.n_consec)
                    if st is not None:
                        return out, st
            self._sleep(self.sample_dt_s)
        return out, settle_time(out, t0, self._target, self.tol_mhz, self.n_consec)

    def _stats_mhz(self, samples):
        d = [(f - self._target) * 1e6 for _, f in samples]
        if not d:
            return None, None
        m = sum(d) / len(d)
        return m, math.sqrt(sum((x - m) ** 2 for x in d) / len(d))

    def _deactivate(self):
        try:
            self.client.cd_activate(False)
            self._log("  CounterDrift OFF -- laser is now UNLOCKED; re-enable your lock.")
        except Exception as e:   # noqa: BLE001
            self._log(f"  !! could not confirm CounterDrift OFF ({e}) -- CHECK MATISSE COMMANDER")

    # ---- phases ----
    def _probe_unchanged(self, probe_s):
        """Longest time a normal HF reading stays identical (WS7 switching
        cycle + InfNothingChanged repeats). --frozen-s must sit well above it."""
        t0 = self._clock()
        last_f, last_t, longest = None, t0, 0.0
        while self._clock() - t0 < probe_s:
            f, t = self.read_freq(), self._clock()
            if f is not None and f != last_f:
                longest = max(longest, t - last_t) if last_f is not None else longest
                last_f, last_t = f, t
            self._sleep(self.sample_dt_s)
        return max(longest, self._clock() - last_t)

    def phase_a(self, probe_s=0.0):
        lam = self.client.get_wavelength_nm()
        f = self._wait_freq()
        r = classify_convention(f, lam, tol_mhz=self.max_mismatch_mhz)
        r.update(lam_nm=lam, f_hf=f, ok=(r["verdict"] == "vacuum"))
        self._log(f"A: MC {lam:.6f} nm | HF {f:.7f} THz = {thz_to_nm(f):.6f} nm (vac) | "
                  f"vac mismatch {r['mism_vac_mhz']:+.0f} MHz, air {r['mism_air_mhz']:+.0f} MHz "
                  f"-> {r['verdict'].upper()}")
        if probe_s > 0:
            mu = self._probe_unchanged(probe_s)
            r.update(max_unchanged_s=mu, frozen_s_ok=mu < self.frozen_s / 2)
            self._log(f"A: longest identical HF reading {mu:.2f} s over {probe_s:.0f} s; "
                      f"--frozen-s {self.frozen_s} "
                      f"{'OK' if r['frozen_s_ok'] else 'TOO SMALL -- raise it to > 2x this'}")
        return r

    def _precheck(self, steps_mhz):
        big = [s for s in steps_mhz if abs(s) > self.max_step_mhz]
        if big:
            return None, f"step(s) {big} MHz exceed --max-step-mhz {self.max_step_mhz}"
        a = self.phase_a()
        if a["verdict"] == "air":
            return None, ("Matisse Commander reports AIR wavelengths; HF_Locking sends VACUUM "
                          "nm -- a ~100 GHz offset. Resolve the convention first.")
        if a["verdict"] != "vacuum":
            return None, ("Matisse Commander wavelength does not match this HF channel -- wrong "
                          "wavemeter channel in the MC plugin, or wrong --wlm-port.")
        return a, ""

    def _activate_at(self, f0):
        self.client.cd_open()
        self._setpoint(f0)
        self.client.cd_activate(True)
        self._log("  CounterDrift ACTIVE")

    def phase_b(self, step_mhz=20.0):
        a, why = self._precheck([step_mhz])
        if a is None:
            return {"ok": False, "reason": why}
        f0 = a["f_hf"]
        try:
            self._activate_at(f0)
            hold, _ = self._track(self.hold_s)
            m, sd = self._stats_mhz(hold)
            self._log(f"B: hold at start: mean {m:+.2f} MHz, std {sd:.2f} MHz")
            self._setpoint(f0 + step_mhz * 1e-6, step_mhz)
            _, st = self._track(self.settle_timeout_s, until_settled=True)
            if st is None:
                return {"ok": False, "reason": f"did not settle within {self.tol_mhz} MHz of a "
                        f"{step_mhz} MHz step in {self.settle_timeout_s} s",
                        "hold_mean_mhz": m, "hold_std_mhz": sd}
            self._log(f"B: {step_mhz} MHz step settled in {st:.1f} s -> setpoint string parsed OK")
            self._setpoint(f0, step_mhz)
            self._track(self.settle_timeout_s, until_settled=True)
            return {"ok": True, "reason": "", "settle_s": st,
                    "hold_mean_mhz": m, "hold_std_mhz": sd}
        except BringUpAbort as e:
            return {"ok": False, "reason": str(e)}
        except (MatisseError, OSError) as e:
            return {"ok": False, "reason": f"Matisse Commander error: {e}"}
        finally:
            self._deactivate()

    def phase_c(self, steps_mhz=(20.0, 100.0, 500.0)):
        a, why = self._precheck(list(steps_mhz))
        if a is None:
            return {"ok": False, "reason": why}
        f0 = a["f_hf"]
        settle, back = {}, {}
        try:
            self._activate_at(f0)
            self._track(self.hold_s)
            for s in steps_mhz:
                self._setpoint(f0 + s * 1e-6, s)
                _, settle[s] = self._track(self.settle_timeout_s, until_settled=True)
                self._setpoint(f0, s)
                _, back[s] = self._track(self.settle_timeout_s, until_settled=True)
                self._log(f"C: step {s:+g} MHz: settle {settle[s]} s, back {back[s]} s")
            hold, _ = self._track(self.hold_s)
            m, sd = self._stats_mhz(hold)
            self._log(f"C: final hold: mean {m:+.2f} MHz, std {sd:.2f} MHz")
            ok = all(v is not None for v in list(settle.values()) + list(back.values()))
            return {"ok": ok, "reason": "" if ok else "a step did not settle",
                    "settle_s": settle, "back_s": back, "hold_mean_mhz": m, "hold_std_mhz": sd}
        except BringUpAbort as e:
            return {"ok": False, "reason": str(e), "settle_s": settle}
        except (MatisseError, OSError) as e:
            return {"ok": False, "reason": f"Matisse Commander error: {e}", "settle_s": settle}
        finally:
            self._deactivate()


# ---------------------------------------------------------------------------
# HF_Locking PUB feed reader: single-part "<port> <freq_THz>", 0.0 = no data
# ---------------------------------------------------------------------------

def parse_pub_line(line, port):
    parts = line.split()
    if len(parts) != 2 or parts[0] != str(port):
        return None
    try:
        f = float(parts[1])
    except ValueError:
        return None
    return f if f > 1.0 else None


class PubFreqReader:
    def __init__(self, addr, port, stale_s=1.0):
        import zmq  # noqa: PLC0415
        self._zmq = zmq
        self.addr, self.port, self.stale_s = addr, port, stale_s
        self._latest = (None, 0.0)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        ctx = self._zmq.Context.instance()
        sub = ctx.socket(self._zmq.SUB)
        sub.setsockopt(self._zmq.LINGER, 0)
        sub.setsockopt_string(self._zmq.SUBSCRIBE, f"{self.port} ")
        sub.connect(self.addr)
        while not self._stop.is_set():
            if sub.poll(200):
                f = parse_pub_line(sub.recv_string(), self.port)
                if f is not None:
                    self._latest = (f, time.monotonic())
        sub.close()

    def read(self):
        f, t = self._latest
        return f if (f is not None and time.monotonic() - t < self.stale_s) else None

    def close(self):
        self._stop.set()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["A", "B", "C"])
    ap.add_argument("--laser", default="TiSa_1", help="name in matisse_cd_config.json")
    ap.add_argument("--host"), ap.add_argument("--port", type=int)
    ap.add_argument("--wlm-port", type=int, help="HF_Locking channel (default: from config)")
    ap.add_argument("--decimal-sep", help="default: from config")
    ap.add_argument("--pub", default="tcp://127.0.0.1:3797")
    ap.add_argument("--go", action="store_true", help="required for B/C (writes to the laser)")
    ap.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    ap.add_argument("--step-mhz", type=float, default=20.0)
    ap.add_argument("--steps", type=float, nargs="+", default=[20.0, 100.0, 500.0])
    ap.add_argument("--max-step-mhz", type=float, default=1000.0)
    ap.add_argument("--abort-mhz", type=float, default=300.0,
                    help="abort if |f - target| > this + current step ...")
    ap.add_argument("--abort-grace-s", type=float, default=1.0, help="... for this long")
    ap.add_argument("--no-signal-s", type=float, default=5.0)
    ap.add_argument("--frozen-s", type=float, default=3.0,
                    help="identical HF readings for this long = signal lost (check in phase A "
                         "that normal readings change faster than this)")
    ap.add_argument("--tol-mhz", type=float, default=1.0)
    ap.add_argument("--hold-s", type=float, default=30.0)
    ap.add_argument("--settle-timeout-s", type=float, default=60.0)
    ap.add_argument("--csv", default=None, help="sample log (default: timestamped file in cwd)")
    a = ap.parse_args(argv)

    cfg = load_config()
    lc = cfg["lasers"].get(a.laser, {})
    host, port = a.host or lc.get("host", "127.0.0.1"), a.port or lc.get("port", 30000)
    wlm_port = a.wlm_port or lc.get("wlm_port", 0)
    sep = a.decimal_sep or cfg.get("decimal_sep", ".")
    if not wlm_port:
        sys.exit("no HF channel: set it in the HF_Locking panel or pass --wlm-port")
    if a.phase in "BC":
        if not a.go:
            sys.exit(f"phase {a.phase} writes to the laser: add --go")
        print(__doc__.split("Lab state required")[1].split("GUARDS")[0])
        if not a.yes and input("Type GO to continue: ").strip() != "GO":
            sys.exit("aborted")

    client = MatisseCommanderClient(host, port, open_timeout_s=cfg.get("open_timeout_s", 120.0))
    reader = PubFreqReader(a.pub, wlm_port)
    b = BringUp(client, reader.read, decimal_sep=sep, abort_mhz=a.abort_mhz,
                abort_grace_s=a.abort_grace_s, no_signal_s=a.no_signal_s, frozen_s=a.frozen_s,
                max_step_mhz=a.max_step_mhz, tol_mhz=a.tol_mhz,
                hold_s=a.hold_s, settle_timeout_s=a.settle_timeout_s)
    print(f"{a.laser}: Matisse Commander {host}:{port}, HF ch{wlm_port} via {a.pub}, "
          f"decimal_sep '{sep}'")
    try:
        client.connect()
        if a.phase == "A":
            r = b.phase_a(probe_s=5.0)
        elif a.phase == "B":
            r = b.phase_b(a.step_mhz)
        else:
            r = b.phase_c(a.steps)
    finally:
        reader.close()
        client.close()
        if b.samples:
            path = a.csv or f"matisse_cd_bringup_{a.phase}_{datetime.now():%Y%m%d_%H%M%S}.csv"
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["t_s", "f_THz", "target_THz"])
                w.writerows(b.samples)
            print(f"samples -> {path}")
    print("RESULT:", {k: v for k, v in r.items() if k != "samples"})
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
