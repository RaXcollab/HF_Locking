# workers.py
from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot, QTimer, QMutex, QMutexLocker
from PyQt5 import QtCore
import time
import json
import zmq
import wlmConst
import config

PORTS = list(range(1, 9))

# Worker poll rates (write to SharedState only, GUI pulls separately)
INTERVAL_POLL_FAST_MS = 20     # Measurements: matches WLM switcher cycle (~20-50ms)
INTERVAL_POLL_SLOW_MS = 1000   # Status/globals: setpoints, bounds, T, P (slow-changing)

# ZMQ
ZMQ_REQ_PORT = 3796
ZMQ_PUB_PORT = 3797
PUB_PERIOD_S = 0.1            # 10 Hz
LOCK_TOLERANCE = 0.000005     # THz
LOCK_TIMEOUT_S = 60.0
MIN_VALID_SETPOINT_THZ = 1.0  # reject bogus setpoints (e.g. 0 from remote init)

# If you haven't removed the print() inside wlm_utils.get_pid_course_num(),
# enabling this will avoid console spam without touching wlm_utils.py.
SUPPRESS_SETPOINT_READ_STDOUT = False


class SharedExperimentState:
    """
    Thread-safe cache for ZMQ + UI.
    - Measurements: updated in fast poll and sometimes by write handlers (volt readback)
    - Status: updated in slow poll and by write handlers (setpoint/switcher/lock deltas)
    - Globals: updated in slow poll and by global toggle handlers
    """
    def __init__(self):
        self._mutex = QMutex()

        # Pre-populate so PUB always has 8 entries.
        self._measurements = {
            p: {
                "freq_raw": None,       # raw DLL return (float or sentinel)
                "freq_display": None,   # last-good freq for display/stream continuity
                "freq_plot": None,      # None when invalid -> GUI can plot NaN/gap
                "valid": False,         # True only when measurement is usable for locking
                "volt": 0.0,
                "exp": (0.0, 0.0),
                "amp": (0.0, 0.0),
            }
            for p in PORTS
        }

        self._status = {
            p: {
                "use": False,
                "show": False,
                "setpoint": 0.0,
                "bound_min": 0.0,
                "bound_max": 0.0,
                "lock_enabled": False,  # software ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œarmedÃƒÂ¢Ã¢â€šÂ¬Ã‚Â state (not lock_status)
            }
            for p in PORTS
        }

        self._globals = {
            "temperature": 0.0,
            "pressure": 0.0,
            "autocal": False,
            "deviation_mode": False,
            "wlm_active": True,
        }

    # ---- writers ----
    def update_measurement(self, port: int, delta: dict) -> None:
        with QMutexLocker(self._mutex):
            self._measurements[port].update(delta)

    def update_status(self, port: int, delta: dict) -> None:
        with QMutexLocker(self._mutex):
            self._status[port].update(delta)

    def update_globals(self, delta: dict) -> None:
        with QMutexLocker(self._mutex):
            self._globals.update(delta)

    # ---- readers (copies) ----
    def get_measurement(self, port: int) -> dict:
        with QMutexLocker(self._mutex):
            return self._measurements[port].copy()

    def get_all_measurements(self) -> dict:
        with QMutexLocker(self._mutex):
            return {p: d.copy() for p, d in self._measurements.items()}

    def get_status(self, port: int) -> dict:
        with QMutexLocker(self._mutex):
            return self._status[port].copy()

    def get_all_status(self) -> dict:
        with QMutexLocker(self._mutex):
            return {p: d.copy() for p, d in self._status.items()}

    def get_globals(self) -> dict:
        with QMutexLocker(self._mutex):
            return self._globals.copy()

    def get_gui_snapshot(self) -> dict:
        """
        Atomic read of everything the GUI needs, in one mutex acquisition.
        Returns independent copies Ã¢â‚¬â€ safe to use from the GUI thread
        while the worker thread continues writing.
        """
        with QMutexLocker(self._mutex):
            return {
                "measurements": {p: d.copy() for p, d in self._measurements.items()},
                "status": {p: d.copy() for p, d in self._status.items()},
                "globals": self._globals.copy(),
            }


