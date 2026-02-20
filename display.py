# display.py
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg
from collections import deque
import math
import time
import numpy as np

LOCK_TOL = 0.000005  # THz

# Voltage plot padding: fraction of data range added above/below
VOLT_PAD_FRAC = 0.15
VOLT_MIN_RANGE = 0.5  # mV minimum visible range when signal is flat

# Frequency offset plot padding
FREQ_PAD_FRAC = 0.15
FREQ_MIN_RANGE = 1.0  # MHz minimum visible range when signal is flat


def _nice_y_range(ymin, ymax, min_span=0.0):
    """
    Expand a data range so that:
      - the span is at least `min_span`
      - the limits snap to "nice" tick boundaries (1-2-5 series)
      - at least 2 major ticks are always visible
    Returns (new_min, new_max, step) where step is the major tick spacing.
    """
    # Enforce minimum span
    span = ymax - ymin
    if span < min_span:
        mid = (ymin + ymax) * 0.5
        ymin = mid - min_span * 0.5
        ymax = mid + min_span * 0.5
        span = min_span

    # Pick a nice tick step targeting ~4-6 ticks
    raw_step = span / 5.0
    if raw_step <= 0:
        raw_step = 1.0
    mag = 10.0 ** math.floor(math.log10(raw_step))
    step = mag  # fallback
    for nice in (1.0, 2.0, 5.0, 10.0):
        step = nice * mag
        if step >= raw_step:
            break

    # Snap to tick-aligned boundaries
    new_min = math.floor(ymin / step) * step
    new_max = math.ceil(ymax / step) * step

    # Guarantee at least 2 ticks
    if new_max - new_min < 2 * step:
        mid = (ymin + ymax) * 0.5
        new_min = math.floor(mid / step) * step - step
        new_max = new_min + 2 * step

    return new_min, new_max, step


