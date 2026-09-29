"""Matisse CounterDrift offload (matisse_cd.py) -- no hardware, no Qt loop.

Needs only PyQt5 (not zmq_v2): matisse_cd deliberately does not import
workers.py. The workers.py side (lock-toggle guard, wait_for_lock gate) is
covered at the bottom and skips like the rest of the suite without zmq_v2.

Pins:
  C1  wire framing: 4-byte BE length prefix, "#SERVER " prefix, prompt strip
  C2  error replies raise MatisseError; transport failure retries once
  C3  THz -> vacuum nm conversion + setpoint string formatting
  C4  activation interlocks: unassigned channel, no setpoint, air/vacuum
      or wrong-channel wavelength mismatch -> refused, nothing sent
  C5  activation: HF lock switched off first, then open/setpoint/activate
  C6  setpoints forwarded only to the active CD on that port
  C7  runaway watchdog deactivates after runaway_s
  C8  channel reassignment refused while active / onto a taken channel
"""
from __future__ import annotations

import socket
import struct
import threading
from unittest import mock

import pytest

matisse_cd = pytest.importorskip("matisse_cd", reason="needs PyQt5")

from tests.conftest import require_workers, make_wavemeter_worker_self  # noqa: E402

F_TISA = 375.000000          # THz
NM_TISA = matisse_cd.C_NM_THZ / F_TISA


# ------------------------------------------------------------ fakes

class FakeState:
    """Duck-typed subset of SharedExperimentState used by MatisseCDWorker."""

    def __init__(self):
        self.status = {p: {"setpoint": 0.0, "lock_enabled": False, "cd_active": False}
                       for p in range(1, 9)}
        self.meas = {p: {"valid": False, "freq_display": None} for p in range(1, 9)}

    def get_status(self, port):
        return dict(self.status[port])

    def update_status(self, port, delta):
        self.status[port].update(delta)

    def get_measurement(self, port):
        return dict(self.meas[port])


class FakeClient:
    def __init__(self, host, port, **_kw):
        self.connected = False
        self.calls = []
        self.wavelength = NM_TISA
        self.fail_connect = False

    def connect(self):
        if self.fail_connect:
            raise ConnectionRefusedError("refused")
        self.connected = True

    def close(self, graceful=True):
        self.connected = False

    def cd_open(self):
        self.calls.append(("open",))

    def cd_setpoint_nm(self, s):
        self.calls.append(("setpoint", s))

    def cd_activate(self, on):
        self.calls.append(("activate", on))

    def get_wavelength_nm(self):
        return self.wavelength


def make_worker(port_tisa1=1, sp=F_TISA, f_meas=F_TISA, lock_enabled=False):
    cfg = matisse_cd.load_config(path="/nonexistent/matisse_cd_config.json")
    cfg["lasers"]["TiSa_1"]["wlm_port"] = port_tisa1
    st = FakeState()
    st.status[port_tisa1].update(setpoint=sp, lock_enabled=lock_enabled)
    st.meas[port_tisa1].update(valid=True, freq_display=f_meas)
    clients = {}

    def factory(host, port, **kw):
        c = FakeClient(host, port, **kw)
        clients[port] = c
        return c

    w = matisse_cd.MatisseCDWorker(st, cfg, client_factory=factory, save_fn=lambda cfg: None)
    w.log = []
    w.log_message.connect(w.log.append)
    return w, st, clients


# ------------------------------------------------------------ C1/C2 wire

