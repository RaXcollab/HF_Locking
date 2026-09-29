"""Regression tests for the 2026-09-29 adversarial review of matisse_cd.

Written BEFORE the fixes (TDD): each test reproduces one verified finding.

  R1  stale lock_enabled snapshot: HF lock enabled during the wavelength
      round-trip must still be switched off before CD activates
  R2  display: CD active AND WS7 lock on -> button must stay usable to
      switch the HF lock off (not hidden behind "MATISSE CD")
  R3  failed activation restores the HF lock it switched off
  R4  activation refused when the laser is far from the WS7 setpoint
  R5  failed setpoint forward is retried by the poll and flagged meanwhile
  R6  error replies of other shapes are detected (not treated as success)
  R7  one hung Matisse Commander does not delay the other laser
  R8  shutdown with a hung Matisse Commander finishes all threads
  R9  config with two lasers on one channel is repaired on load;
      cd_active on a port is the OR over lasers
  R10 banner bytes sent on connect do not desync request/reply framing
  R11 plugin open gets a long timeout and is not retried; read-only
      wavelength poll is not retried
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time

import pytest

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

matisse_cd = pytest.importorskip("matisse_cd", reason="needs PyQt5")

from tests.conftest import (  # noqa: E402
    require_workers, FakeCDState, FakeMatisseClient,
)

F = 375.0
NM = matisse_cd.C_NM_THZ / F


@pytest.fixture(scope="module")
def qapp():
    from PyQt5 import QtWidgets  # noqa: PLC0415
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _cfg(**over):
    cfg = matisse_cd.load_config(path=os.devnull + ".none")
    cfg.update(over)
    return cfg


def _worker(cfg=None, lock_enabled=False, sp=F, f_meas=F, client_cls=FakeMatisseClient):
    cfg = cfg or _cfg()
    st = FakeCDState()
    st.status[1].update(setpoint=sp, lock_enabled=lock_enabled)
    st.meas[1].update(valid=True, freq_display=f_meas)
    clients = {}

    def factory(host, port, **kw):
        clients[port] = client_cls(host, port, **kw)
        return clients[port]

    w = matisse_cd.MatisseCDWorker(st, cfg, client_factory=factory, save_fn=lambda c: None)
    w.log = []
    w.log_message.connect(w.log.append)
    w.hf = []

    def hf(port, en):   # stands in for WavemeterWorker.handle_lock_toggle
        w.hf.append((port, en))
        if not (en and st.status[port]["cd_active"]):
            st.update_status(port, {"lock_enabled": en})

    w.request_hf_lock.connect(hf)
    return w, st, clients


# ------------------------------------------------------------------ R1

def test_R1_lock_enabled_during_wavelength_roundtrip_is_switched_off():
    w, st, clients = _worker(lock_enabled=False)
    c = w._client("TiSa_1")
    real = c.get_wavelength_nm

    def slow_readback():
        st.update_status(1, {"lock_enabled": True})   # operator clicks Enable Lock meanwhile
        return real()

    c.get_wavelength_nm = slow_readback
    w.handle_activate("TiSa_1", True)
    assert (1, False) in w.hf
    assert st.status[1]["lock_enabled"] is False
    assert st.status[1]["cd_active"] is True


def test_R1_lock_reenabled_right_after_activation_is_switched_off_again():
    w, st, clients = _worker()
    c = w._client("TiSa_1")
    real = c.cd_activate

    def activate(on):
        real(on)
        if on:
            st.status[1]["lock_enabled"] = True   # e.g. WLM app / poll_slow readback
    c.cd_activate = activate
    w.handle_activate("TiSa_1", True)
    assert w.hf[-1] == (1, False)
    assert st.status[1]["lock_enabled"] is False


# ------------------------------------------------------------------ R2

def test_R2_fight_state_keeps_hf_lock_button_usable_for_disable(qapp):
    require_workers()
    pytest.importorskip("pyqtgraph")
    import display  # noqa: PLC0415

    ch = display.ChannelControl(1, "TiSa_1")
    ch.update_slow({"setpoint": F, "lock_enabled": True, "cd_active": True})
    assert ch.lock_btn.isEnabled()
    assert "HF" in ch.lock_btn.text() and "CD" in ch.lock_btn.text()
    emitted = []
    ch.request_lock.connect(lambda p, en: emitted.append((p, en)))
    ch.lock_btn.click()
    assert emitted == [(1, False)]


# ------------------------------------------------------------------ R3

class _ActivateFails(FakeMatisseClient):
    def cd_activate(self, on):
        self.calls.append(("activate", on))
        if on:
            raise matisse_cd.MatisseError("Error: plugin not ready")


def test_R3_failed_activation_restores_hf_lock():
    w, st, clients = _worker(lock_enabled=True, client_cls=_ActivateFails)
    w.handle_activate("TiSa_1", True)
    assert w.hf == [(1, False), (1, True)]
    assert st.status[1]["lock_enabled"] is True
    assert st.status[1]["cd_active"] is False
    assert not w.get_snapshot()["TiSa_1"]["active"]


def test_R3_failed_activation_without_prior_hf_lock_leaves_it_off():
    w, st, clients = _worker(lock_enabled=False, client_cls=_ActivateFails)
    w.handle_activate("TiSa_1", True)
    assert (1, True) not in w.hf
    assert st.status[1]["lock_enabled"] is False


# ------------------------------------------------------------------ R4

def test_R4_activation_refused_far_from_setpoint():
    # laser (and MC) at F, WS7 setpoint stale by 200 GHz
    w, st, clients = _worker(sp=F + 0.200)
    w.handle_activate("TiSa_1", True)
    assert not w.get_snapshot()["TiSa_1"]["active"]
    assert clients[30000].calls == []
    assert "setpoint" in w.log[-1].lower()


def test_R4_activation_allowed_near_setpoint():
    w, st, clients = _worker(sp=F + 50e-6)     # 50 MHz
    w.handle_activate("TiSa_1", True)
    assert w.get_snapshot()["TiSa_1"]["active"]


# ------------------------------------------------------------------ R5

class _SetpointFailsOnce(FakeMatisseClient):
    fails = 1

    def cd_setpoint_nm(self, s):
        if self.fails and self.calls and self.calls[-1] == ("activate", True):
            self.fails -= 1
            raise socket.timeout("timed out")
        self.calls.append(("setpoint", s))


def test_R5_failed_setpoint_forward_is_retried_and_flagged():
    w, st, clients = _worker(client_cls=_SetpointFailsOnce)
    w.handle_activate("TiSa_1", True)
    c = clients[30000]
    w.handle_setpoint_committed(1, 375.001)
    assert w.get_snapshot()["TiSa_1"]["setpoint_pending"] is True
    assert c.calls[-1] == ("activate", True)             # not delivered yet

    w._poll()
    want = matisse_cd.format_nm(matisse_cd.C_NM_THZ / 375.001)
    assert c.calls[-1] == ("setpoint", want)
    assert w.get_snapshot()["TiSa_1"]["setpoint_pending"] is False


# ------------------------------------------------------------------ R6

def _one_reply_server(reply):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            n = struct.unpack(">I", conn.recv(4))[0]
            conn.recv(n)
            b = reply.encode()
            conn.sendall(struct.pack(">I", len(b)) + b)
            time.sleep(0.2)
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


@pytest.mark.parametrize("reply", [
    'Matisse> Error: not open',
    '2,"parameter out of range"',
    '!ERROR 4',
    ':MCP_WM.Counterdrift Activate: Error: not open',
    'Err: x',
    '1,"general syntax error"',
])
def test_R6_error_reply_shapes_raise(reply):
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", _one_reply_server(reply))
    with pytest.raises(matisse_cd.MatisseError):
        c.cd_activate(True)
    c.close(graceful=False)


@pytest.mark.parametrize("reply", ["Matisse> OK", "", "0,\"no error\"", "true"])
def test_R6_benign_replies_pass(reply):
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", _one_reply_server(reply))
    c.cd_activate(True)
    c.close(graceful=False)


# ------------------------------------------------------------------ R7 / R8

class _HangingClient(FakeMatisseClient):
    """Blocks in every call until aborted (MC accepted TCP but never replies)."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._abort = threading.Event()

    def get_wavelength_nm(self):
        self._abort.wait(10)
        raise socket.timeout("hung")

    def abort(self):
        self._abort.set()


