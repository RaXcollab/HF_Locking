# controller.py
import sys
import threading
from PyQt5 import QtWidgets, QtCore, QtGui
import wlm_utils
import workers
import display
import config

# Elevate process priority to reduce latency jitter. ABOVE_NORMAL (base 10) is
# preferred over HIGH (base 13) here because Microsoft explicitly warns that
# HIGH should be reserved for brief time-critical *events*, not sustained
# loops — the wavemeter polls continuously at 20 ms, so HIGH risks starving
# system threads (disk, audio, USB) which sit at base priority 8-12.
# ABOVE_NORMAL still beats NORMAL and the foreground-priority boost (which
# only affects NORMAL-class processes), without crowding out the kernel.
# Doesn't require admin either. See Scheduling Priorities docs.
import ctypes
import ctypes.wintypes
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
_kernel32.SetPriorityClass.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
_kernel32.SetPriorityClass.restype = ctypes.wintypes.BOOL
_handle = _kernel32.GetCurrentProcess()
if _kernel32.SetPriorityClass(_handle, 0x00008000):       # ABOVE_NORMAL_PRIORITY_CLASS
    print("[PRIORITY] Set to ABOVE_NORMAL")
else:
    _err = ctypes.get_last_error()
    print(f"[PRIORITY] Failed to set priority (error {_err})")

# Opt out of Windows EcoQoS execution-speed throttling so the wavemeter UI and
# worker threads keep running at full CPU clock when the window loses focus.
# Requires Windows 10 1709+ (build 16299). Independent of priority class.
class _PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.wintypes.ULONG),
        ("ControlMask", ctypes.wintypes.ULONG),
        ("StateMask", ctypes.wintypes.ULONG),
    ]
_kernel32.SetProcessInformation.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.c_int,
    ctypes.c_void_p, ctypes.wintypes.DWORD,
]
_kernel32.SetProcessInformation.restype = ctypes.wintypes.BOOL
_state = _PROCESS_POWER_THROTTLING_STATE()
_state.Version     = 1     # PROCESS_POWER_THROTTLING_CURRENT_VERSION
_state.ControlMask = 0x1   # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
_state.StateMask   = 0     # 0 with ControlMask set => disable throttling
_PROCESS_INFO_POWER_THROTTLING = 4  # ProcessPowerThrottling enum
if _kernel32.SetProcessInformation(
    _handle, _PROCESS_INFO_POWER_THROTTLING,
    ctypes.byref(_state), ctypes.sizeof(_state),
):
    print("[POWER] EcoQoS execution-speed throttling disabled")
else:
    _err = ctypes.get_last_error()
    print(f"[POWER] Failed to disable power throttling (error {_err})")

CHANNEL_NAMES = {
    1: "Ch_1", 
    2: "Ch_2", 
    3: "Vexlum", 
    4: "TiSa_1",
    5: "Ch_5", 
    6: "Ch_6", 
    7: "Ch7", 
    8: "Rb_Ref",
}

PORTS = range(1, 9)

# GUI refresh rates decoupled from worker poll rates.
GUI_FAST_MS = 33    # measurements, plots (~30 FPS)
GUI_SLOW_MS = 1000  # status, globals — matches worker slow producer (1 Hz)

ICON_PATH = "laser.ico"  # Path to your custom icon file
WINDOW_TITLE = "HighFinesse WLM Controller"
TARGET_SCREEN = r"\\.\DISPLAY5"  # Match by QScreen.name(); fallback to primary

