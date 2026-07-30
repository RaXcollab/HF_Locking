#!/usr/bin/env python
"""WRITE-CAPABLE Matisse scan-vs-lock test over the Network Server SCPI channel.

Decides whether stepping SCAN:NOW (ref-cell piezo, SCAN:DEVICE=2) moves the
TiSa-2 frequency while the Matisse INTERNAL fast-piezo lock holds
(FASTPIEZO:LOCK stays TRUE) across small hops -- i.e. whether the no-LabVIEW
SCPI path can be the remote setpoint, with no Counterdrift / LabVIEW plug-in.

Required lab config for a clean test:
  - Matisse internal fast-piezo lock ON  (FASTPIEZO:LOCK == TRUE)
  - Counterdrift OFF and HF_Locking Ch_6 lock OFF  (both fight the ref-cell scan)
  - Matisse Commander connected to TiSa-2, Network Server ON (port 30000)
  - HF_Locking GUI running, WS7 routing Ch_6 -> live freq read off PUB 3797

Frequency is READ (never written) off the HF_Locking PUB feed:
  tcp://127.0.0.1:3797, single-part "6 <freq_THz>" @10 Hz, 0.0 == no data
  (GUIs/HF_Locking/workers.py:503). SCPI uses LabVIEW length-prefixed framing
  (see matisse_scpi_probe.py).

Phases -- each a SEPARATE, individually-gated invocation:
  A  read-only baseline         ->  python matisse_scan_lock_test.py A
  B  single nudge + revert      ->  python matisse_scan_lock_test.py B --go
  C  arbitrary-order 5 MHz hops ->  python matisse_scan_lock_test.py C --go --thz-per-unit K

GUARDRAILS (phases B/C): every write is clamped to x0 +/- MAX_DELTA and to the
live scan limits; every write is read back and verified (a decimal-separator /
locale mismatch aborts); FASTPIEZO:LOCK is checked before and after every write
and on ANY drop to FALSE the scan reverts to x0 and the test aborts; x0 is
restored in a finally block; the connection is closed gracefully.
"""
import argparse
import random
import socket
import struct
import sys
import threading
import time

import zmq

SCPI_HOST = "127.0.0.1"
SCPI_PORT = 30000
PUB_ADDR = "tcp://127.0.0.1:3797"
TISA2_PORT = 6            # HF_Locking wire port for TiSa-2 (Ch_6), main_wlm.py:65

MAX_DELTA = 0.003         # default hard clamp on |SCAN:NOW - x0|, scan units
ENV_UNITS_CAP = 0.006     # phase D backstop: never exceed ~320 MHz from x0
LIMIT_MARGIN = 0.01       # stay this far inside SCAN:LOWER/UPPERLIMIT
NUDGE_B = 0.0005          # phase B single-nudge size, scan units
READBACK_TOL = 2e-4       # SCAN:NOW readback must match target within this


