"""Regression tests for the 2026-09-30 (Fable) adversarial review of
b98c91d..20b1a78. Written BEFORE the fixes.

  F2  abort() is terminal: no silent reconnect+retry after abort (real client,
      silent TCP server); group stop with a real hung client finishes threads
  F3  success replies containing "error" are not errors; header-echoed DSP
      codes are honoured
  F4  header-echoed wavelength reply parses
  F5  CounterDrift OFF not confirmed (MC hung) -> port stays claimed, HF lock
      stays blocked/not restored, retried by the poll; MC not running
      (connection refused) counts as OFF
(F1 -- bring-up tool frozen PUB value -- lives in test_matisse_cd_bringup.py.)
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

import pytest

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

matisse_cd = pytest.importorskip("matisse_cd", reason="needs PyQt5")

from tests.conftest import FakeCDState, FakeMatisseClient  # noqa: E402
from tests.test_matisse_review_fixes import _one_reply_server, _cfg, _worker, F  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt5 import QtWidgets  # noqa: PLC0415
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _silent_server():
    """Accepts connections, reads, never replies. Counts connections."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    conns = []

    def run():
        srv.settimeout(10)
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            conns.append(c)

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], conns


# ------------------------------------------------------------------ F2

def test_F2_abort_is_terminal_no_reconnect_retry():
    port, conns = _silent_server()
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", port, command_timeout_s=4.0)
    threading.Timer(0.3, c.abort).start()
    t0 = time.monotonic()
    with pytest.raises(OSError):
        c.cd_setpoint_nm("799.44655467")          # retrying write
    dt = time.monotonic() - t0
    assert dt < 1.5, f"abort did not unblock promptly ({dt:.1f}s)"
    time.sleep(0.2)
    assert len(conns) == 1, f"reconnected after abort ({len(conns)} connections)"


def test_F2_group_stop_with_real_hung_client_finishes(qapp):
    port, conns = _silent_server()
    cfg = _cfg(command_timeout_s=4.0)
    cfg["lasers"]["TiSa_1"].update(port=port, active=True)      # restored-active
    cfg["lasers"]["TiSa-2"].update(connect=False)
    shared = FakeCDState()
    g = matisse_cd.MatisseCDGroup(shared, cfg, save_fn=lambda c: None)
    g.start()
    time.sleep(0.5)                                             # TiSa_1 now blocked in its poll
    from PyQt5 import QtCore  # noqa: PLC0415
    QtCore.QMetaObject.invokeMethod(g.workers[0], "handle_setpoint_committed",
                                    QtCore.Qt.QueuedConnection,
                                    QtCore.Q_ARG(int, 1), QtCore.Q_ARG(float, 375.001))
    t0 = time.monotonic()
    ok = g.stop(timeout_s=1.0)
    assert ok is True
    assert time.monotonic() - t0 < 3.0
    assert all(not th.isRunning() for th in g.threads)


# ------------------------------------------------------------------ F3

@pytest.mark.parametrize("reply", [
    "No error",
    ':MCP_WM.Counterdrift Activate: 0,"no error"',
    '0,"No errors"',
    "OK, no errors",
])
def test_F3_success_replies_mentioning_error_pass(reply):
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", _one_reply_server(reply))
    c.cd_activate(True)
    c.close(graceful=False)


@pytest.mark.parametrize("reply", [
    ':MCP_WM.Counterdrift Activate: 2,"parameter out of range"',
    "Error: no errors allowed here",    # leading Error wins
])
def test_F3_header_echoed_dsp_errors_raise(reply):
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", _one_reply_server(reply))
    with pytest.raises(matisse_cd.MatisseError):
        c.cd_activate(True)
    c.close(graceful=False)


# ------------------------------------------------------------------ F4

@pytest.mark.parametrize("reply,nm", [
    (":MCP_WM_GET_WAVELENGTH: 799.446555 nm", 799.446555),
    ("799,446555 nm", 799.446555),
    ("Matisse> 799.446555", 799.446555),
])
def test_F4_wavelength_reply_shapes(reply, nm):
    c = matisse_cd.MatisseCommanderClient("127.0.0.1", _one_reply_server(reply))
    assert c.get_wavelength_nm() == pytest.approx(nm)
    c.close(graceful=False)


# ------------------------------------------------------------------ F5

class _OffTimesOut(FakeMatisseClient):
    hung = True

    def cd_activate(self, on):
        self.calls.append(("activate", on))
        if not on and self.hung:
            raise socket.timeout("MC hung")


class _OffRefused(FakeMatisseClient):
    def cd_activate(self, on):
        self.calls.append(("activate", on))
        if not on:
            self.connected = False
            raise ConnectionRefusedError("MC not running")


def test_F5_unconfirmed_off_keeps_port_claimed_until_confirmed():
    w, st, clients = _worker(client_cls=_OffTimesOut)
    w.handle_activate("TiSa_1", True)
    assert st.status[1]["cd_active"] is True
    w.handle_activate("TiSa_1", False)               # MC hung: OFF not confirmed
    assert st.status[1]["cd_active"] is True, "HF lock must stay blocked"
    snap = w.get_snapshot()["TiSa_1"]
    assert snap["off_unconfirmed"] is True
    c = clients[30000]
    c.hung = False                                   # MC responsive again
    w._poll()
    assert c.calls[-1] == ("activate", False)
    assert st.status[1]["cd_active"] is False
    assert w.get_snapshot()["TiSa_1"]["off_unconfirmed"] is False


def test_F5_refused_connection_counts_as_off():
    w, st, clients = _worker(client_cls=_OffRefused)
    w.handle_activate("TiSa_1", True)
    w.handle_activate("TiSa_1", False)
    assert st.status[1]["cd_active"] is False
    assert w.get_snapshot()["TiSa_1"]["off_unconfirmed"] is False


class _ActivateTimesOutThenOffHung(FakeMatisseClient):
    def cd_activate(self, on):
        self.calls.append(("activate", on))
        raise socket.timeout("no reply")             # Activate true MAY have executed


def test_F5_activation_timeout_with_unconfirmed_off_does_not_restore_hf_lock():
    w, st, clients = _worker(lock_enabled=True, client_cls=_ActivateTimesOutThenOffHung)
    w.handle_activate("TiSa_1", True)
    assert (1, True) not in w.hf, "HF lock restored while CD may be running"
    assert st.status[1]["cd_active"] is True
    assert w.get_snapshot()["TiSa_1"]["off_unconfirmed"] is True