def _group(cfg, hang_port):
    shared = FakeCDState()
    shared.status[1].update(setpoint=F)
    clients = {}

    def factory(host, port, **kw):
        cls = _HangingClient if port == hang_port else FakeMatisseClient
        clients[port] = cls(host, port, **kw)
        return clients[port]

    g = matisse_cd.MatisseCDGroup(shared, cfg, client_factory=factory, save_fn=lambda c: None)
    return g, shared, clients



def test_R7_hung_laser_does_not_delay_other_lasers_setpoints(qapp):
    from PyQt5.QtCore import QObject, pyqtSignal  # noqa: PLC0415

    class Wlm(QObject):
        setpoint_committed = pyqtSignal(int, float)

        def handle_lock_toggle(self, port, en):
            pass

    cfg = _cfg()
    cfg["lasers"]["TiSa_1"]["active"] = True          # restored-active: forwards setpoints
    g, shared, clients = _group(cfg, hang_port=30001)  # TiSa-2 MC hangs
    wlm = Wlm()
    matisse_cd.wire_into_hf(wlm, g)
    g.start()
    try:
        time.sleep(0.3)                                # TiSa-2 now blocked in its poll
        t0 = time.monotonic()
        wlm.setpoint_committed.emit(1, 375.001)
        while time.monotonic() - t0 < 2.0:
            if any(c[0] == "setpoint" for c in clients[30000].calls):
                break
            time.sleep(0.01)
        latency = time.monotonic() - t0
        assert any(c[0] == "setpoint" for c in clients[30000].calls)
        assert latency < 0.5, f"TiSa_1 setpoint delayed {latency:.2f}s by hung TiSa-2"
    finally:
        g.stop(timeout_s=1.0)