class WavemeterWorker(QObject):
    """
    Owns ALL wavemeter I/O (QObject moved to a QThread).
    """
    # Measurements: no longer pushed to GUI. GUI pulls from SharedExperimentState.
    # (This eliminates signal queue backlog that caused the freeze.)

    # Status deltas: (port, delta_dict). Only emitted by write handlers for immediate feedback.
    status_updated = pyqtSignal(int, dict)

    # Globals: only emitted by write handlers for immediate feedback.
    globals_updated = pyqtSignal(dict)

    # Logging: only meaningful events
    log_message = pyqtSignal(str)

    # Config persistence: emitted after manual save completes (success, message)
    config_saved = pyqtSignal(bool, str)

    finished = pyqtSignal()

    # ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œHard invalidÃƒÂ¢Ã¢â€šÂ¬Ã‚Â codes (errors)
    HARD_INVALID = {
        wlmConst.ErrNoValue,
        wlmConst.ErrNoSignal,
        wlmConst.ErrBadSignal,
        wlmConst.ErrLowSignal,
        wlmConst.ErrBigSignal,
        wlmConst.ErrWlmMissing,
        wlmConst.ErrNotAvailable,
        wlmConst.ErrNoPulse,
        wlmConst.ErrChannelNotAvailable,
        wlmConst.ErrDiv0, 
        wlmConst.ErrOutOfRange, 
        wlmConst.ErrUnitNotAvailable,
    }

    def __init__(self, wlm_link, shared_state: SharedExperimentState):
        super().__init__()
        self.wlm = wlm_link
        self.state = shared_state

        # Internal state (only accessed from worker thread Ã¢â‚¬â€ no mutex needed)
        self._running = False
        self._busy_fast = False   # re-entrancy guard for _poll_fast
        self._last_good_freq = {p: None for p in PORTS}
        self._lock_enabled = {p: False for p in PORTS}   # ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œarmedÃƒÂ¢Ã¢â€šÂ¬Ã‚Â state only

        self._timer_fast = None
        self._timer_slow = None
        self._wlm_active = True  # assume active; _poll_slow will verify

    @pyqtSlot()
    def start_polling(self):
        self._running = True

        self._timer_fast = QTimer(self)
        self._timer_fast.setTimerType(QtCore.Qt.PreciseTimer)
        self._timer_fast.timeout.connect(self._poll_fast)
        self._timer_fast.start(INTERVAL_POLL_FAST_MS)

        self._timer_slow = QTimer(self)
        self._timer_slow.setTimerType(QtCore.Qt.CoarseTimer)
        self._timer_slow.timeout.connect(self._poll_slow)
        self._timer_slow.start(INTERVAL_POLL_SLOW_MS)

        QTimer.singleShot(0, self._poll_slow)
        self.log_message.emit("WLM worker started.")

    @pyqtSlot()
    def stop(self):
        self._running = False
        if self._timer_fast:
            self._timer_fast.stop()
        if self._timer_slow:
            self._timer_slow.stop()
        self.log_message.emit("WLM worker stopped.")
        self.finished.emit()

    # --------------------------
    # Internal helpers
    # --------------------------

    def _normalize_frequency(self, port: int, f_raw):
        """
        Returns (freq_raw, freq_display, freq_plot, valid)

        Policy:
        - HARD_INVALID -> valid=False, plot gap (None), display last_good (if any)
        - InfNothingChanged -> treat as "no new sample":
              if last_good exists: valid=True, raw=last_good, plot=last_good, display=last_good
              else: valid=False
        - Otherwise -> valid=True, update last_good
        """
        last = self._last_good_freq.get(port)

        # InfNothingChanged: reuse last_good if available (avoids -7 spikes, no timestamps needed)
        if f_raw == wlmConst.InfNothingChanged:
            if last is None:
                return f_raw, None, None, False
            return f_raw, last, last, True

        # Hard invalids
        if f_raw in self.HARD_INVALID:
            return f_raw, last, None, False

        # Valid reading
        try:
            f = float(f_raw)
            self._last_good_freq[port] = f
            return f, f, f, True
        except Exception:
            return f_raw, last, None, False

    def _emit_full_status_for_port(self, port: int):
        """
        Read status from WLM and write to SharedState.
        No signal emitted Ã¢â‚¬â€ GUI pulls on its own timer.
        """
        use_val, show_val = self.wlm.get_switcher_signal(port)
        sp = self.wlm.get_pid_course_num(port)
        bmin, bmax = self.wlm.get_deviation_bounds(port)

        # Read actual lock (deviation channel assignment) from WLM hardware
        lock_hw = self.wlm.get_channel_assignment(port)
        self._lock_enabled[port] = lock_hw

        s_full = {
            "use": bool(use_val),
            "show": bool(show_val),
            "setpoint": float(sp),
            "bound_min": float(bmin),
            "bound_max": float(bmax),
            "lock_enabled": bool(lock_hw),
        }

        self.state.update_status(port, s_full)

    # --------------------------
    # Poll loops
    # --------------------------
    def _poll_fast(self):
        if not self._running:
            return
        if not self._wlm_active:
            return

        # Re-entrancy guard: if the previous poll is still running
        # (e.g. DLL stall on startup), skip this tick rather than
        # piling up back-to-back polls.
        if self._busy_fast:
            return
        self._busy_fast = True
        try:
            for port in PORTS:
                f_raw = self.wlm.get_frequency_num(port)
                freq_raw, freq_disp, freq_plot, valid = self._normalize_frequency(port, f_raw)

                volt = float(self.wlm.get_deviation_signal(port))
                exp = self.wlm.get_exposure_num(port)
                amp = self.wlm.get_amplitude(port)

                pkt = {
                    "freq_raw": freq_raw,
                    "freq_display": freq_disp,
                    "freq_plot": freq_plot,
                    "valid": bool(valid),
                    "volt": volt,
                    "exp": tuple(exp),
                    "amp": tuple(amp),
                }

                self.state.update_measurement(port, pkt)
        except Exception as e:
            self.log_message.emit(f"poll_fast error: {e}")
        finally:
            self._busy_fast = False

    def _poll_slow(self):
        if not self._running:
            return

        # Check WLM availability; back off gracefully when offline
        was_active = self._wlm_active
        try:
            self._wlm_active = self.wlm.is_active()
        except Exception:
            self._wlm_active = False

        if not self._wlm_active:
            if was_active:
                self.log_message.emit("WLM server not detected \u2013 waiting for it to start\u2026")
            self.state.update_globals({"wlm_active": False})
            return

        if not was_active:
            self.log_message.emit("WLM server detected \u2013 resuming polling.")
            self.state.update_globals({"wlm_active": True})

        # Globals
        try:
            g = {
                "temperature": float(self.wlm.get_temperature()),
                "pressure": float(self.wlm.get_pressure()),
                "autocal": (self.wlm.get_autocal_mode() == 1),
                "deviation_mode": (self.wlm.get_deviation_mode() == 1),
                "wlm_active": True,
            }
            self.state.update_globals(g)
            # No signal Ã¢â‚¬â€ GUI pulls on its own timer
        except Exception as e:
            self.log_message.emit(f"poll_slow globals error: {e}")

        # Status for all 8 ports
        for port in PORTS:
            try:
                self._emit_full_status_for_port(port)
            except Exception as e:
                self.log_message.emit(f"poll_slow status ch{port} error: {e}")

    # --------------------------
    # Write handlers (write + targeted readback + delta emit)
    # --------------------------
    @pyqtSlot(int, float)
    def handle_setpoint_write(self, port: int, value: float):
        # Reject bogus setpoints (e.g. 0 sent by remote clients on first connect)
        if value < MIN_VALID_SETPOINT_THZ:
            self.log_message.emit(
                f"Setpoint write ch{port} REJECTED: {value} THz is below "
                f"minimum ({MIN_VALID_SETPOINT_THZ} THz)"
            )
            return

        self.wlm.set_pid_course_num(port, value)

        try:
            sp = self.wlm.get_pid_course_num(port)
            self.log_message.emit(f"Setpoint write ch{port}: {sp:.7f}")
            delta = {"setpoint": float(sp)}
            self.state.update_status(port, delta)
            self.status_updated.emit(port, delta)
        except Exception as e:
            self.log_message.emit(f"Setpoint readback ch{port} failed: {e}")

    @pyqtSlot(int, float)
    def handle_voltage_write(self, port: int, value: float):
        self.wlm.set_deviation_signal(port, value)

        try:
            v = float(self.wlm.get_deviation_signal(port))
            self.log_message.emit(f"Voltage write ch{port}: {v}")
            self.state.update_measurement(port, {"volt": v})
        except Exception as e:
            self.log_message.emit(f"Voltage readback ch{port} failed: {e}")

    @pyqtSlot(int, bool, bool)
    def handle_switcher_write(self, port: int, use: bool, show: bool):
        self.wlm.set_switcher_signal(port, int(use), int(show))
        self.log_message.emit(f"Switcher write ch{port}: use={use} show={show}")

        # targeted readback: use/show only
        try:
            u, s = self.wlm.get_switcher_signal(port)
            delta = {"use": bool(u), "show": bool(s)}
            self.state.update_status(port, delta)
            self.status_updated.emit(port, delta)
        except Exception as e:
            self.log_message.emit(f"Switcher readback ch{port} failed: {e}")

    @pyqtSlot(int, bool)
    def handle_lock_toggle(self, port: int, enabled: bool):
        # This is the ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œarmingÃƒÂ¢Ã¢â€šÂ¬Ã‚Â state, not lock_status.
        self.wlm.set_channel_assignment(port, enabled)
        self._lock_enabled[port] = bool(enabled)

        self.log_message.emit(f"Lock enable ch{port}: {enabled}")

        # targeted readback: lock_enabled only (and optionally emit for UI/ZMQ immediately)
        delta = {"lock_enabled": bool(enabled)}
        self.state.update_status(port, delta)
        self.status_updated.emit(port, delta)

    @pyqtSlot(bool)
    def handle_autocal_toggle(self, enable: bool):
        self.wlm.set_autocal_mode(1 if enable else 0)
        self.log_message.emit(f"Autocal set: {enable}")

        # targeted readback: autocal only
        try:
            ac = (self.wlm.get_autocal_mode() == 1)
            delta = {"autocal": bool(ac)}
            self.state.update_globals(delta)
            self.globals_updated.emit(delta)
        except Exception as e:
            self.log_message.emit(f"Autocal readback failed: {e}")

    @pyqtSlot(bool)
    def handle_deviation_toggle(self, enable: bool):
        self.wlm.set_deviation_mode(1 if enable else 0)
        self.log_message.emit(f"Deviation mode set: {enable}")

        # targeted readback: deviation_mode only
        try:
            dm = (self.wlm.get_deviation_mode() == 1)
            delta = {"deviation_mode": bool(dm)}
            self.state.update_globals(delta)
            self.globals_updated.emit(delta)
        except Exception as e:
            self.log_message.emit(f"Deviation mode readback failed: {e}")

    @pyqtSlot()
    def handle_save_config(self):
        """Save current PID config to JSON. Runs on worker thread for DLL safety."""
        try:
            config.save_config(self.wlm, PORTS)
            self.log_message.emit("Config saved manually.")
            self.config_saved.emit(True, f"PID settings saved to {config.CONFIG_PATH}")
        except Exception as e:
            self.log_message.emit(f"Manual config save failed: {e}")
            self.config_saved.emit(False, str(e))


