# controller.py
import sys
from PyQt5 import QtWidgets, QtCore
import wlm_utils
import workers
import display

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

# GUI refresh rates — decoupled from worker poll rates.
GUI_FAST_MS = 50    # measurements, plots (10 Hz — matches human perception)
GUI_SLOW_MS = 1000   # status, globals (1 Hz — setpoints/bounds/T/P rarely change)

class ExperimentController(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HighFinesse WLM Controller V12")
        self.resize(1400, 900)

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

        self.grid = QtWidgets.QGridLayout()
        vbox.addLayout(self.grid)

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

            self.grid.addWidget(widget, (port - 1) // 4, (port - 1) % 4)

        self.global_ctrl = display.GlobalControl()
        self.global_ctrl.request_autocal.connect(self.worker_wlm.handle_autocal_toggle, QtCore.Qt.QueuedConnection)
        self.global_ctrl.request_deviation.connect(self.worker_wlm.handle_deviation_toggle, QtCore.Qt.QueuedConnection)
        vbox.addWidget(self.global_ctrl)

        # Worker -> UI: only write-handler feedback (infrequent, no backlog risk)
        self.thread_wlm.started.connect(self.worker_wlm.start_polling)
        self.worker_wlm.status_updated.connect(self.handle_slow_update)
        self.worker_wlm.globals_updated.connect(self.handle_globals_update)

        # GUI refresh timers: PULL model with two cadences.
        # Fast: measurements + plots at 10 Hz
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

        # Start
        self.thread_wlm.start()
        self.zmq_pub.start()
        self.zmq_rep.start()

    def _refresh_gui_fast(self):
        """Pull measurements at 10 Hz — plots, frequency readouts, exposure, amplitude."""
        meas = self.shared.get_all_measurements()
        for port, m in meas.items():
            if port in self.channels:
                self.channels[port].update_fast(m)

    def _refresh_gui_slow(self):
        """Pull status + globals at 1 Hz — setpoints, bounds, switcher, lock, T, P."""
        status = self.shared.get_all_status()
        for port, s in status.items():
            if port in self.channels:
                self.channels[port].update_slow(s)

        g = self.shared.get_globals()
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

        event.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win = ExperimentController()
    win.show()
    sys.exit(app.exec_())
