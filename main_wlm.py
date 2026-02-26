# controller.py
import sys
from PyQt5 import QtWidgets, QtCore, QtGui
import wlm_utils
import workers
import display
import config

# Set process priority to HIGH to minimize latency jitter (optional, but can help with responsiveness).
import ctypes
ctypes.windll.kernel32.SetPriorityClass(
    ctypes.windll.kernel32.GetCurrentProcess(),
    0x00000080  # HIGH_PRIORITY_CLASS
)
# ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
# HIGH_PRIORITY_CLASS = 0x00000080
# REALTIME_PRIORITY_CLASS = 0x00000100  # Will freeze system, use with caution~

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
GUI_FAST_MS = 30    # measurements, plots
GUI_SLOW_MS = 500   # status, globals (setpoints/bounds/T/P rarely change)

ICON_PATH = "laser.ico"  # Path to your custom icon file
WINDOW_TITLE = "HighFinesse WLM Controller"

class _RestoreDialog(QtWidgets.QDialog):
    """Dialog showing config differences with checkboxes for selective restore."""

    def __init__(self, diffs, channel_names, saved_at, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restore PID Config")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        self._diffs = diffs
        self._checkboxes = {}  # {(port, name): QCheckBox}

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

            # 4 rows Ã— 2 columns: port 1-2 in row 0, 3-4 in row 1, etc.
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
        # Fast: measurements + plots at 10 Hz
        self._gui_timer_fast = QtCore.QTimer(self)
        self._gui_timer_fast.setTimerType(QtCore.Qt.PreciseTimer)
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
        """Pull measurements at 10 Hz â€” plots, frequency readouts, exposure, amplitude."""
        meas = self.shared.get_all_measurements()
        for port, m in meas.items():
            if port in self.channels:
                self.channels[port].update_fast(m)

    def _refresh_gui_slow(self):
        “””Pull status + globals at 1 Hz -- setpoints, bounds, switcher, lock, T, P.”””
        snap = self.shared.get_gui_snapshot()
        for port, s in snap[“status”].items():
            if port in self.channels:
                self.channels[port].update_slow(s)

        g = snap[“globals”]
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
            screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
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
        result = dialog.exec_()

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
