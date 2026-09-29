"""Integration of matisse_cd into HF_Locking -- written test-first.

These pin the contracts between the optional CounterDrift module and the
existing HF_Locking pieces (SharedExperimentState, WavemeterWorker,
ChannelControl, the wiring done in main_wlm.py). Real production objects
are used wherever possible; only the DLL (autospec stub) and the Matisse
socket (FakeMatisseClient) are faked. Queued signals are delivered with
QCoreApplication.processEvents() -- no event loop thread, no ZMQ binds.

  I1  SharedExperimentState status carries cd_active (default False)
  I2  WavemeterWorker warns once when the WS7 lock is re-armed outside
      HF_Locking while CounterDrift drives the same channel
  I3  ChannelControl: CD owns channel -> lock button disabled, "Locked (CD)"
  I5  matisse_cd.wire_into_hf connects both directions with QUEUED delivery
  I6  end-to-end: local Set F and ZMQ setpoints reach CounterDrift in nm;
      activating CD turns a running WS7 lock off through the real worker;
      the HF lock cannot be re-armed while CD is active
The ZMQ wait_for_lock gate (I4) lives in test_zmq_v2_protocol.py (needs zmq_v2).
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.conftest import (  # noqa: E402
    require_workers, make_wlm_stub, FakeMatisseClient,
)

F_TISA = 375.0
NM_TISA = 299792.458 / F_TISA


@pytest.fixture(scope="module")
def qapp():
    QtWidgets = pytest.importorskip("PyQt5.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _process(qapp, n=5):
    for _ in range(n):
        qapp.processEvents()


# ------------------------------------------------------------------ I1

def test_I1_shared_state_status_has_cd_active_default_false():
    w, _ = require_workers()
    st = w.SharedExperimentState()
    for p in w.PORTS:
        assert st.get_status(p)["cd_active"] is False
    assert all(s["cd_active"] is False for s in st.get_gui_snapshot()["status"].values())


# ------------------------------------------------------------------ I2

def test_I2_external_ws7_lock_with_cd_active_warns_once():
    w, _ = require_workers()
    wlm = make_wlm_stub()
    wlm.get_switcher_signal.return_value = (1, 1)
    wlm.get_pid_course_num.return_value = F_TISA
    wlm.get_deviation_bounds.return_value = (-1.0, 1.0)
    wlm.get_channel_assignment.return_value = True     # re-armed in WLM app
    worker = w.WavemeterWorker(wlm, w.SharedExperimentState())
    worker.state.update_status(1, {"cd_active": True})
    logs = []
    worker.log_message.connect(logs.append)

    worker._emit_full_status_for_port(1)
    worker._emit_full_status_for_port(1)
    fights = [m for m in logs if "fighting" in m]
    assert len(fights) == 1, logs

    wlm.get_channel_assignment.return_value = False    # resolved -> re-arms warning
    worker._emit_full_status_for_port(1)
    wlm.get_channel_assignment.return_value = True
    worker._emit_full_status_for_port(1)
    assert len([m for m in logs if "fighting" in m]) == 2
    # cd_active must survive the full-status refresh (not in s_full)
    assert worker.state.get_status(1)["cd_active"] is True


# ------------------------------------------------------------------ I3

def test_I3_channel_control_shows_cd_ownership(qapp):
    require_workers()
    pytest.importorskip("pyqtgraph")
    import display  # noqa: PLC0415

    ch = display.ChannelControl(1, "TiSa_1")
    ch.chk_use.setChecked(True)
    ch.set_globals({"deviation_mode": False})     # CD does not need WS7 deviation mode
    ch.update_slow({"setpoint": F_TISA, "lock_enabled": False, "cd_active": True})
    assert not ch.lock_btn.isEnabled()
    assert "CD" in ch.lock_btn.text()

    ch.update_fast({"valid": True, "freq_plot": F_TISA + 1e-7, "freq_display": F_TISA + 1e-7,
                    "volt": 0.0, "exp": (1, 1), "amp": (0, 0)})
    assert "Locked (CD)" in ch.status_label.text()

    # a later lock_enabled-only delta must not forget CD ownership
    ch.update_slow({"lock_enabled": False})
    assert not ch.lock_btn.isEnabled()

    ch.update_slow({"cd_active": False})
    assert ch.lock_btn.isEnabled()
    assert ch.lock_btn.text() == "Enable Lock"


# ------------------------------------------------------------------ I5/I6 harness

def _build_hf(qapp, panel=None):
    """Real SharedExperimentState + WavemeterWorker + MatisseCDWorker (fake
    socket), wired exactly as main_wlm.py does, all on this thread."""
    w, _ = require_workers()
    import matisse_cd  # noqa: PLC0415

    shared = w.SharedExperimentState()
    wlm = make_wlm_stub()
    ws7 = {"sp": {p: 0.0 for p in w.PORTS}, "lock": {p: False for p in w.PORTS}}
    wlm.set_pid_course_num.side_effect = lambda port, v: ws7["sp"].__setitem__(port, v)
    wlm.get_pid_course_num.side_effect = lambda port: ws7["sp"][port]
    wlm.set_channel_assignment.side_effect = lambda port, en: ws7["lock"].__setitem__(port, bool(en))
    wlm_worker = w.WavemeterWorker(wlm, shared)

    cfg = matisse_cd.load_config(path=os.devnull + ".none")
    clients = {}

    def factory(host, port, **kw):
        clients[port] = FakeMatisseClient(host, port, **kw)
        return clients[port]

    cd = matisse_cd.MatisseCDWorker(shared, cfg, client_factory=factory, save_fn=lambda c: None)
    matisse_cd.wire_into_hf(wlm_worker, cd, panel=panel)

    # seed: TiSa_1 on ch1 at F_TISA, measured on target
    wlm_worker.handle_setpoint_write(1, F_TISA)
    shared.update_measurement(1, {"valid": True, "freq_display": F_TISA, "freq_raw": F_TISA})
    _process(qapp)
    return dict(w=w, shared=shared, wlm=wlm, ws7=ws7, wlm_worker=wlm_worker,
                cd=cd, clients=clients, qapp=qapp)


@pytest.fixture
def hf_with_cd(qapp):
    return _build_hf(qapp)


# ------------------------------------------------------------------ I5

def test_I5_wire_into_hf_is_queued_both_directions(hf_with_cd):
    h = hf_with_cd
    cd, wlm_worker, qapp = h["cd"], h["wlm_worker"], h["qapp"]
    h["cd"].handle_activate("TiSa_1", True)
    c = h["clients"][30000]
    c.calls.clear()

    wlm_worker.setpoint_committed.emit(1, 375.001)
    assert c.calls == [], "setpoint_committed must be QUEUED (worker threads differ in prod)"
    _process(qapp)
    assert c.calls and c.calls[-1][0] == "setpoint"

    h["ws7"]["lock"][1] = True
    h["shared"].update_status(1, {"cd_active": False})     # allow the toggle through
    cd.request_hf_lock.emit(1, False)
    assert h["ws7"]["lock"][1] is True, "request_hf_lock must be QUEUED"
    _process(qapp)
    assert h["ws7"]["lock"][1] is False


def test_I5_wire_into_hf_connects_panel(qapp):
    require_workers()
    import matisse_cd  # noqa: PLC0415
    panel = matisse_cd.MatisseCDPanel(["TiSa_1", "TiSa-2"])
    h = _build_hf(qapp, panel=panel)

    panel.request_channel.emit("TiSa_1", 3)
    panel.request_activate.emit("TiSa-2", False)
    panel.request_connect.emit("TiSa-2", False)
    _process(qapp)
    snap = h["cd"].get_snapshot()
    assert snap["TiSa_1"]["wlm_port"] == 3
    assert snap["TiSa-2"]["want_connected"] is False


# ------------------------------------------------------------------ I6

def test_I6_local_set_f_reaches_counterdrift_in_nm(hf_with_cd):
    h = hf_with_cd
    h["cd"].handle_activate("TiSa_1", True)
    c = h["clients"][30000]
    c.calls.clear()

    h["wlm_worker"].handle_setpoint_write(1, 375.002)        # what Set F triggers
    _process(h["qapp"])

    assert h["ws7"]["sp"][1] == 375.002                      # WS7 course still written
    assert h["shared"].get_status(1)["setpoint"] == 375.002  # CHECK_VALUE source
    assert c.calls == [("setpoint", "%.8f" % (299792.458 / 375.002))]


def test_I6_zmq_setpoint_reaches_counterdrift(hf_with_cd):
    h = hf_with_cd
    from PyQt5 import QtCore  # noqa: PLC0415
    zmq_rep = h["w"].ZMQRepWorker(h["shared"])               # not started: no bind
    zmq_rep.request_setpoint_write.connect(h["wlm_worker"].handle_setpoint_write,
                                           QtCore.Qt.QueuedConnection)   # as main_wlm.py
    h["cd"].handle_activate("TiSa_1", True)
    c = h["clients"][30000]
    c.calls.clear()

    zmq_rep.request_setpoint_write.emit(1, 374.999)
    _process(h["qapp"])
    assert c.calls == [("setpoint", "%.8f" % (299792.458 / 374.999))]


def test_I6_activation_turns_running_ws7_lock_off_via_real_worker(hf_with_cd):
    h = hf_with_cd
    h["wlm_worker"].handle_lock_toggle(1, True)
    assert h["ws7"]["lock"][1] is True

    # Activation blocks on the lock-off (normally across threads). Here the
    # queued toggle must be delivered while _activate waits, so pump events
    # from inside the wait loop's sleep.
    import matisse_cd  # noqa: PLC0415
    real_sleep = matisse_cd.time.sleep
    with mock.patch.object(matisse_cd.time, "sleep",
                           side_effect=lambda s: (h["qapp"].processEvents(), real_sleep(0))):
        h["cd"].handle_activate("TiSa_1", True)

    assert h["ws7"]["lock"][1] is False
    assert h["shared"].get_status(1)["cd_active"] is True
    assert h["clients"][30000].calls[-1] == ("activate", True)


def test_I6_hf_lock_cannot_be_rearmed_while_cd_active(hf_with_cd):
    h = hf_with_cd
    h["cd"].handle_activate("TiSa_1", True)
    h["wlm_worker"].handle_lock_toggle(1, True)               # Enable Lock click
    assert h["ws7"]["lock"][1] is False
    assert h["shared"].get_status(1)["lock_enabled"] is False

    h["cd"].handle_activate("TiSa_1", False)
    h["wlm_worker"].handle_lock_toggle(1, True)
    assert h["ws7"]["lock"][1] is True
