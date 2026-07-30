# display.py
from PyQt5 import QtWidgets, QtCore
import pyqtgraph as pg
from collections import deque
import math
import time
import numpy as np  # kept for display_wide compatibility; not used in hot paths

from workers import lock_tolerance  # per-channel lock tolerance (THz)

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

        # Dirty flags: True while the user has typed an unsubmitted edit in
        # the box. The hasFocus()/pending-guard pair does NOT cover the
        # press->release window of the Set F/Set mV button itself: a click
        # is two event-loop iterations, focus leaves the box on *press*,
        # but the pending guard is only armed on *release* (in _on_setpoint).
        # A slow-poll QTimer firing in that gap would clobber the typed
        # value with the stale cached one before _on_setpoint reads it.
        # textEdited fires only on user edits (not programmatic setText),
        # so this flag is the real "unsubmitted edit" invariant.
        self._setpoint_dirty = False
        self._voltage_dirty = False

        # Plot buffers — raw elapsed stored, rendered via cycle-shift + clipToView.
        self._t0 = time.perf_counter()
        self._sweep_s = 60.0
        self._x_window = 6.0          # visible time window (seconds)
        self.t = deque(maxlen=1200)
        self.f = deque(maxlen=1200)    # stores MHz offset from _freq_ref
        self.v = deque(maxlen=1200)

        # Widget update caches — avoid redundant setText/setValue calls
        self._last_exp_text = ""
        self._last_status_text = ""
        self._last_amp1 = -1
        self._last_amp2 = -1
        self._last_freq_title = ""

        # Autoscale range caches — avoid redundant setYRange/setTickSpacing calls
        self._prev_freq_yrange = (None, None, None)
        self._prev_volt_yrange = (None, None)

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

        # Time window dropdown in row1 (between status and lock button)
        self.cmb_xwin = QtWidgets.QComboBox()
        self.cmb_xwin.addItems(["6s", "12s", "30s", "60s"])
        self.cmb_xwin.setFixedWidth(50)
        self.cmb_xwin.setFixedHeight(22)
        self.cmb_xwin.setToolTip("Visible time window")
        self.cmb_xwin.currentTextChanged.connect(self._on_xwin_changed)

        row1.addWidget(self.status_label, 3)
        row1.addWidget(self.cmb_xwin)
        row1.addWidget(self.lock_btn)
        self.lock_btn.setFixedWidth(160)
        layout.addLayout(row1)

        # -- Row 2: checkboxes | [setpoint] Set F | [mV] Set V | Exp | CCD bars --
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(2)

        self.chk_use = QtWidgets.QCheckBox("Use")
        self.chk_show = QtWidgets.QCheckBox("Show")
        self.chk_use.clicked.connect(self._on_switcher)
        self.chk_show.clicked.connect(self._on_switcher)

        self.chk_auto_y = QtWidgets.QCheckBox("Auto Y")
        self.chk_auto_y.setChecked(True)
        self.chk_auto_y.setToolTip("Auto-scale frequency plot Y-axis to data range")
        self.chk_auto_y.toggled.connect(self._on_auto_y_toggled)

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
        self.input_set.textEdited.connect(self._on_setpoint_edited)

        self.input_volt = QtWidgets.QLineEdit()
        self.input_volt.setPlaceholderText("mV")
        self.input_volt.setFixedHeight(22)
        self.input_volt.setFixedWidth(48)
        self.btn_volt = QtWidgets.QPushButton("Set mV")
        self.btn_volt.setFixedHeight(22)
        self.btn_volt.clicked.connect(self._on_voltage)
        self.input_volt.textEdited.connect(self._on_voltage_edited)

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
        row2.addWidget(self.chk_auto_y)
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
        self.plot_freq.enableAutoRange(axis="x", enable=False)
        self.plot_freq.setMouseEnabled(x=False)
        self.plot_freq.setClipToView(True)
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
        self.plot_volt.enableAutoRange(axis="x", enable=False)
        self.plot_volt.setMouseEnabled(x=False)
        self.plot_volt.setClipToView(True)
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

    def _on_auto_y_toggled(self, checked):
        """Enable/disable frequency plot Y-axis autoranging."""
        self.chk_incl_sp.setEnabled(checked)
        if not checked:
            # Allow manual zoom/pan via mouse
            self.plot_freq.enableAutoRange(axis="y", enable=False)
            self._prev_freq_yrange = (None, None, None)
        # When re-enabled, next update_fast() will recompute and snap to range

    def _on_xwin_changed(self, text):
        """Update visible time window from dropdown."""
        self._x_window = float(text.rstrip("s"))

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

        # Update title (guarded)
        if self._use_exact_ref:
            title = f"Freq offset from SP {self._freq_ref:.6f} THz (MHz)"
        else:
            title = f"Freq offset from {self._freq_ref:.3f} THz (MHz)"
        if title != self._last_freq_title:
            self._last_freq_title = title
            self.plot_freq.setTitle(title)

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
        if not self.chk_use.isChecked():
            return

        elapsed = time.perf_counter() - self._t0

        # Purge data older than one sweep cycle
        cutoff = elapsed - self._sweep_s
        while self.t and self.t[0] < cutoff:
            self.t.popleft()
            self.f.popleft()
            self.v.popleft()

        valid = bool(meas.get("valid", False))
        f_plot = meas.get("freq_plot", None)       # None => gap
        f_disp = meas.get("freq_display", None)    # for text readout
        vval = float(meas.get("volt", 0.0))

        # Frequency plot: convert to MHz offset, gap on invalid / missing f_plot
        if (not valid) or (f_plot is None):
            fplot_mhz = float("nan")
        else:
            fplot_mhz = (float(f_plot) - self._freq_ref) * 1.0e6

        self.t.append(elapsed)
        self.f.append(fplot_mhz)
        self.v.append(vval)

        # Render: modular time with old-cycle points shifted by -sweep
        # so they appear left of 0 during the wrap. clipToView handles the rest.
        t_raw = np.array(self.t)
        cycle = elapsed // self._sweep_s
        t_arr = t_raw % self._sweep_s
        t_arr[(t_raw // self._sweep_s) < cycle] -= self._sweep_s
        self.curve_freq.setData(t_arr, np.array(self.f))
        self.curve_volt.setData(t_arr, np.array(self.v))

        # Scrolling x-axis: trace tip at right edge
        t_mod = elapsed % self._sweep_s
        x_right = t_mod + 0.5
        x_left = t_mod - self._x_window
        self.plot_freq.setXRange(x_left, x_right, padding=0)
        self.plot_volt.setXRange(x_left, x_right, padding=0)

        # --- Autoscale Y-axes using only visible points ---
        vis = (t_arr >= x_left) & (t_arr <= x_right)
        f_arr = np.array(self.f)
        v_arr = np.array(self.v)

        if self.chk_auto_y.isChecked() and vis.any():
            f_vis = f_arr[vis]
            f_finite = f_vis[np.isfinite(f_vis)]
            if len(f_finite) > 0:
                fmin = float(f_finite.min())
                fmax = float(f_finite.max())
                if self.chk_incl_sp.isChecked():
                    fmin = min(fmin, self._sp_mhz)
                    fmax = max(fmax, self._sp_mhz)

                ylo, yhi, step = _nice_y_range(fmin, fmax, min_span=FREQ_MIN_RANGE)
                if (ylo, yhi, step) != self._prev_freq_yrange:
                    self._prev_freq_yrange = (ylo, yhi, step)
                    self.plot_freq.setYRange(ylo, yhi, padding=0)
                    self.plot_freq.getAxis("left").setTickSpacing(step, step / 5.0)

        if vis.any():
            v_vis = v_arr[vis]
            v_finite = v_vis[np.isfinite(v_vis)]
            if len(v_finite) > 0:
                vmin = float(v_finite.min())
                vmax = float(v_finite.max())
            if vmin <= vmax:  # at least one finite value
                span = vmax - vmin
                if span < VOLT_MIN_RANGE:
                    mid = (vmin + vmax) / 2.0
                    vmin = mid - VOLT_MIN_RANGE / 2.0
                    vmax = mid + VOLT_MIN_RANGE / 2.0
                    span = VOLT_MIN_RANGE
                pad = span * VOLT_PAD_FRAC
                new_vrange = (vmin - pad, vmax + pad)
                if new_vrange != self._prev_volt_yrange:
                    self._prev_volt_yrange = new_vrange
                    self.plot_volt.setYRange(new_vrange[0], new_vrange[1], padding=0)

        # --- Exposure label (guarded) ---
        e1, e2 = meas.get("exp", (0.0, 0.0))
        exp_text = f"Exp: {float(e1):.0f}+{float(e2):.0f} ms"
        if exp_text != self._last_exp_text:
            self._last_exp_text = exp_text
            self.lbl_exp.setText(exp_text)

        # --- Amplitude bars (guarded) ---
        a1, a2 = meas.get("amp", (0.0, 0.0))
        ia1 = int(a1)
        ia2 = int(a2)
        if ia1 != self._last_amp1:
            self._last_amp1 = ia1
            self.bar_amp1.setValue(ia1)
        if ia2 != self._last_amp2:
            self._last_amp2 = ia2
            self.bar_amp2.setValue(ia2)

        # Derived lock_status (arming state + global deviation mode + within tolerance)
        if valid and (f_disp is not None):
            in_tol = abs(float(f_disp) - float(self._setpoint)) < lock_tolerance(self.port)
            locked = bool(self._lock_enabled and self._global_deviation_mode and in_tol)
        else:
            locked = False

        # Status text (guarded)
        if not valid:
            tag = "<span style='color:#7f8c8d'>NO SIGNAL</span>"
            ftxt = "N/A"
        else:
            tag = "<span style='color:#27ae60'>Locked</span>" if locked else "<span style='color:#e67e22'>Unlocked</span>"
            if f_disp is None or (isinstance(fplot_mhz, float) and math.isnan(fplot_mhz)):
                ftxt = "N/A"
            else:
                ftxt = f"{float(f_disp):.6f}"

        status_text = f"<b>{self.name}: {ftxt} THz \u2014 {tag}</b>"
        if status_text != self._last_status_text:
            self._last_status_text = status_text
            self.status_label.setText(status_text)

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
            tol_mhz = lock_tolerance(self.port) * 1.0e6  # THz -> MHz
            self.line_setpoint.setPos(self._sp_mhz)
            self.line_tol_up.setPos(self._sp_mhz + tol_mhz)
            self.line_tol_dn.setPos(self._sp_mhz - tol_mhz)

            if (not self.input_set.hasFocus()
                    and not self._setpoint_dirty
                    and time.perf_counter() > self._setpoint_pending_until):
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
    def _on_setpoint_edited(self, text):
        # User typed in the setpoint box: suppress pull-refresh clobber
        # until they submit (Set F) or clear the box. An empty box is not
        # dirty, so clearing it resumes auto-refresh from the live value.
        self._setpoint_dirty = bool(text.strip())

    def _on_voltage_edited(self, text):
        # Defensive twin of _on_setpoint_edited. Currently latent: nothing
        # auto-refreshes input_volt, so the guard never fires today — but it
        # makes the voltage box safe-by-construction if a pull-refresh is
        # ever added to update_slow (mirrors the setpoint mechanism above).
        self._voltage_dirty = bool(text.strip())

    def _on_setpoint(self):
        try:
            val = float(self.input_set.text())
            self._setpoint_dirty = False
            self._setpoint_pending_until = time.perf_counter() + self._PENDING_GUARD_S
            self.request_setpoint.emit(self.port, val)
        except Exception:
            pass

    def _on_voltage(self):
        try:
            val = float(self.input_volt.text())
            self._voltage_dirty = False
            self._voltage_pending_until = time.perf_counter() + self._PENDING_GUARD_S
            self.request_voltage.emit(self.port, val)
        except Exception:
            pass

    def _on_lock_toggled(self):
        self.request_lock.emit(self.port, self.lock_btn.isChecked())

    def _on_switcher(self):
        if not self.chk_use.isChecked():
            # Auto-disable lock when channel is taken out of the cycle
            if self.lock_btn.isChecked():
                self.lock_btn.blockSignals(True)
                self.lock_btn.setChecked(False)
                self.lock_btn.blockSignals(False)
                self.request_lock.emit(self.port, False)
            self.t.clear()
            self.f.clear()
            self.v.clear()
            self.curve_freq.setData([], [])
            self.curve_volt.setData([], [])
            # Reset readouts so a Use=off channel doesn't show a stale "Locked" badge
            self.lbl_exp.setText("Exp: --")
            self.bar_amp1.setValue(0)
            self.bar_amp2.setValue(0)
            self.status_label.setText(
                f"<b>{self.name}: <span style='color:#7f8c8d'>INACTIVE</span></b>"
            )
            # Clear unsubmitted-edit guards: a dirtied box on a disabled
            # channel must not stay frozen forever — re-enable refreshes it.
            self._setpoint_dirty = False
            self._voltage_dirty = False
            # Invalidate guarded-update caches so re-enable triggers fresh setText
            self._last_exp_text = None
            self._last_amp1 = -1
            self._last_amp2 = -1
            self._last_status_text = None
            self._prev_freq_yrange = None
            self._prev_volt_yrange = None
        self.request_switcher.emit(self.port, self.chk_use.isChecked(), self.chk_show.isChecked())


class GlobalControl(QtWidgets.QWidget):
    request_autocal = QtCore.pyqtSignal(bool)
    request_deviation = QtCore.pyqtSignal(bool)
    request_save_config = QtCore.pyqtSignal()
    request_backup_wlm = QtCore.pyqtSignal()

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

        self.btn_save_config = QtWidgets.QPushButton("Save Config")
        self.btn_save_config.setMinimumWidth(100)
        self.btn_save_config.setToolTip("Save current PID settings to pid_config.json")
        self.btn_save_config.clicked.connect(lambda: self.request_save_config.emit())

        self.btn_backup_wlm = QtWidgets.QPushButton("Backup WLM")
        self.btn_backup_wlm.setMinimumWidth(100)
        self.btn_backup_wlm.setToolTip(
            "Copy WLM app config (wlm_ws7.ini, calibration, history)\n"
            "to a timestamped backup folder in wlm_backups/"
        )
        self.btn_backup_wlm.clicked.connect(lambda: self.request_backup_wlm.emit())

        layout.addWidget(self.lbl_wlm)
        layout.addWidget(self.lbl_temp)
        layout.addWidget(self.lbl_press)
        layout.addStretch(1)
        layout.addWidget(self.btn_backup_wlm)
        layout.addWidget(self.btn_save_config)
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
