# display.py
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg
from collections import deque
import math
import time

LOCK_TOL = 0.000005  # THz


class ChannelControl(QtWidgets.QWidget):
    request_setpoint = QtCore.pyqtSignal(int, float)
    request_voltage = QtCore.pyqtSignal(int, float)
    request_lock = QtCore.pyqtSignal(int, bool)
    request_switcher = QtCore.pyqtSignal(int, bool, bool)

    def __init__(self, port: int, name: str):
        super().__init__()
        self.port = port
        self.name = name

        # Status/cache from controller
        self._setpoint = 0.0
        self._lock_enabled = False          # "arming" state (button)
        self._global_deviation_mode = False # global deviation mode state

        # Guard: after the user clicks "Set Freq" or "Set V", ignore
        # incoming overwrites for a short window so the pull-based
        # refresh doesn't clobber the text box before the worker
        # has confirmed the new value.
        self._setpoint_pending_until = 0.0
        self._voltage_pending_until = 0.0
        self._PENDING_GUARD_S = 1.0        # seconds to suppress overwrites

        # Plot buffers
        self._t0 = time.perf_counter()
        self.t = deque(maxlen=100)
        self.f = deque(maxlen=100)
        self.v = deque(maxlen=100)

        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        # Header row
        header = QtWidgets.QHBoxLayout()
        self.status_label = QtWidgets.QLabel(f"<b>{self.name} (Ch {self.port})</b>")
        self.lock_btn = QtWidgets.QPushButton("Enable Lock")
        self.lock_btn.setCheckable(True)
        self.lock_btn.clicked.connect(self._on_lock_toggled)
        header.addWidget(self.status_label)
        header.addWidget(self.lock_btn)
        layout.addLayout(header)

        # Controls row
        controls = QtWidgets.QHBoxLayout()
        self.chk_use = QtWidgets.QCheckBox("Use")
        self.chk_show = QtWidgets.QCheckBox("Show")
        self.chk_use.clicked.connect(self._on_switcher)
        self.chk_show.clicked.connect(self._on_switcher)

        self.input_set = QtWidgets.QLineEdit()
        self.input_set.setPlaceholderText("Setpoint (THz)")
        self.btn_set = QtWidgets.QPushButton("Set Freq")
        self.btn_set.clicked.connect(self._on_setpoint)

        self.input_volt = QtWidgets.QLineEdit()
        self.input_volt.setPlaceholderText("Voltage (mV)")
        self.btn_volt = QtWidgets.QPushButton("Set V")
        self.btn_volt.clicked.connect(self._on_voltage)

        controls.addWidget(self.chk_use)
        controls.addWidget(self.chk_show)
        controls.addWidget(self.input_set)
        controls.addWidget(self.btn_set)
        controls.addWidget(self.input_volt)
        controls.addWidget(self.btn_volt)
        layout.addLayout(controls)

        # Frequency plot
        self.plot_freq = pg.PlotWidget(title=f"Ch {self.port} Frequency (THz)")
        self.curve_freq = self.plot_freq.plot()
        self.line_setpoint = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('g', style=QtCore.Qt.DashLine))
        self.line_tol_up = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.line_tol_dn = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.plot_freq.addItem(self.line_setpoint)
        self.plot_freq.addItem(self.line_tol_up)
        self.plot_freq.addItem(self.line_tol_dn)

        # Voltage plot
        self.plot_volt = pg.PlotWidget(title=f"Ch {self.port} Deviation Signal (mV)")
        self.curve_volt = self.plot_volt.plot()
        self.line_bound_min = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.line_bound_max = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.plot_volt.addItem(self.line_bound_min)
        self.plot_volt.addItem(self.line_bound_max)

        layout.addWidget(self.plot_freq)
        layout.addWidget(self.plot_volt)

        # Exposure + power bars
        self.lbl_exp = QtWidgets.QLabel("Exposure: N/A")
        layout.addWidget(self.lbl_exp)

        pwr = QtWidgets.QHBoxLayout()
        self.bar_amp1 = QtWidgets.QProgressBar()
        self.bar_amp1.setRange(0, 5000)
        self.bar_amp1.setTextVisible(False)
        self.bar_amp2 = QtWidgets.QProgressBar()
        self.bar_amp2.setRange(0, 5000)
        self.bar_amp2.setTextVisible(False)

        pwr.addWidget(QtWidgets.QLabel("CCD1:"))
        pwr.addWidget(self.bar_amp1)
        pwr.addWidget(QtWidgets.QLabel("CCD2:"))
        pwr.addWidget(self.bar_amp2)
        layout.addLayout(pwr)

    # ----- controller-fed state -----
    def set_globals(self, g: dict):
        # controller should call this after merging globals deltas
        self._global_deviation_mode = bool(g.get("deviation_mode", False))

    def update_fast(self, meas: dict):
        """
        meas keys from workers.py:
          - valid: bool
          - freq_plot: float or None   (None => plot gap)
          - freq_display: float or None
          - volt: float
          - exp: (e1,e2)
          - amp: (a1,a2)
        """
        now = time.perf_counter() - self._t0

        valid = bool(meas.get("valid", False))
        f_plot = meas.get("freq_plot", None)       # None => gap
        f_disp = meas.get("freq_display", None)    # for text readout
        vval = float(meas.get("volt", 0.0))

        # Frequency plot: gap on invalid / missing f_plot
        if (not valid) or (f_plot is None):
            fplot_val = float("nan")
        else:
            fplot_val = float(f_plot)

        self.t.append(now)
        self.f.append(fplot_val)
        self.v.append(vval)

        self.curve_freq.setData(list(self.t), list(self.f))
        self.curve_volt.setData(list(self.t), list(self.v))

        e1, e2 = meas.get("exp", (0.0, 0.0))
        self.lbl_exp.setText(f"Exposure: {float(e1):.1f} ms + {float(e2):.1f} ms")

        a1, a2 = meas.get("amp", (0.0, 0.0))
        self.bar_amp1.setValue(int(a1))
        self.bar_amp2.setValue(int(a2))

        # Derived lock_status (arming state + global deviation mode + within tolerance)
        if valid and (f_disp is not None):
            in_tol = abs(float(f_disp) - float(self._setpoint)) < LOCK_TOL
            locked = bool(self._lock_enabled and self._global_deviation_mode and in_tol)
        else:
            locked = False

        # Status text
        if not valid:
            tag = "<span style='color:#7f8c8d'>NO SIGNAL</span>"
            ftxt = "N/A"
        else:
            tag = "<span style='color:#27ae60'>Locked</span>" if locked else "<span style='color:#e67e22'>Unlocked</span>"
            # Prefer displaying freq_display (stable), but fall back gracefully
            if f_disp is None or (isinstance(fplot_val, float) and math.isnan(fplot_val)):
                ftxt = "N/A"
            else:
                ftxt = f"{float(f_disp):.6f}"

        self.status_label.setText(f"<b>{self.name}: {ftxt} THz â€” {tag}</b>")

    def update_slow(self, status: dict):
        """
        status keys from workers.py (full snapshot or deltas):
          - setpoint, use, show, bound_min, bound_max, lock_enabled
        """
        if "setpoint" in status:
            sp = float(status.get("setpoint", 0.0))
            self._setpoint = sp
            self.line_setpoint.setPos(sp)
            self.line_tol_up.setPos(sp + LOCK_TOL)
            self.line_tol_dn.setPos(sp - LOCK_TOL)

            if not self.input_set.hasFocus() and time.perf_counter() > self._setpoint_pending_until:
                self.input_set.setText(f"{sp:.6f}")

        if "bound_min" in status or "bound_max" in status:
            bmin = float(status.get("bound_min", self.line_bound_min.value()))
            bmax = float(status.get("bound_max", self.line_bound_max.value()))
            self.line_bound_min.setPos(bmin)
            self.line_bound_max.setPos(bmax)

        if "use" in status or "show" in status:
            self.chk_use.blockSignals(True)
            self.chk_show.blockSignals(True)
            self.chk_use.setChecked(bool(status.get("use", self.chk_use.isChecked())))
            self.chk_show.setChecked(bool(status.get("show", self.chk_show.isChecked())))
            self.chk_use.blockSignals(False)
            self.chk_show.blockSignals(False)

        # Lock button reflects lock_enabled (arming state)
        if "lock_enabled" in status:
            lock_val = bool(status.get("lock_enabled", False))
            self._lock_enabled = lock_val
            self.lock_btn.blockSignals(True)
            self.lock_btn.setChecked(lock_val)
            self.lock_btn.setText("LOCK ENABLED" if lock_val else "Enable Lock")
            self.lock_btn.setStyleSheet(
                f"background-color: {'#27ae60' if lock_val else '#c0392b'}; color: white;"
            )
            self.lock_btn.blockSignals(False)

    # ----- user actions -----
    def _on_setpoint(self):
        try:
            val = float(self.input_set.text())
            self._setpoint_pending_until = time.perf_counter() + self._PENDING_GUARD_S
            self.request_setpoint.emit(self.port, val)
        except Exception:
            pass

    def _on_voltage(self):
        try:
            val = float(self.input_volt.text())
            self._voltage_pending_until = time.perf_counter() + self._PENDING_GUARD_S
            self.request_voltage.emit(self.port, val)
        except Exception:
            pass

    def _on_lock_toggled(self):
        self.request_lock.emit(self.port, self.lock_btn.isChecked())

    def _on_switcher(self):
        self.request_switcher.emit(self.port, self.chk_use.isChecked(), self.chk_show.isChecked())


