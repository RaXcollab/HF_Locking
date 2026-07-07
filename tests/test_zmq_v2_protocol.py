"""H7: ZMQ v2 protocol roundtrip via InMemoryTransport.

Exercises the REAL ``_LaserLockV2Server`` (RemoteControlServerBase
subclass) that ships inside ``workers.py``. No sockets bound; the test
pairs two ``InMemoryTransport`` instances so the dispatcher path runs
end-to-end with real envelope encode/parse.

Pins:
  * HELLO reply: status SUCCESS, protocol_version 2,
    capabilities = {monitors, heartbeat, wait_for_lock}, NO
    ``connections`` key (single-instance server).
  * v1 hard sunset: missing ``v`` -> v1_protocol_refused.
  * id echo on every reply.
  * PROGRAM_VALUE: bare-integer connection accepted; wait_for_lock
    moves into args dict (Q2 §10-resolved); TIMEOUT status when lock
    fails to converge.
  * CHECK_VALUE: returns shared-state setpoint.

Run:
    conda activate guis && pytest GUIs/HF_Locking/tests/test_zmq_v2_protocol.py -v
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from tests.conftest import require_workers


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def workers_mod():
    w, _ = require_workers()
    return w


@pytest.fixture
def zmq_v2():
    """Importable only after workers.py path-injection runs."""
    pytest.importorskip("zmq_v2")
    import zmq_v2  # noqa: PLC0415
    return zmq_v2


@pytest.fixture
def make_v2_pair(workers_mod, zmq_v2):
    """Returns (outer, client_transport, v2_server). Caller drives
    ``serve_once`` after each client.send()."""
    def _factory(*, lock_enabled=False, deviation_mode=False,
                 setpoint_seed=None, wait_for_lock=True,
                 lock_will_succeed=True):
        outer = mock.MagicMock()
        outer.wait_for_lock = bool(wait_for_lock)

        # Real SharedExperimentState so set_status / get_status merge runs
        # exactly as in production.
        state = workers_mod.SharedExperimentState()
        if setpoint_seed:
            for port, value in setpoint_seed.items():
                state.update_status(port, {"setpoint": value})
        for port in workers_mod.PORTS:
            state.update_status(port, {"lock_enabled": lock_enabled})
        state.update_globals({"deviation_mode": deviation_mode})
        outer.state = state

        outer._wait_for_lock = mock.MagicMock(return_value=lock_will_succeed)

        client_t, server_t = zmq_v2.InMemoryTransport.pair()
        v2_server = workers_mod._LaserLockV2Server(outer, server_t)
        return outer, client_t, v2_server

    return _factory


def _roundtrip(client_t, v2_server, envelope_dict):
    client_t.send(json.dumps(envelope_dict).encode("utf-8"))
    served = v2_server.serve_once(timeout_ms=100)
    assert served is True, "serve_once did not dispatch"
    reply_bytes = client_t.recv(timeout_ms=100)
    return json.loads(reply_bytes.decode("utf-8"))


# ---------------------------------------------------------------- tests


def test_H7_hello_advertises_canonical_capabilities_no_connections(
        zmq_v2, make_v2_pair):
    """Single-instance server: HELLO reply has capabilities but NO
    `connections` key (only hubs advertise prefixes per Q1)."""
    outer, client_t, v2_server = make_v2_pair()

    reply = _roundtrip(client_t, v2_server,
                       {"v": 2, "id": 1, "action": "HELLO"})

    assert reply["status"] == "SUCCESS"
    assert reply["id"] == 1
    assert reply["protocol_version"] == 2
    assert reply["server"] == "LaserLockGUI"
    caps = set(reply["capabilities"])
    assert caps == {"monitors", "heartbeat", "wait_for_lock"}
    assert caps.issubset(zmq_v2.CANONICAL_CAPABILITIES)
    assert "connections" not in reply, (
        "single-instance server must NOT advertise connections (Q1)")


def test_H7_v1_envelope_refused(make_v2_pair):
    """Q4 hard sunset: no v: 2 -> ERROR / v1_protocol_refused."""
    outer, client_t, v2_server = make_v2_pair()

    reply = _roundtrip(client_t, v2_server,
                       {"action": "HELLO"})

    assert reply["status"] == "ERROR"
    assert reply["error"]["code"] == "v1_protocol_refused"


def test_H7_program_value_immediate_success_when_no_lock_wait(make_v2_pair):
    """No lock-wait conditions met -> SUCCESS returned immediately,
    setpoint signal still emitted."""
    outer, client_t, v2_server = make_v2_pair(
        lock_enabled=False, deviation_mode=False)

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 7, "action": "PROGRAM_VALUE",
        "connection": "4", "value": 348.666410,
    })

    assert reply["status"] == "SUCCESS"
    assert reply["id"] == 7
    outer.request_setpoint_write.emit.assert_called_once_with(4, 348.666410)
    outer._wait_for_lock.assert_not_called()


def test_H7_program_value_blocks_on_lock_wait_via_args(make_v2_pair):
    """When lock_enabled + deviation_mode + args.wait_for_lock=True ->
    delegates to outer._wait_for_lock(port, target). Q2 §10-resolved:
    wait_for_lock lives in `args`, not top-level."""
    outer, client_t, v2_server = make_v2_pair(
        lock_enabled=True, deviation_mode=True, lock_will_succeed=True)

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 8, "action": "PROGRAM_VALUE",
        "connection": "4", "value": 348.666410,
        "args": {"wait_for_lock": True},
    })

    assert reply["status"] == "SUCCESS"
    outer._wait_for_lock.assert_called_once_with(4, 348.666410)


def test_H7_program_value_lock_timeout_returns_TIMEOUT_status(make_v2_pair):
    """Lock-wait failure -> v2 TIMEOUT enum with retryable=True."""
    outer, client_t, v2_server = make_v2_pair(
        lock_enabled=True, deviation_mode=True, lock_will_succeed=False)

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 9, "action": "PROGRAM_VALUE",
        "connection": "4", "value": 348.666410,
        "args": {"wait_for_lock": True},
    })

    assert reply["status"] == "TIMEOUT"
    assert reply["error"]["code"] == "lock_wait_timeout"
    assert reply["error"]["retryable"] is True


def test_H7_program_value_unparseable_connection_returns_UNKNOWN_CONNECTION(
        make_v2_pair):
    """HF expects port-integer-strings; non-integer -> UNKNOWN_CONNECTION."""
    outer, client_t, v2_server = make_v2_pair()

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 10, "action": "PROGRAM_VALUE",
        "connection": "Vexlum_set", "value": 348.0,
    })

    assert reply["status"] == "UNKNOWN_CONNECTION"
    assert reply["error"]["code"] == "unknown_connection"
    outer.request_setpoint_write.emit.assert_not_called()


def test_H7_program_value_non_numeric_value_returns_invalid_value(make_v2_pair):
    outer, client_t, v2_server = make_v2_pair()

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 11, "action": "PROGRAM_VALUE",
        "connection": "4", "value": "not_a_number",
    })

    assert reply["status"] == "ERROR"
    assert reply["error"]["code"] == "invalid_value"


def test_H7_check_value_returns_setpoint_from_shared_state(make_v2_pair):
    """CHECK_VALUE reads from SharedExperimentState (the DLL-read cache),
    NOT GUI textboxes — Q4-resolved spec §1.3 fields."""
    outer, client_t, v2_server = make_v2_pair(
        setpoint_seed={4: 348.123456})

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 12, "action": "CHECK_VALUE",
        "connection": "4",
    })

    assert reply["status"] == "SUCCESS"
    assert reply["value"] == 348.123456


def test_H7_unknown_action_returns_unknown_action(make_v2_pair):
    outer, client_t, v2_server = make_v2_pair()

    reply = _roundtrip(client_t, v2_server,
                       {"v": 2, "id": 13, "action": "FROBNICATE"})

    assert reply["status"] == "ERROR"
    assert reply["error"]["code"] == "unknown_action"


# Review I3 2026-05-23: port range validation.

def test_H7_program_value_port_out_of_range_rejected(make_v2_pair):
    """Port 0 / 9+ -> UNKNOWN_CONNECTION + code=port_out_of_range."""
    outer, client_t, v2_server = make_v2_pair()

    for bad_port in (0, 9, -1, 99):
        reply = _roundtrip(client_t, v2_server, {
            "v": 2, "id": 14, "action": "PROGRAM_VALUE",
            "connection": str(bad_port), "value": 100.0,
        })
        assert reply["status"] == "UNKNOWN_CONNECTION", (
            f"port={bad_port} should be rejected: {reply}")
        assert reply["error"]["code"] == "port_out_of_range"
    # No setpoint signal must have fired for any bad port.
    outer.request_setpoint_write.emit.assert_not_called()


# Review C2 2026-05-23: lock-bypass warning when wait=True but
# AND-gate (lock_enabled + deviation_mode) not met.

def test_H7_wait_for_lock_with_lock_off_logs_warning_and_writes(make_v2_pair):
    """lock_enabled=False, dev_mode=True, wait=True -> SUCCESS without
    blocking, but log_message warns about the silent lock-bypass."""
    outer, client_t, v2_server = make_v2_pair(
        lock_enabled=False, deviation_mode=True)

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 15, "action": "PROGRAM_VALUE",
        "connection": "4", "value": 348.0,
        "args": {"wait_for_lock": True},
    })

    assert reply["status"] == "SUCCESS"
    outer._wait_for_lock.assert_not_called()
    # The setpoint write DID fire (lab state changed) -- the WARNING
    # surfaces this in BLACS.log so the operator sees the gap.
    outer.request_setpoint_write.emit.assert_called_once_with(4, 348.0)
    warning_msgs = [
        c.args[0] for c in outer.log_message.emit.call_args_list
        if "WARNING" in c.args[0]
    ]
    assert warning_msgs, "expected a WARNING log for the lock-bypass"


def test_H7_wait_for_lock_with_dev_mode_off_logs_warning_and_writes(
        make_v2_pair):
    """lock_enabled=True, dev_mode=False, wait=True -> SUCCESS + warning."""
    outer, client_t, v2_server = make_v2_pair(
        lock_enabled=True, deviation_mode=False)

    _roundtrip(client_t, v2_server, {
        "v": 2, "id": 16, "action": "PROGRAM_VALUE",
        "connection": "4", "value": 348.0,
        "args": {"wait_for_lock": True},
    })

    outer._wait_for_lock.assert_not_called()
    outer.request_setpoint_write.emit.assert_called_once_with(4, 348.0)
    warning_msgs = [
        c.args[0] for c in outer.log_message.emit.call_args_list
        if "WARNING" in c.args[0]
    ]
    assert warning_msgs, "expected a WARNING log for the lock-bypass"


# Review I4 2026-05-23: CHECK_VALUE on uninitialized port -> UNKNOWN_CONNECTION.

def test_H7_check_value_uninitialized_port_returns_UNKNOWN(make_v2_pair):
    """CHECK_VALUE on a port with no setpoint (default 0.0) MUST NOT
    return 0.0 -- BLACS would write 0.0 THz to a laser. Return
    UNKNOWN_CONNECTION/setpoint_not_initialized instead."""
    # Default factory: no setpoint_seed, so all ports stay at 0.0.
    outer, client_t, v2_server = make_v2_pair()

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 17, "action": "CHECK_VALUE",
        "connection": "4",
    })

    assert reply["status"] == "UNKNOWN_CONNECTION"
    assert reply["error"]["code"] == "setpoint_not_initialized"
    assert reply["error"]["retryable"] is True


# BLOCKER 2 (2026-07-07): wait_for_lock absence-semantics inversion.
# The absent `args.wait_for_lock` key used to fall back to the instance
# default -- which main_wlm.py constructs as True -- so a manual program on
# a *locked* channel silently lock-waited (5 s client timeout vs 60 s server
# wait). Absence must mean False regardless of the instance flag.

def test_H8_wait_for_lock_absent_defaults_to_no_wait(make_v2_pair):
    """PROGRAM_VALUE with NO args key must return SUCCESS immediately and
    never call _wait_for_lock, even though the outer worker was constructed
    with wait_for_lock=True (main_wlm.py passes True). Guards against the
    absence-semantics inversion found 2026-07-02.

    Channel is fully locked (lock_enabled + deviation_mode) so the ONLY
    thing that can keep this from blocking is absent-key => False. If the
    server ever falls back to `self._outer.wait_for_lock` (True here), it
    calls _wait_for_lock and this test fails."""
    outer, client_t, v2_server = make_v2_pair(
        lock_enabled=True, deviation_mode=True, wait_for_lock=True,
        lock_will_succeed=True)
    assert outer.wait_for_lock is True  # precondition: instance default True

    reply = _roundtrip(client_t, v2_server, {
        "v": 2, "id": 18, "action": "PROGRAM_VALUE",
        "connection": "4", "value": 348.686,
    })  # NO args key -> absence must resolve to False

    assert reply["status"] == "SUCCESS"
    outer._wait_for_lock.assert_not_called()