class ZMQPubWorker(QThread):
    log_message = pyqtSignal(str)

    def __init__(self, shared_state, pub_port=3797, pub_period_s=PUB_PERIOD_S):
        super().__init__()
        self.state = shared_state
        self.pub_port = int(pub_port)
        self.pub_period_s = float(pub_period_s)
        self._running = True

    def stop(self):
        self._running = False
        self.requestInterruption()

    def run(self):
        ctx = zmq.Context()  # per-thread context
        pub = ctx.socket(zmq.PUB)
        pub.setsockopt(zmq.LINGER, 0)

        try:
            pub.bind(f"tcp://0.0.0.0:{self.pub_port}")
        except zmq.ZMQError as e:
            self.log_message.emit(f"ZMQ PUB bind error: {e}")
            return

        # give subscribers a moment (similar spirit to v10)
        time.sleep(0.1)

        last = 0.0
        while self._running and not self.isInterruptionRequested():
            now = time.time()
            if now - last >= self.pub_period_s:
                pub.send_string("heartbeat")
                all_meas = self.state.get_all_measurements()
                for port in PORTS:
                    m = all_meas.get(port, {})
                    f = m.get("freq_display", None)
                    pub.send_string(f"{port} {0.0 if f is None else f}")
                last = now

            time.sleep(0.01)  # light idle

        try:
            pub.close()
            ctx.term()
        except Exception:
            pass