def _serve(replies, received):
    """One-shot fake Matisse Commander Network Server. Returns (port, thread)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            for rep in replies:
                n = struct.unpack(">I", conn.recv(4))[0]
                buf = b""
                while len(buf) < n:
                    buf += conn.recv(n - len(buf))
                received.append(buf.decode())
                body = rep.encode()
                conn.sendall(struct.pack(">I", len(body)) + body)
        srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], t


def test_C1_framing_prefix_and_prompt_strip():
    received = []
    port, t = _serve(["Matisse> 794.97865 nm", "Matisse> OK"], received)
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", port)
    assert c.get_wavelength_nm() == pytest.approx(794.97865)
    c.cd_setpoint_nm("794.97865000")
    c.close(graceful=False)
    t.join(2)
    assert received == ["#SERVER MCP_WM_GET_WAVELENGTH",
                        "#SERVER MCP_WM.Counterdrift Setpoint 794.97865000"]


def test_C2_error_reply_raises():
    received = []
    port, t = _serve(['Matisse> Error: plugin not loaded', '1,"general syntax error"'], received)
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", port)
    with pytest.raises(matisse_cd.MatisseError):
        c.cd_open()
    with pytest.raises(matisse_cd.MatisseError):
        c.cd_activate(True)
    c.close(graceful=False)
    t.join(2)


def test_C2_transport_failure_retries_once_then_raises():
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", 1, connect_timeout_s=0.2)
    with mock.patch.object(c, "connect", side_effect=ConnectionRefusedError("x")) as m:
        with pytest.raises(OSError):
            c.ask("#SERVER MCP_WM_GET_WAVELENGTH")
    assert m.call_count == 2


# ------------------------------------------------------------ C3

def test_C3_conversion_and_format():
    assert matisse_cd.thz_to_nm(299792.458) == pytest.approx(1.0)
    assert matisse_cd.thz_to_nm(377.107463380) == pytest.approx(794.978851, abs=1e-6)  # Rb D1 (vac)
    assert matisse_cd.format_nm(794.9788512, 8, ".") == "794.97885120"
    assert matisse_cd.format_nm(794.9788512, 8, ",") == "794,97885120"


# ------------------------------------------------------------ C4

def test_C4_refuses_unassigned_channel():
    w, st, clients = make_worker(port_tisa1=1)
    w.handle_set_channel("TiSa_1", 0)
    w.handle_activate("TiSa_1", True)
    assert not w.get_snapshot()["TiSa_1"]["active"]
    assert all(c.calls == [] for c in clients.values())


def test_C4_refuses_without_valid_setpoint():
    w, st, clients = make_worker(sp=0.0)
    w.handle_activate("TiSa_1", True)
    assert not st.status[1]["cd_active"]
    assert clients[30000].calls == []


@pytest.mark.parametrize("lam_mc", [
    NM_TISA * 1.000273,   # MC reports AIR wavelength (~100 GHz off)
    780.241209,           # MC plugin reads a different switch channel / laser
    0.0,                  # error sentinel
])
def test_C4_refuses_on_wavelength_mismatch(lam_mc):
    w, st, clients = make_worker()
    w._client("TiSa_1").wavelength = lam_mc
    w.handle_activate("TiSa_1", True)
    assert not st.status[1]["cd_active"]
    assert clients[30000].calls == []
    assert "NOT activated" in w.log[-1]


# ------------------------------------------------------------ C5

def test_C5_activation_switches_hf_lock_off_then_commands_in_order():
    w, st, clients = make_worker(lock_enabled=True)
    seen = []

    def fake_wlm_lock_toggle(port, enabled):
        # WavemeterWorker must see cd_active already set (blocks re-arm)
        seen.append((port, enabled, st.status[port]["cd_active"]))
        st.update_status(port, {"lock_enabled": enabled})

    w.request_hf_lock.connect(fake_wlm_lock_toggle)
    w.handle_activate("TiSa_1", True)

    assert seen == [(1, False, True)]
    assert clients[30000].calls == [
        ("open",),
        ("setpoint", matisse_cd.format_nm(NM_TISA, 8, ".")),
        ("activate", True),
    ]
    assert st.status[1]["cd_active"] is True
    assert w.get_snapshot()["TiSa_1"]["active"] is True


def test_C5_hf_lock_that_never_turns_off_aborts_activation():
    w, st, clients = make_worker(lock_enabled=True)
    with mock.patch.object(matisse_cd, "HF_LOCK_OFF_WAIT_S", 0.05):
        w.handle_activate("TiSa_1", True)  # nobody listens to request_hf_lock
    assert st.status[1]["cd_active"] is False
    assert clients[30000].calls == []


# ------------------------------------------------------------ C6

def test_C6_setpoint_forwarded_only_to_active_port():
    w, st, clients = make_worker()
    w.handle_activate("TiSa_1", True)
    c = clients[30000]
    c.calls.clear()

    w.handle_setpoint_committed(6, 380.0)      # other channel: ignored
    w.handle_setpoint_committed(1, 0.5)        # bogus: ignored
    w.handle_setpoint_committed(1, 375.001)
    assert c.calls == [("setpoint", matisse_cd.format_nm(matisse_cd.C_NM_THZ / 375.001))]

    w.handle_activate("TiSa_1", False)
    assert c.calls[-1] == ("activate", False)
    assert st.status[1]["cd_active"] is False
    c.calls.clear()
    w.handle_setpoint_committed(1, 375.002)    # inactive: not forwarded
    assert c.calls == []


# ------------------------------------------------------------ C7

def test_C7_runaway_watchdog_deactivates():
    w, st, clients = make_worker()
    w.handle_activate("TiSa_1", True)
    st.meas[1]["freq_display"] = F_TISA + 0.010   # 10 GHz away, > runaway_mhz
    w.cfg["runaway_s"] = 5.0
    w._check_runaway("TiSa_1", 1, now=100.0)
    assert w.get_snapshot()["TiSa_1"]["active"]
    w._check_runaway("TiSa_1", 1, now=104.0)
    assert w.get_snapshot()["TiSa_1"]["active"]
    w._check_runaway("TiSa_1", 1, now=106.0)
    assert not w.get_snapshot()["TiSa_1"]["active"]
    assert clients[30000].calls[-1] == ("activate", False)


# ------------------------------------------------------------ C8

def test_C8_channel_reassignment_guards():
    w, st, clients = make_worker()
    w.handle_set_channel("TiSa_1", 6)          # ch6 belongs to TiSa-2 by default
    assert w.get_snapshot()["TiSa_1"]["wlm_port"] == 1
    w.handle_activate("TiSa_1", True)
    w.handle_set_channel("TiSa_1", 4)          # refused while active
    assert w.get_snapshot()["TiSa_1"]["wlm_port"] == 1
    w.handle_activate("TiSa_1", False)
    w.handle_set_channel("TiSa_1", 4)
    assert w.get_snapshot()["TiSa_1"]["wlm_port"] == 4


def test_C8_restored_active_flag_blocks_hf_lock_until_deactivated():
    cfg = matisse_cd.load_config(path="/nonexistent")
    cfg["lasers"]["TiSa_1"]["active"] = True
    st = FakeState()
    w = matisse_cd.MatisseCDWorker(st, cfg, client_factory=FakeClient, save_fn=lambda c: None)
    assert st.status[1]["cd_active"] is True
    assert w.get_snapshot()["TiSa_1"]["restored"] is True


# ------------------------------------------------------------ workers.py side

def test_W1_hf_lock_enable_rejected_while_cd_active():
    w, _ = require_workers()
    self_ = make_wavemeter_worker_self()
    self_.state.update_status(1, {"cd_active": True})
    w.WavemeterWorker.handle_lock_toggle(self_, 1, True)
    self_.wlm.set_channel_assignment.assert_not_called()
    assert self_.state.get_status(1)["lock_enabled"] is False
    # disabling is always allowed
    w.WavemeterWorker.handle_lock_toggle(self_, 1, False)
    self_.wlm.set_channel_assignment.assert_called_once_with(1, False)


def test_W2_setpoint_write_emits_committed():
    w, _ = require_workers()
    self_ = make_wavemeter_worker_self()
    self_.setpoint_committed = mock.Mock()
    self_.wlm.get_pid_course_num.return_value = 375.0
    w.WavemeterWorker.handle_setpoint_write(self_, 1, 375.0)
    self_.setpoint_committed.emit.assert_called_once_with(1, 375.0)