# ---------- SCPI over LabVIEW length-prefixed framing ----------
def _recvall(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("SCPI connection closed mid-read")
        buf += chunk
    return buf


def scpi_send(sock, cmd):
    p = cmd.encode("ascii")
    sock.sendall(struct.pack(">L", len(p)) + p)


def scpi_recv(sock):
    n = struct.unpack(">L", _recvall(sock, 4))[0]
    return "" if n == 0 else _recvall(sock, n).decode("ascii", "replace")


def scpi_cmd(sock, cmd):
    scpi_send(sock, cmd)
    return scpi_recv(sock).strip()


def _last(resp):
    # responses are header-echoed, e.g. ":SCAN:NOW: 2.616830e-01" -> "2.616830e-01"
    parts = resp.split()
    return parts[-1] if parts else ""


def scpi_value(sock, cmd):
    return _last(scpi_cmd(sock, cmd))


def scpi_float(sock, cmd):
    return float(scpi_value(sock, cmd))


def lock_raw(sock):
    return scpi_cmd(sock, "FASTPIEZO:LOCK?")


def lock_true(sock):
    u = _last(lock_raw(sock)).upper()
    return ("TRUE" in u) or (u == "1")


# ---------- live frequency reader (PUB 3797, READ-ONLY) ----------
class FreqReader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._latest = (None, 0.0)          # (freq_THz, monotonic_t)
        self._stop = threading.Event()

    def run(self):
        ctx = zmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        sub.connect(PUB_ADDR)
        sub.setsockopt(zmq.SUBSCRIBE, f"{TISA2_PORT} ".encode())
        sub.setsockopt(zmq.RCVTIMEO, 200)
        try:
            while not self._stop.is_set():
                try:
                    msg = sub.recv_string()
                except zmq.Again:
                    continue
                try:
                    f = float(msg.split(" ", 1)[1])
                except (IndexError, ValueError):
                    continue
                if f >= 1.0:                 # "6 0.0" == no data
                    self._latest = (f, time.monotonic())
        finally:
            sub.close()

    def latest(self, max_age=1.0):
        f, t = self._latest
        if f is None or (time.monotonic() - t) > max_age:
            return None
        return f

    def stop(self):
        self._stop.set()


def wait_freq(reader, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        f = reader.latest()
        if f is not None:
            return f
        time.sleep(0.05)
    return None


# ---------- baseline + guarded write ----------
def read_baseline(sock, reader):
    dev = scpi_cmd(sock, "SCAN:DEVICE?")
    x0 = scpi_float(sock, "SCAN:NOW?")
    rc = scpi_float(sock, "REFERENCECELL:NOW?")
    lo = scpi_float(sock, "SCAN:LOWERLIMIT?")
    hi = scpi_float(sock, "SCAN:UPPERLIMIT?")
    lk = lock_raw(sock)
    f0 = wait_freq(reader)
    print("  SCAN:DEVICE       = %s   (expect 2 = ref cell)" % dev)
    print("  SCAN:NOW     (x0) = %.6f" % x0)
    print("  REFERENCECELL:NOW = %.6f   (delta from x0 = %+.6f)" % (rc, rc - x0))
    print("  SCAN limits       = [%.4f, %.4f]" % (lo, hi))
    print("  FASTPIEZO:LOCK?   = %r" % lk)
    print("  TiSa-2 freq (PUB) = %s THz" % (("%.9f" % f0) if f0 is not None else "NO DATA"))
    return dict(dev=_last(dev), x0=x0, rc=rc, lo=lo, hi=hi,
                locked=("TRUE" in _last(lk).upper()) or (_last(lk) == "1"), f0=f0)


def clamp_target(x0, target, lo, hi, max_delta=MAX_DELTA):
    lo_ok = max(lo + LIMIT_MARGIN, x0 - max_delta)
    hi_ok = min(hi - LIMIT_MARGIN, x0 + max_delta)
    return max(lo_ok, min(hi_ok, target))


def set_scan(sock, x0, target, lo, hi, max_delta=MAX_DELTA):
    """Clamp, write SCAN:NOW, and verify the readback. Raises on mismatch."""
    clamped = clamp_target(x0, target, lo, hi, max_delta)
    if abs(clamped - target) > 1e-9:
        print("    [clamp] %.6f -> %.6f" % (target, clamped))
    scpi_cmd(sock, "SCAN:NOW %.6f" % clamped)
    back = scpi_float(sock, "SCAN:NOW?")
    if abs(back - clamped) > READBACK_TOL:
        raise RuntimeError(
            "readback mismatch: wrote %.6f, read %.6f "
            "(possible decimal-separator/locale issue) -- ABORT" % (clamped, back))
    return back


def _precheck(base):
    if "2" not in base["dev"]:
        print("ABORT: SCAN:DEVICE is not 2 (scan master is not the ref cell).")
        return False
    if not base["locked"]:
        print("ABORT: FASTPIEZO:LOCK is FALSE -- laser is not internally locked.")
        return False
    if base["f0"] is None:
        print("ABORT: no live TiSa-2 frequency on PUB (check HF GUI / switcher / Ch_6).")
        return False
    return True


# ---------- phase B: single nudge + revert ----------
def phase_b(sock, reader):
    base = read_baseline(sock, reader)
    if not _precheck(base):
        return 2
    x0, lo, hi, f0 = base["x0"], base["lo"], base["hi"], base["f0"]
    target = x0 + NUDGE_B
    print("\nNUDGE: SCAN:NOW %.6f -> %.6f  (delta = %+.4f units)" % (x0, target, NUDGE_B))

    samples = []
    lock_held = True
    try:
        set_scan(sock, x0, target, lo, hi)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            if not lock_true(sock):
                lock_held = False
                print("  !! FASTPIEZO:LOCK dropped to FALSE -- reverting NOW")
                break
            sn = scpi_float(sock, "SCAN:NOW?")
            rc = scpi_float(sock, "REFERENCECELL:NOW?")
            samples.append((time.monotonic() - t0, sn, rc, reader.latest()))
            time.sleep(0.1)
    finally:
        set_scan(sock, x0, x0, lo, hi)
        print("  reverted SCAN:NOW -> %.6f" % scpi_float(sock, "SCAN:NOW?"))

    print("\n  t(s)   SCAN:NOW   REFCELL     TiSa-2 THz")
    for t, sn, rc, f in samples:
        print("  %4.1f  %9.6f  %9.6f  %s"
              % (t, sn, rc, ("%.9f" % f) if f is not None else "--"))

    fs = [f for _, _, _, f in samples if f is not None]
    if lock_held and fs and f0:
        df_thz = (sum(fs) / len(fs)) - f0
        k = df_thz / NUDGE_B
        print("\n  RESULT: lock HELD across the nudge.")
        print("  delta-f ~ %+.2f MHz for delta-scan %+.4f units" % (df_thz * 1e6, NUDGE_B))
        if k:
            print("  calibration k ~ %.4e THz/unit  =>  5 MHz step = %.6f units"
                  % (k, 5e-6 / abs(k)))
    elif not lock_held:
        print("\n  RESULT: lock BROKE on the nudge -> native SCAN path does NOT hold; "
              "Counterdrift/LabVIEW path needed.")
    return 0


# ---------- phase C: arbitrary-order ~5 MHz hops ----------
def phase_c(sock, reader, k):
    base = read_baseline(sock, reader)
    if not _precheck(base):
        return 2
    x0, lo, hi, f0 = base["x0"], base["lo"], base["hi"], base["f0"]
    d5 = 5e-6 / abs(k)                       # scan units per ~5 MHz
    plan = [1, 3, -2, 1, 4, 0]               # arbitrary-order multiples of d5
    print("\nHOPS (5 MHz step = %.6f units): offsets %s x d5" % (d5, plan))

    rows = []
    lock_held = True
    try:
        for m in plan:
            set_scan(sock, x0, x0 + m * d5, lo, hi)
            t0 = time.monotonic()
            settle = None
            while time.monotonic() - t0 < 1.0:
                if not lock_true(sock):
                    lock_held = False
                    break
                f = reader.latest()
                if f is not None and settle is None and abs((f - f0) - m * d5 * k) < 1e-6:
                    settle = time.monotonic() - t0
                time.sleep(0.05)
            if not lock_held:
                print("  !! lock dropped at offset %+d -- reverting" % m)
                break
            f = reader.latest()
            rows.append((m, f, settle))
            print("  offset %+d  f=%s THz  settle=%s s"
                  % (m, ("%.9f" % f) if f is not None else "--",
                     ("%.2f" % settle) if settle is not None else ">1.0"))
    finally:
        set_scan(sock, x0, x0, lo, hi)
        print("  reverted SCAN:NOW -> %.6f" % scpi_float(sock, "SCAN:NOW?"))

    print("\n  RESULT: hops=%d  lock-losses=%d  -> %s"
          % (len(rows), 0 if lock_held else 1, "PASS" if lock_held else "FAIL"))
    return 0


# ---------- phase D: 100-hop stress run (commissioning acceptance) ----------
def phase_d(sock, reader, k, n, env_mhz):
    base = read_baseline(sock, reader)
    if not _precheck(base):
        return 2
    x0, lo, hi, f0 = base["x0"], base["lo"], base["hi"], base["f0"]
    env_units = min((env_mhz * 1e-6) / abs(k), ENV_UNITS_CAP)
    md = env_units + 2e-4                     # clamp headroom for the write guard
    print("\nSTRESS: %d hops, uniform +/- %.0f MHz (= +/- %.6f units), seed=1"
          % (n, env_units * abs(k) * 1e6, env_units))

    random.seed(1)
    settles, excursions, unrecovered, modehops, nosettle, max_acc = [], 0, 0, 0, 0, 0.0
    completed = 0
    try:
        for i in range(n):
            o = random.uniform(-env_units, env_units)
            f_pred = f0 + o * k
            set_scan(sock, x0, x0 + o, lo, hi, md)
            samples = []
            exc = False
            settle = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < 2.0:
                lk = lock_true(sock)
                if not lk:
                    exc = True
                f = reader.latest()
                samples.append((time.monotonic() - t0, f, lk))
                fv = [s for s in samples if s[1] is not None]
                if lk and len(fv) >= 3 and abs(fv[-1][1] - fv[-2][1]) < 0.5e-6:
                    settle = fv[-1][0]
                    break
                time.sleep(0.05)

            final_lock = lock_true(sock)
            ffinal = reader.latest()
            completed = i + 1
            if exc:
                excursions += 1
            if settle is not None:
                settles.append(settle)
            else:
                nosettle += 1
            if ffinal is not None:
                err = abs(ffinal - f_pred)
                if err > 100e-6:
                    modehops += 1
                else:
                    max_acc = max(max_acc, err)
            if not final_lock:
                unrecovered += 1
                print("  !! hop %d: FASTPIEZO:LOCK UNRECOVERED -- aborting run" % i)
                break
            if completed % 10 == 0:
                print("  %3d/%d  excursions=%d modehops=%d nosettle=%d"
                      % (completed, n, excursions, modehops, nosettle))
    finally:
        set_scan(sock, x0, x0, lo, hi, md)
        print("  reverted SCAN:NOW -> %.6f" % scpi_float(sock, "SCAN:NOW?"))

    ss = sorted(settles)
    med = ss[len(ss) // 2] if ss else None
    print("\n  === STRESS SUMMARY (%d/%d hops completed) ===" % (completed, n))
    print("  lock excursions (LOCK went FALSE) : %d" % excursions)
    print("  unrecovered lock losses           : %d" % unrecovered)
    print("  mode-hops (>100 MHz from target)  : %d" % modehops)
    print("  no-settle within 2 s              : %d" % nosettle)
    if ss:
        print("  settle time  min/med/max (s)      : %.2f / %.2f / %.2f"
              % (ss[0], med, ss[-1]))
    print("  max accuracy error vs prediction  : %.2f MHz  (calibration linearity)"
          % (max_acc * 1e6))
    ok = (unrecovered == 0 and modehops == 0 and excursions <= 1)
    print("  RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0


# ---------- phase MOVE: slow rate-limited visible ramp up + back ----------
def phase_move(sock, reader, k, delta_mhz, step_mhz):
    base = read_baseline(sock, reader)
    if not _precheck(base):
        return 2
    x0, lo, hi = base["x0"], base["lo"], base["hi"]
    total_off = (delta_mhz * 1e-6) / k              # signed scan units for +delta_mhz in FREQ
    step = (step_mhz * 1e-6) / abs(k)               # sub-step magnitude, units
    md = min(ENV_UNITS_CAP, max(MAX_DELTA, abs(total_off) + 2e-4))
    n = max(1, int(round(abs(total_off) / step)))
    up = [x0 + total_off * (j + 1) / n for j in range(n)]
    down = [x0 + total_off * (n - 1 - j) / n for j in range(n)]
    print("\nMOVE: %+.0f MHz (freq) as a %d x %.0f MHz rate-limited ramp, then back"
          % (delta_mhz, n, step_mhz))

    def do_ramp(path, label):
        for sp in path:
            set_scan(sock, x0, sp, lo, hi, md)
            time.sleep(0.4)
            if not lock_true(sock):
                print("  !! FASTPIEZO:LOCK dropped -- reverting")
                return False
            f = reader.latest()
            print("  [%-4s] SCAN=%.6f  f=%s THz"
                  % (label, sp, ("%.9f" % f) if f is not None else "--"))
            time.sleep(0.2)
        return True

    try:
        if do_ramp(up, "up"):
            print("  --- holding at top for 3 s (watch your display) ---")
            time.sleep(3.0)
            do_ramp(down, "back")
    finally:
        set_scan(sock, x0, x0, lo, hi, md)
        f = reader.latest()
        print("  final SCAN:NOW -> %.6f  f=%s THz"
              % (scpi_float(sock, "SCAN:NOW?"), ("%.9f" % f) if f is not None else "--"))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=["A", "B", "C", "D", "MOVE"])
    ap.add_argument("--go", action="store_true",
                    help="required to perform WRITES (phases B, C, D)")
    ap.add_argument("--thz-per-unit", type=float, default=None,
                    help="calibration k from phase B (required for phases C, D)")
    ap.add_argument("--n", type=int, default=100, help="phase D: number of hops")
    ap.add_argument("--envelope-mhz", type=float, default=200.0,
                    help="phase D: +/- envelope around x0")
    ap.add_argument("--delta-mhz", type=float, default=100.0, help="MOVE: freq move size")
    ap.add_argument("--step-mhz", type=float, default=10.0, help="MOVE: sub-step size")
    ap.add_argument("--host", default=SCPI_HOST)
    ap.add_argument("--port", type=int, default=SCPI_PORT)
    a = ap.parse_args()

    if a.phase in ("B", "C", "D", "MOVE") and not a.go:
        print("Phase %s performs WRITES to the laser. Re-run with --go to proceed." % a.phase)
        return 1
    if a.phase in ("C", "D", "MOVE") and a.thz_per_unit is None:
        print("Phase %s needs --thz-per-unit K (from phase B)." % a.phase)
        return 1

    reader = FreqReader()
    reader.start()
    print("connecting SCPI %s:%d (length-prefixed framing) ..." % (a.host, a.port))
    try:
        s = socket.create_connection((a.host, a.port), timeout=3.0)
        s.settimeout(2.0)
    except Exception as e:
        print("SCPI CONNECT FAILED: %s" % e)
        print("-> Network Server not enabled/listening, or wrong port.")
        reader.stop()
        return 1

    try:
        if a.phase == "A":
            print("PHASE A -- read-only baseline (no writes):\n")
            read_baseline(s, reader)
            rc = 0
        elif a.phase == "B":
            rc = phase_b(s, reader)
        elif a.phase == "C":
            rc = phase_c(s, reader, a.thz_per_unit)
        elif a.phase == "D":
            rc = phase_d(s, reader, a.thz_per_unit, a.n, a.envelope_mhz)
        else:
            rc = phase_move(s, reader, a.thz_per_unit, a.delta_mhz, a.step_mhz)
    finally:
        try:
            scpi_send(s, "Close_Network_Connection")
            time.sleep(0.3)
        except Exception:
            pass
        s.close()
        reader.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