class ElapsedAxisItem(pg.AxisItem):
    """
    X-axis that displays elapsed time as stable integer seconds.
    Avoids the default float formatting that causes digit flashing.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):
        return [f"{int(v)}s" for v in values]


class ChannelControl(QtWidgets.QWidget):
    request_setpoint = QtCore.pyqtSignal(int, float)
    request_voltage = QtCore.pyqtSignal(int, float)
    request_lock = QtCore.pyqtSignal(int, bool)
    request_switcher = QtCore.pyqtSignal(int, bool, bool)

    def __init__(self, port: int, name: str):
        super().__init__()
        self.port = port
        self.name = name

        # Rb_Ref (or any "Ref" channel): use exact setpoint as reference
        # so the plot shows deviation from setpoint directly (0 = on target).
        self._use_exact_ref = ("Ref" in name)

        # Status/cache from controller
        self._setpoint = 0.0
        self._freq_ref = 0.0                # plot reference (THz units)
        self._sp_mhz = 0.0                  # cached setpoint in MHz-offset coords
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
        self.f = deque(maxlen=100)     # stores MHz offset from _freq_ref
        self.v = deque(maxlen=100)

        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # -- Row 1: name/freq/status + [Lock Button] --
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(6)
        self.status_label = QtWidgets.QLabel(f"<b>{self.name} (Ch {self.port})</b>")
        self.status_label.setStyleSheet("font-size: 14pt;")

        self.lock_btn = QtWidgets.QPushButton("Enable Lock")
        self.lock_btn.setCheckable(True)
        self.lock_btn.setFixedHeight(32)
        self.lock_btn.setStyleSheet("font-size: 11pt; font-weight: bold;")
        self.lock_btn.clicked.connect(self._on_lock_toggled)

        row1.addWidget(self.status_label, 3)
        row1.addWidget(self.lock_btn, 2)      # stretches to ~40% of row
        layout.addLayout(row1)

        # -- Row 2: Use | Show | Incl SP | [setpoint] Set Freq | [mV] Set V | Exp | CCD bar --
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(2)

        self.chk_use = QtWidgets.QCheckBox("Use")
        self.chk_show = QtWidgets.QCheckBox("Show")
        self.chk_use.clicked.connect(self._on_switcher)
        self.chk_show.clicked.connect(self._on_switcher)

        # Toggle: include setpoint line in freq-plot Y autoscale
        self.chk_incl_sp = QtWidgets.QCheckBox("Incl SP")
        self.chk_incl_sp.setChecked(True)
        self.chk_incl_sp.setToolTip("Include setpoint in frequency plot Y-axis autoscale")

        self.input_set = QtWidgets.QLineEdit()
        self.input_set.setPlaceholderText("SP (THz)")
        self.input_set.setFixedHeight(22)
        self.input_set.setFixedWidth(88)
        self.btn_set = QtWidgets.QPushButton("Set F")
        self.btn_set.setFixedHeight(22)
        self.btn_set.clicked.connect(self._on_setpoint)

        self.input_volt = QtWidgets.QLineEdit()
        self.input_volt.setPlaceholderText("mV")
        self.input_volt.setFixedHeight(22)
        self.input_volt.setFixedWidth(48)
        self.btn_volt = QtWidgets.QPushButton("Set V")
        self.btn_volt.setFixedHeight(22)
        self.btn_volt.clicked.connect(self._on_voltage)

        self.lbl_exp = QtWidgets.QLabel("Exp: N/A")
        self.lbl_exp.setFixedHeight(22)

        # CCD bars (compact)
        self.bar_amp1 = QtWidgets.QProgressBar()
        self.bar_amp1.setRange(0, 5000)
        self.bar_amp1.setTextVisible(False)
        self.bar_amp1.setFixedHeight(12)
        self.bar_amp1.setMaximumWidth(40)
        self.bar_amp2 = QtWidgets.QProgressBar()
        self.bar_amp2.setRange(0, 5000)
        self.bar_amp2.setTextVisible(False)
        self.bar_amp2.setFixedHeight(12)
        self.bar_amp2.setMaximumWidth(40)

        row2.addWidget(self.chk_use)
        row2.addWidget(self.chk_show)
        row2.addWidget(self.chk_incl_sp)
        row2.addWidget(self.input_set)
        row2.addWidget(self.btn_set)
        row2.addWidget(self.input_volt)
        row2.addWidget(self.btn_volt)
        row2.addStretch(1)
        row2.addWidget(self.lbl_exp)
        row2.addWidget(self.bar_amp1)
        row2.addWidget(self.bar_amp2)
        layout.addLayout(row2)

        # -- Plots (take all remaining vertical space) --
        # Frequency offset plot (MHz offset from reference)
        self.plot_freq = pg.PlotWidget(
            title="Freq offset (MHz)",
            axisItems={"bottom": ElapsedAxisItem(orientation="bottom")},
        )
        self.plot_freq.setMinimumHeight(90)
        self.plot_freq.getPlotItem().titleLabel.setMaximumHeight(16)
        self.plot_freq.enableAutoRange(axis="y", enable=False)
        self.plot_freq.getAxis("left").enableAutoSIPrefix(False)
        self.curve_freq = self.plot_freq.plot()
        # Setpoint & tolerance lines in MHz offset units
        self.line_setpoint = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('g', style=QtCore.Qt.DashLine))
        self.line_tol_up = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.line_tol_dn = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.plot_freq.addItem(self.line_setpoint)
        self.plot_freq.addItem(self.line_tol_up)
        self.plot_freq.addItem(self.line_tol_dn)

        # Voltage plot (with stable elapsed-time x-axis; Y autoscaled to data)
        self.plot_volt = pg.PlotWidget(
            title="Deviation (mV)",
            axisItems={"bottom": ElapsedAxisItem(orientation="bottom")},
        )
        self.plot_volt.setMinimumHeight(90)
        self.plot_volt.getPlotItem().titleLabel.setMaximumHeight(16)
        self.plot_volt.enableAutoRange(axis="y", enable=False)
        self.curve_volt = self.plot_volt.plot()
        self.line_bound_min = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.line_bound_max = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('r', style=QtCore.Qt.DashLine))
        self.plot_volt.addItem(self.line_bound_min)
        self.plot_volt.addItem(self.line_bound_max)

        layout.addWidget(self.plot_freq, 1)
        layout.addWidget(self.plot_volt, 1)

    # ----- helpers -----
    def _thz_to_mhz_offset(self, freq_thz):
        """Convert an absolute THz frequency to MHz offset from _freq_ref."""
        return (freq_thz - self._freq_ref) * 1.0e6

    def _update_freq_ref(self, setpoint_thz):
        """
        Recompute the plot reference.
          - "Ref" channels: exact setpoint (plot shows deviation from target)
          - Other channels: setpoint rounded to nearest GHz
        Clears plot buffers when the reference changes to avoid jumps.
        """
        if self._use_exact_ref:
            new_ref = setpoint_thz
        else:
            # 1 GHz = 0.001 THz
            new_ref = round(setpoint_thz / 0.001) * 0.001

        if new_ref != self._freq_ref:
            self._freq_ref = new_ref
            self.t.clear()
            self.f.clear()
            self.v.clear()

        # Update title
        if self._use_exact_ref:
            self.plot_freq.setTitle(f"Freq offset from SP {self._freq_ref:.6f} THz (MHz)")
        else:
            self.plot_freq.setTitle(f"Freq offset from {self._freq_ref:.3f} THz (MHz)")

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

        # Frequency plot: convert to MHz offset, gap on invalid / missing f_plot
        if (not valid) or (f_plot is None):
            fplot_mhz = float("nan")
        else:
            fplot_mhz = self._thz_to_mhz_offset(float(f_plot))

        self.t.append(now)
        self.f.append(fplot_mhz)
        self.v.append(vval)

        self.curve_freq.setData(list(self.t), list(self.f))
        self.curve_volt.setData(list(self.t), list(self.v))

        # --- Autoscale frequency offset Y-axis ---
        if len(self.f) > 0:
            farr = np.array(self.f)
            finite = farr[np.isfinite(farr)]
            if len(finite) > 0:
                fmin, fmax = float(finite.min()), float(finite.max())

                # Optionally include setpoint position in the range
                if self.chk_incl_sp.isChecked():
                    fmin = min(fmin, self._sp_mhz)
                    fmax = max(fmax, self._sp_mhz)

                ylo, yhi, step = _nice_y_range(fmin, fmax, min_span=FREQ_MIN_RANGE)
                self.plot_freq.setYRange(ylo, yhi, padding=0)
                self.plot_freq.getAxis("left").setTickSpacing(step, step / 5.0)

        # --- Autoscale voltage Y-axis ---
        if len(self.v) > 0:
            varr = np.array(self.v)
            finite = varr[np.isfinite(varr)]
            if len(finite) > 0:
                vmin, vmax = float(finite.min()), float(finite.max())
                span = vmax - vmin
                if span < VOLT_MIN_RANGE:
                    mid = (vmin + vmax) / 2.0
                    vmin = mid - VOLT_MIN_RANGE / 2.0
                    vmax = mid + VOLT_MIN_RANGE / 2.0
                    span = VOLT_MIN_RANGE
                pad = span * VOLT_PAD_FRAC
                self.plot_volt.setYRange(vmin - pad, vmax + pad, padding=0)

        e1, e2 = meas.get("exp", (0.0, 0.0))
        self.lbl_exp.setText(f"Exp: {float(e1):.0f}+{float(e2):.0f} ms")

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
            if f_disp is None or (isinstance(fplot_mhz, float) and math.isnan(fplot_mhz)):
                ftxt = "N/A"
            else:
                ftxt = f"{float(f_disp):.6f}"

        self.status_label.setText(f"<b>{self.name}: {ftxt} THz \u2014 {tag}</b>")

    def update_slow(self, status: dict):
        """
        status keys from workers.py (full snapshot or deltas):
          - setpoint, use, show, bound_min, bound_max, lock_enabled
        """
        if "setpoint" in status:
            sp = float(status.get("setpoint", 0.0))
            self._setpoint = sp
            self._update_freq_ref(sp)

            # Position setpoint + tolerance lines in MHz offset units
            self._sp_mhz = self._thz_to_mhz_offset(sp)
            tol_mhz = LOCK_TOL * 1.0e6  # 5 MHz for 0.000005 THz
            self.line_setpoint.setPos(self._sp_mhz)
            self.line_tol_up.setPos(self._sp_mhz + tol_mhz)
            self.line_tol_dn.setPos(self._sp_mhz - tol_mhz)

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
            # Preserve font styling while updating color
            self.lock_btn.setStyleSheet(
                f"font-size: 11pt; font-weight: bold; "
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
        layout.setContentsMargins(4, 2, 4, 2)

        self.lbl_wlm = QtWidgets.QLabel("")
        self.lbl_temp = QtWidgets.QLabel("T: N/A")
        self.lbl_press = QtWidgets.QLabel("P: N/A")

        self.btn_auto = QtWidgets.QPushButton("Autocal OFF")
        self.btn_auto.setCheckable(True)
        self.btn_auto.setMinimumWidth(120)
        self.btn_auto.clicked.connect(lambda: self.request_autocal.emit(self.btn_auto.isChecked()))

        self.btn_dev = QtWidgets.QPushButton("Deviation OFF")
        self.btn_dev.setCheckable(True)
        self.btn_dev.setMinimumWidth(120)
        self.btn_dev.clicked.connect(lambda: self.request_deviation.emit(self.btn_dev.isChecked()))

        layout.addWidget(self.lbl_wlm)
        layout.addWidget(self.lbl_temp)
        layout.addWidget(self.lbl_press)
        layout.addStretch(1)
        layout.addWidget(self.btn_auto)
        layout.addWidget(self.btn_dev)

    def update_globals(self, g: dict):
        # g may be full snapshot or delta; controller should merge before calling
        wlm_active = bool(g.get("wlm_active", True))
        if wlm_active:
            self.lbl_wlm.setText("<b style='color:#27ae60'>WLM Online</b>")
        else:
            self.lbl_wlm.setText("<b style='color:#c0392b'>WLM Offline</b>")

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