class ZMQRepWorker(QThread):
    request_setpoint_write = pyqtSignal(int, float)
    log_message = pyqtSignal(str)

    def __init__(self, shared_state, req_port=3796, wait_for_lock=True):
        super().__init__()
        self.state = shared_state
        self.req_port = int(req_port)
        self.wait_for_lock = bool(wait_for_lock)
        self._running = True

    def stop(self):
        self._running = False
        self.requestInterruption()

    def run(self):
        ctx = zmq.Context()  # per-thread context
        rep = ctx.socket(zmq.REP)
        rep.setsockopt(zmq.LINGER, 0)

        try:
            rep.bind(f"tcp://0.0.0.0:{self.req_port}")
        except zmq.ZMQError as e:
            self.log_message.emit(f"ZMQ REP bind error: {e}")
            return

        poller = zmq.Poller()
        poller.register(rep, zmq.POLLIN)

        while self._running and not self.isInterruptionRequested():
            socks = dict(poller.poll(250))  # lets us exit cleanly
            if rep not in socks:
                continue

            try:
                msg = rep.recv_string()
                resp = self._handle_msg(msg)
                rep.send_string(resp)
            except Exception as e:
                try:
                    rep.send_string(json.dumps({"status": "ERROR", "message": str(e)}))
                except Exception:
                    pass

        try:
            rep.close()
            ctx.term()
        except Exception:
            pass

    def _handle_msg(self, msg: str) -> str:
        d = json.loads(msg)
        action = d.get("action")

        if action == "HELLO":
            return json.dumps({"status": "SUCCESS"})

        if action == "CHECK_VALUE":
            p = int(d["connection"])
            st = self.state.get_status(p)
            return json.dumps({"status": "SUCCESS", "value": st.get("setpoint", 0.0)})

        if action == "PROGRAM_VALUE":
            p = int(d["connection"])
            target = float(d["value"])

            self.request_setpoint_write.emit(p, target)

            # Let the client opt in/out of lock-wait; default to instance setting.
            # Manual-mode clients send wait_for_lock=False for immediate return.
            # Buffered-shot clients send wait_for_lock=True to block until converged.
            wait = d.get("wait_for_lock", self.wait_for_lock)

            st = self.state.get_status(p)
            gl = self.state.get_globals()
            lock_enabled = bool(st.get("lock_enabled", False))
            dev_mode = bool(gl.get("deviation_mode", False))

            if wait and lock_enabled and dev_mode:
                self.log_message.emit(f"ZMQ: waiting for lock ch{p} target={target}")
                ok = self._wait_for_lock(p, target)
                return json.dumps({"status": "SUCCESS" if ok else "ERROR",
                                   "message": "" if ok else "Timeout waiting for lock"})
            else:
                return json.dumps({"status": "SUCCESS"})

        return json.dumps({"status": "ERROR", "message": "Unknown action"})

    def _wait_for_lock(self, port: int, target: float) -> bool:
        t0 = time.perf_counter()
        consecutive = 0

        while time.perf_counter() - t0 < LOCK_TIMEOUT_S:
            if self.isInterruptionRequested() or not self._running:
                return False

            meas = self.state.get_measurement(port)
            valid = bool(meas.get("valid", False))
            f_raw = meas.get("freq_raw", None)

            # Require a new sample: skip -7
            if not valid or f_raw == wlmConst.InfNothingChanged:
                consecutive = 0
                time.sleep(0.1)
                continue

            f = meas.get("freq_display", None)
            if f is None:
                consecutive = 0
                time.sleep(0.1)
                continue

            try:
                if abs(float(f) - target) < LOCK_TOLERANCE:
                    consecutive += 1
                    if consecutive >= 2:
                        self.log_message.emit(f"ZMQ: lock achieved ch{port}")
                        return True
                else:
                    consecutive = 0
            except Exception:
                consecutive = 0

            time.sleep(0.1)

        self.log_message.emit(f"ZMQ: lock timeout ch{port}")
        return False