class _RestoreDialog(QtWidgets.QDialog):
    """Dialog showing config differences with checkboxes for selective restore."""

    console_response = QtCore.pyqtSignal(bool)

    def __init__(self, diffs, channel_names, saved_at, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restore PID Config")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        self._diffs = diffs
        self._checkboxes = {}  # {(port, name): QCheckBox}

        # Console fallback: background stdin reader emits this when user types
        # 'y' / 'n'. Routed through a slot so the accept/reject happens on the
        # Qt main thread (AutoConnection -> QueuedConnection across threads).
        self.console_response.connect(self._on_console_response)

        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel(
            f"Saved config from: {saved_at}\n"
            f"The following settings differ from the current WLM state.\n"
            f"Check the settings you want to restore:"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QtWidgets.QWidget()
        scroll_layout = QtWidgets.QVBoxLayout(scroll_widget)

        for port in sorted(diffs.keys()):
            ch_name = channel_names.get(port, f"Ch {port}")
            group = QtWidgets.QGroupBox(f"{ch_name} (Port {port})")
            group_layout = QtWidgets.QVBoxLayout(group)

            for name, live_val, saved_val in diffs[port]:
                if isinstance(live_val, float):
                    text = f"{name}: {live_val:.9g} \u2192 {saved_val:.9g}"
                else:
                    text = f"{name}: {live_val} \u2192 {saved_val}"

                cb = QtWidgets.QCheckBox(text)
                cb.setChecked(True)
                group_layout.addWidget(cb)
                self._checkboxes[(port, name)] = cb

            scroll_layout.addWidget(group)

        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)

        btn_row_top = QtWidgets.QHBoxLayout()
        btn_all = QtWidgets.QPushButton("Select All")
        btn_none = QtWidgets.QPushButton("Deselect All")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none.clicked.connect(lambda: self._set_all(False))
        btn_row_top.addWidget(btn_all)
        btn_row_top.addWidget(btn_none)
        btn_row_top.addStretch()
        layout.addLayout(btn_row_top)

        btn_row = QtWidgets.QHBoxLayout()
        btn_restore = QtWidgets.QPushButton("Restore Selected")
        btn_skip = QtWidgets.QPushButton("Skip (Keep Current)")
        btn_restore.clicked.connect(self.accept)
        btn_skip.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_restore)
        btn_row.addWidget(btn_skip)
        layout.addLayout(btn_row)

    def _set_all(self, checked):
        for cb in self._checkboxes.values():
            cb.setChecked(checked)

    def get_approved_settings(self):
        """Return dict: {port: {name: saved_value}} for checked settings."""
        approved = {}
        for (port, name), cb in self._checkboxes.items():
            if cb.isChecked():
                for diff_name, _live, saved in self._diffs[port]:
                    if diff_name == name:
                        approved.setdefault(port, {})[name] = saved
                        break
        return approved

    @QtCore.pyqtSlot(bool)
    def _on_console_response(self, accept):
        """Resolve the modal dialog from the console stdin reader.
        accept=True keeps every checkbox in its current (default = all checked) state.
        """
        if accept:
            print("[CONFIG] Console: restoring all flagged settings")
            self.accept()
        else:
            print("[CONFIG] Console: skipping restore")
            self.reject()