def test_R8_shutdown_with_hung_laser_finishes_all_threads(qapp):
    g, shared, clients = _group(_cfg(), hang_port=30001)
    g.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    ok = g.stop(timeout_s=1.0)
    assert ok is True
    assert time.monotonic() - t0 < 3.0
    assert all(not th.isRunning() for th in g.threads)


# ------------------------------------------------------------------ R9

def test_R9_duplicate_channels_repaired_on_load(tmp_path):
    p = tmp_path / "cd.json"
    p.write_text(json.dumps({"lasers": {"TiSa2_renamed": {"host": "127.0.0.1", "port": 30002,
                                                          "wlm_port": 6}}}))
    cfg = matisse_cd.load_config(str(p))
    ports = [lc["wlm_port"] for lc in cfg["lasers"].values() if lc["wlm_port"]]
    assert len(ports) == len(set(ports)), cfg["lasers"]


def test_R9_cd_active_is_or_over_lasers_on_port():
    """Defence in depth if a bad config slips through: an INACTIVE laser on
    the same port must not publish cd_active=False over an active one."""
    cfg = _cfg()
    cfg["lasers"]["TiSa_1"]["active"] = True
    cfg["lasers"]["TiSa-2"]["wlm_port"] = 1          # same port, inactive
    w, st, clients = _worker(cfg=cfg)
    assert st.status[1]["cd_active"] is True
    w._poll()                                        # publishes both lasers
    assert st.status[1]["cd_active"] is True


# ------------------------------------------------------------------ R10 / R11

def _scripted_server(script, received, banner=b""):
    """script: list of (reply_text, delay_s), consumed across ALL connections
    (so a client retry on a fresh connection is visible). Records requests."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    it = iter(script)

    def handle(conn):
        with conn:
            if banner:
                conn.sendall(banner)
            while True:
                hdr = conn.recv(4)
                if len(hdr) < 4:
                    return
                n = struct.unpack(">I", hdr)[0]
                received.append(conn.recv(n).decode())
                reply, delay = next(it, ("Matisse> OK", 0))
                time.sleep(delay)
                b = reply.encode()
                try:
                    conn.sendall(struct.pack(">I", len(b)) + b)
                except OSError:
                    return

    def run():
        srv.settimeout(5)
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def test_R10_connect_banner_is_drained():
    banner_text = b"Matisse Commander Network Server ready"
    banner = struct.pack(">I", len(banner_text)) + banner_text
    rec = []
    port = _scripted_server([("Matisse> 799.446555", 0)], rec, banner=banner)
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", port)
    c.connect()
    assert c.get_wavelength_nm() == pytest.approx(799.446555)
    c.close(graceful=False)


def test_R11_open_uses_long_timeout_without_retry():
    rec = []
    port = _scripted_server([("Matisse> OK", 1.2)], rec)
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", port, command_timeout_s=0.5,
                                          open_timeout_s=3.0)
    c.cd_open()
    assert rec == ["#SERVER MCP_WM_CounterDrift"]
    c.close(graceful=False)


def test_R11_wavelength_poll_timeout_is_not_retried():
    rec = []
    port = _scripted_server([("Matisse> 799.4", 1.0), ("Matisse> 799.4", 1.0)], rec)
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", port, command_timeout_s=0.3)
    with pytest.raises(OSError):
        c.get_wavelength_nm()
    time.sleep(0.3)
    assert rec == ["#SERVER MCP_WM_GET_WAVELENGTH"]


def test_R5_panel_flags_undelivered_setpoint(qapp):
    panel = matisse_cd.MatisseCDPanel(["TiSa_1"])
    base = {"wlm_port": 1, "connected": True, "active": True, "restored": False,
            "want_connected": True, "wavelength_nm": NM, "mismatch_mhz": 0.0,
            "last_error": ""}
    panel.update_snapshot({"TiSa_1": dict(base, setpoint_pending=True)})
    assert "NOT DELIVERED" in panel._rows["TiSa_1"]["wl"].text()
    panel.update_snapshot({"TiSa_1": dict(base, setpoint_pending=False)})
    assert "NOT DELIVERED" not in panel._rows["TiSa_1"]["wl"].text()