class GlobalControl(QtWidgets.QWidget):
    request_autocal = QtCore.pyqtSignal(bool)
    request_deviation = QtCore.pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        layout = QtWidgets.QHBoxLayout(self)

        self.lbl_temp = QtWidgets.QLabel("T: N/A")
        self.lbl_press = QtWidgets.QLabel("P: N/A")

        self.btn_auto = QtWidgets.QPushButton("Autocal OFF")
        self.btn_auto.setCheckable(True)
        self.btn_auto.clicked.connect(lambda: self.request_autocal.emit(self.btn_auto.isChecked()))

        self.btn_dev = QtWidgets.QPushButton("Deviation OFF")
        self.btn_dev.setCheckable(True)
        self.btn_dev.clicked.connect(lambda: self.request_deviation.emit(self.btn_dev.isChecked()))

        layout.addWidget(self.lbl_temp)
        layout.addWidget(self.lbl_press)
        layout.addWidget(self.btn_auto)
        layout.addWidget(self.btn_dev)

    def update_globals(self, g: dict):
        # g may be full snapshot or delta; controller should merge before calling
        self.lbl_temp.setText(f"T: {float(g.get('temperature', 0.0)):.2f} C")
        self.lbl_press.setText(f"P: {float(g.get('pressure', 0.0)):.2f} mbar")

        ac = bool(g.get("autocal", False))
        self.btn_auto.blockSignals(True)
        self.btn_auto.setChecked(ac)
        self.btn_auto.setText(f"Autocal {'ON' if ac else 'OFF'}")
        self.btn_auto.setStyleSheet(f"background-color: {'#27ae60' if ac else '#c0392b'}; color: white;")
        self.btn_auto.blockSignals(False)

        dm = bool(g.get("deviation_mode", False))
        self.btn_dev.blockSignals(True)
        self.btn_dev.setChecked(dm)
        self.btn_dev.setText(f"Deviation {'ON' if dm else 'OFF'}")
        self.btn_dev.setStyleSheet(f"background-color: {'#27ae60' if dm else '#c0392b'}; color: white;")
        self.btn_dev.blockSignals(False)