class ExperimentController(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(QtGui.QIcon(ICON_PATH))
        self.setWindowTitle(WINDOW_TITLE)
        self._initial_position_done = False

        # Caches for delta-merge (used for status/globals write-handler signals)
        self._status_cache = {p: {} for p in PORTS}
        self._globals_cache = {}

        # WLM client in main thread (your preference)
        self.wlm = wlm_utils.wlm_link()
        self.shared = workers.SharedExperimentState()

        # Worker thread + QObject worker
        self.thread_wlm = QtCore.QThread(self)
        self.worker_wlm = workers.WavemeterWorker(self.wlm, self.shared)
        self.worker_wlm.moveToThread(self.thread_wlm)

        # ZMQ Workers
        self.zmq_pub = workers.ZMQPubWorker(self.shared, pub_port=3797)
        self.zmq_rep = workers.ZMQRepWorker(self.shared, req_port=3796, wait_for_lock=True)

        # UI
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(2)

        self.grid = QtWidgets.QGridLayout()
        self.grid.setSpacing(4)
        vbox.addLayout(self.grid, 1)

        self.channels = {}
        for port in PORTS:
            widget = display.ChannelControl(port, CHANNEL_NAMES.get(port, f"Ch {port}"))
            self.channels[port] = widget

            # Widget -> worker commands: explicit QueuedConnection ensures these
            # always run on the worker thread, never blocking the GUI.
            widget.request_setpoint.connect(self.worker_wlm.handle_setpoint_write, QtCore.Qt.QueuedConnection)
            widget.request_voltage.connect(self.worker_wlm.handle_voltage_write, QtCore.Qt.QueuedConnection)
            widget.request_lock.connect(self.worker_wlm.handle_lock_toggle, QtCore.Qt.QueuedConnection)
            widget.request_switcher.connect(self.worker_wlm.handle_switcher_write, QtCore.Qt.QueuedConnection)

            # 4 rows -- 2 columns: port 1-2 in row 0, 3-4 in row 1, etc.
            self.grid.addWidget(widget, (port - 1) // 2, (port - 1) % 2)

        self.global_ctrl = display.GlobalControl()
        self.global_ctrl.request_autocal.connect(self.worker_wlm.handle_autocal_toggle, QtCore.Qt.QueuedConnection)
        self.global_ctrl.request_deviation.connect(self.worker_wlm.handle_deviation_toggle, QtCore.Qt.QueuedConnection)
        self.global_ctrl.request_save_config.connect(self.worker_wlm.handle_save_config, QtCore.Qt.QueuedConnection)
        self.global_ctrl.request_backup_wlm.connect(self.worker_wlm.handle_backup_wlm, QtCore.Qt.QueuedConnection)
        vbox.addWidget(self.global_ctrl)

        # Worker -> UI: only write-handler feedback (infrequent, no backlog risk)
        self.thread_wlm.started.connect(self.worker_wlm.start_polling)
        self.worker_wlm.status_updated.connect(self.handle_slow_update)
        self.worker_wlm.globals_updated.connect(self.handle_globals_update)
        self.worker_wlm.config_saved.connect(self._on_config_saved)
        self.worker_wlm.wlm_backup_done.connect(self._on_wlm_backup_done)

        # GUI refresh timers: PULL model with two cadences.
        # Fast: measurements + plots (~30 FPS)
        self._busy_gui_fast = False  # re-entrancy guard for _refresh_gui_fast
        self._gui_skip_count = 0     # frames skipped due to overload
        self._gui_frame_count = 0    # total frames attempted
        self._gui_timer_fast = QtCore.QTimer(self)
        self._gui_timer_fast.setTimerType(QtCore.Qt.CoarseTimer)
        self._gui_timer_fast.timeout.connect(self._refresh_gui_fast)
        self._gui_timer_fast.start(GUI_FAST_MS)

        # Slow: status + globals at 1 Hz (setpoints, bounds, T, P)
        self._gui_timer_slow = QtCore.QTimer(self)
        self._gui_timer_slow.setTimerType(QtCore.Qt.CoarseTimer)
        self._gui_timer_slow.timeout.connect(self._refresh_gui_slow)
        self._gui_timer_slow.start(GUI_SLOW_MS)

        # Logging
        self.worker_wlm.log_message.connect(lambda s: print(f"[WLM] {s}"))
        self.zmq_pub.log_message.connect(lambda s: print("[ZMQ PUB]", s))
        self.zmq_rep.log_message.connect(lambda s: print("[ZMQ REP]", s))

        # ZMQ -> Worker command (also cross-thread)
        self.zmq_rep.request_setpoint_write.connect(self.worker_wlm.handle_setpoint_write, QtCore.Qt.QueuedConnection)

        # Safer shutdown sequencing: stop worker, then quit thread
        self.worker_wlm.finished.connect(self.thread_wlm.quit)

        # Config restore (before starting worker — no DLL concurrency)
        self._try_restore_config()

        # Start
        self.thread_wlm.start()
        self.zmq_pub.start()
        self.zmq_rep.start()

    def _refresh_gui_fast(self):
        """Pull measurements at ~30 FPS -- plots, frequency readouts, exposure, amplitude."""
        self._gui_frame_count += 1
        if self._busy_gui_fast:
            self._gui_skip_count += 1
            return
        self._busy_gui_fast = True
        try:
            meas = self.shared.get_all_measurements()
            for port, m in meas.items():
                if port in self.channels:
                    self.channels[port].update_fast(m)
        finally:
            self._busy_gui_fast = False

    def _refresh_gui_slow(self):
        """Pull status + globals at 1 Hz -- setpoints, bounds, switcher, lock, T, P."""
        # Log frame skip rate (overload indicator)
        if self._gui_skip_count > 0:
            print(f"[PERF] Skipped {self._gui_skip_count}/{self._gui_frame_count} frames")
        self._gui_skip_count = 0
        self._gui_frame_count = 0
        snap = self.shared.get_gui_snapshot()
        for port, s in snap["status"].items():
            if port in self.channels:
                self.channels[port].update_slow(s)

        g = snap["globals"]
        self.global_ctrl.update_globals(g)
        for w in self.channels.values():
            w.set_globals(g)

    @QtCore.pyqtSlot(int, dict)
    def handle_slow_update(self, port: int, status_delta: dict):
        """Only called by write handlers for immediate feedback."""
        if port not in self.channels:
            return
        self._status_cache[port].update(status_delta)
        self.channels[port].update_slow(self._status_cache[port])

    @QtCore.pyqtSlot(dict)
    def handle_globals_update(self, g_delta: dict):
        """Only called by write handlers for immediate feedback."""
        self._globals_cache.update(g_delta)
        self.global_ctrl.update_globals(self._globals_cache)
        for w in self.channels.values():
            w.set_globals(self._globals_cache)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_position_done:
            self._initial_position_done = True
            target = None
            for s in QtWidgets.QApplication.screens():
                if s.name() == TARGET_SCREEN:
                    target = s
                    break
            screen = (target or QtWidgets.QApplication.primaryScreen()).availableGeometry()
            frame = self.frameGeometry().height() - self.geometry().height()
            half_w = screen.width() // 2
            self.move(screen.x(), screen.y())
            self.resize(half_w, screen.height() - frame)

    def _try_restore_config(self):
        """Compare saved config with live WLM state and offer to restore differences."""
        if not self.wlm.is_active():
            print("[CONFIG] WLM not active. Skipping config restore.")
            return

        saved = config.load_config()
        if saved is None:
            print("[CONFIG] No saved config found. Will save on exit.")
            return

        live = config.read_live_state(self.wlm, PORTS)
        diffs = config.compare_configs(live, saved["ports"])

        if not diffs:
            print("[CONFIG] Saved config matches current WLM state. No restore needed.")
            return

        summary = config.format_diff_summary(diffs, CHANNEL_NAMES)
        print("[CONFIG] Differences found:")
        print(summary)

        dialog = _RestoreDialog(diffs, CHANNEL_NAMES, saved.get("saved_at", "unknown"), self)

        # Always-on-top + explicit placement on TARGET_SCREEN: the dialog runs
        # before win.show(), so its parent has no geometry yet — on multi-monitor
        # Win11 it could otherwise pop on a screen the user isn't watching.
        dialog.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint, True)
        target = None
        for s in QtWidgets.QApplication.screens():
            if s.name() == TARGET_SCREEN:
                target = s
                break
        scr = (target or QtWidgets.QApplication.primaryScreen()).availableGeometry()
        # adjustSize() forces a layout pass so sizeHint() reflects the actual
        # contents (the diff list can exceed the 500x400 minimum). Cheap to call
        # on an unshown dialog.
        dialog.adjustSize()
        dw = max(dialog.sizeHint().width(), dialog.minimumWidth())
        dh = max(dialog.sizeHint().height(), dialog.minimumHeight())
        dialog.move(scr.x() + (scr.width() - dw) // 2, scr.y() + (scr.height() - dh) // 2)

        # Console fallback: a daemon thread reads stdin so the user can also
        # type 'y'/'n' in the launching CMD console instead of clicking the
        # dialog. Whichever resolves first wins. Daemon thread is killed on
        # process exit; a stray line after exec_() returns is consumed and
        # discarded via reader_done.
        reader_done = threading.Event()

        def _stdin_reader():
            try:
                while not reader_done.is_set():
                    line = sys.stdin.readline()
                    if not line:
                        return  # EOF (stdin closed)
                    if reader_done.is_set():
                        return
                    ch = line.strip().lower()
                    if ch in ("y", "yes", "r", "restore"):
                        dialog.console_response.emit(True)
                        return
                    if ch in ("n", "no", "s", "skip"):
                        dialog.console_response.emit(False)
                        return
                    print("[CONFIG] Type 'y' to restore all, 'n' to skip, or use the dialog.")
            except Exception as e:
                print(f"[CONFIG] stdin reader exited: {e!r}")
                return

        threading.Thread(target=_stdin_reader, daemon=True).start()

        print("[CONFIG] >>> ACTION REQUIRED <<< Restore dialog open on the wavemeter screen.")
        print("[CONFIG] Click a dialog button OR type 'y' (restore all) / 'n' (skip) here + Enter.")
        # Defer raise_/activateWindow to the first event-loop tick after exec_()
        # shows the dialog. Calling show() before exec_() would double-show and
        # could re-trigger placement on some Qt 5.15 builds — exactly the failure
        # mode this whole block is trying to prevent.
        QtCore.QTimer.singleShot(0, lambda: (dialog.raise_(), dialog.activateWindow()))
        result = dialog.exec_()
        reader_done.set()  # stdin reader will discard any future line

        if result == QtWidgets.QDialog.Accepted:
            approved = dialog.get_approved_settings()
            if approved:
                for port, settings in approved.items():
                    results = config.restore_settings(self.wlm, port, settings)
                    for name, rc in results.items():
                        if rc == 0:
                            print(f"[CONFIG] Restored {name} on port {port}")
                        else:
                            print(f"[CONFIG] WARNING: {name} on port {port} returned code {rc}")
                print("[CONFIG] Restore complete.")
            else:
                print("[CONFIG] No settings selected for restore.")
        else:
            print("[CONFIG] User declined restore.")

    @QtCore.pyqtSlot(bool, str)
    def _on_config_saved(self, success, message):
        """Handle config_saved signal from worker thread."""
        if success:
            QtWidgets.QMessageBox.information(self, "Config Saved", message)
        else:
            QtWidgets.QMessageBox.warning(self, "Save Failed", message)

    @QtCore.pyqtSlot(bool, str)
    def _on_wlm_backup_done(self, success, message):
        """Handle wlm_backup_done signal from worker thread."""
        if success:
            QtWidgets.QMessageBox.information(self, "WLM Backup", message)
        else:
            QtWidgets.QMessageBox.warning(self, "WLM Backup Failed", message)

    def closeEvent(self, event):
        # Stop GUI refresh
        self._gui_timer_fast.stop()
        self._gui_timer_slow.stop()

        # Stop ZMQ
        try:
            self.zmq_rep.stop(); self.zmq_rep.wait(500)
            self.zmq_pub.stop(); self.zmq_pub.wait(500)
        except Exception:
            pass

        # Stop WLM worker + thread
        try:
            QtCore.QMetaObject.invokeMethod(self.worker_wlm, "stop", QtCore.Qt.QueuedConnection)
            self.thread_wlm.wait(1000)
        except Exception:
            pass

        # Save config (worker stopped — safe to call DLL from main thread)
        try:
            config.save_config(self.wlm, PORTS)
            print("[CONFIG] Config saved on exit.")
        except Exception as e:
            print(f"[CONFIG] WARNING: Failed to save config on exit: {e}")

        event.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setWindowIcon(QtGui.QIcon(ICON_PATH))
    win = ExperimentController()
    win.show()
    sys.exit(app.exec_())
