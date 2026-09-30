# matisse_cd.py
"""Optional Matisse CounterDrift offload for HF_Locking.

Instead of the WS7 PID driving the Matisse through its analog deviation
output, Matisse Commander's own wavemeter plugin ("CounterDrift") holds the
laser on a wavelength setpoint. HF_Locking stays the single place where the
setpoint is set -- locally (Set F) or remotely (BLACS PROGRAM_VALUE):

    Set F / ZMQ PROGRAM_VALUE
        -> WavemeterWorker.handle_setpoint_write   (WS7 course setpoint, as before)
        -> WavemeterWorker.setpoint_committed(port, THz)          [new signal]
        -> MatisseCDWorker.handle_setpoint_committed
        -> Matisse Commander TCP: "#SERVER MCP_WM.Counterdrift Setpoint <nm>"

The WS7 course setpoint is still written so CHECK_VALUE, the plot setpoint
line, the lock indicator and ZMQ wait_for_lock keep working unchanged. The
WS7 PID for that channel is switched OFF (deviation channel unassigned) while
CounterDrift is active so the two loops never fight; WavemeterWorker refuses
to re-arm it while `cd_active` is set for the port.

Threading: MatisseCDWorker is a QObject on its own QThread and owns every
Matisse Commander socket. It never touches wlmData.dll -- the HF lock is
switched off by signalling WavemeterWorker (request_hf_lock). Blocking TCP
I/O therefore never stalls the 20 ms DLL poll or the GUI.

Wire protocol (adapted from a collaborator's matisse_cd_controller.py and
tools/matisse_scpi_probe.py): LabVIEW length-prefixed framing, 4-byte
big-endian length + ASCII payload, both directions. MCP_* commands are
"Server-Only" and need the "#SERVER " prefix (Matisse Programmer's Guide
v2.4.8 ch. 3, as cited by that file -- not re-verified here). Replies may
start with the "Matisse>" prompt, which is stripped.

UNVERIFIED on our hardware (see CLAUDE.md "Matisse CounterDrift"):
  - wavelength convention of the CounterDrift setpoint (we send VACUUM nm);
    the activation interlock compares MC's wavelength readback against the
    HF measurement and refuses on mismatch, which catches air/vacuum
    (~2.7e-4 relative) and wrong-channel mapping;
  - decimal separator LabVIEW expects on the lab PC (config "decimal_sep");
  - MCP_WM_GET_WAVELENGTH returns nm (per the collaborator, 2026-09-29; air vs
    vacuum not stated) -- whether it works with the plugin closed is unknown.

Front end is THz everywhere (Set F, BLACS, panel readout); nm exists only on
the wire to Matisse Commander.

Standalone read-only probe (no HF_Locking needed):
    python matisse_cd.py --probe 127.0.0.1 30000
"""
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from datetime import datetime

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import QObject, QMutex, QMutexLocker, QTimer, pyqtSignal, pyqtSlot

C_NM_THZ = 299792.458          # c in nm*THz: lambda_vac[nm] = C_NM_THZ / f[THz]
MIN_VALID_SETPOINT_THZ = 1.0   # mirrors workers.MIN_VALID_SETPOINT_THZ (no import: keeps zmq_v2 optional)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_APP_DIR, "matisse_cd_config.json")

# Defaults, overridden by matisse_cd_config.json (written from the GUI panel).
# wlm_port = HF_Locking channel the laser is measured on; 0 = unassigned.
# Two Matisse Commander instances on one PC must use distinct server ports.
DEFAULT_CONFIG = {
    "lasers": {
        "TiSa_1": {"host": "127.0.0.1", "port": 30000, "wlm_port": 1, "connect": True, "active": False},
        "TiSa-2": {"host": "127.0.0.1", "port": 30001, "wlm_port": 6, "connect": True, "active": False},
    },
    "decimal_sep": ".",            # separator in the setpoint string sent to LabVIEW
    "setpoint_decimals": 8,        # nm digits; 1e-8 nm ~ 5 kHz at 800 nm
    "max_mismatch_mhz": 500.0,     # activation interlock: |f_HF - c/lambda_MC|
    "max_activation_offset_mhz": 1000.0,  # activation interlock: |f_HF - WS7 setpoint|
    "runaway_mhz": 5000.0,         # watchdog: deactivate CD if |f_HF - SP| exceeds this ...
    "runaway_s": 20.0,             # ... continuously for this long
    "connect_timeout_s": 2.0,
    "command_timeout_s": 10.0,
    "open_timeout_s": 120.0,       # MCP_WM_CounterDrift (cold plugin start), never retried
}

POLL_MS = 1000
RECONNECT_BACKOFF_S = 5.0
HF_LOCK_OFF_WAIT_S = 2.0


def thz_to_nm(f_thz: float) -> float:
    """Vacuum wavelength in nm for a frequency in THz."""
    return C_NM_THZ / float(f_thz)


def format_nm(nm: float, decimals: int = 8, decimal_sep: str = ".") -> str:
    return f"{float(nm):.{int(decimals)}f}".replace(".", decimal_sep)


# ---------------------------------------------------------------------------
# Config persistence (atomic write, same pattern as config.py)
# ---------------------------------------------------------------------------

def _dedupe_channels(cfg: dict) -> None:
    """Two lasers on one HF channel would let an inactive one un-block the HF
    lock of an active one. Keep the first (active lasers win), unassign the rest."""
    seen = {}
    order = sorted(cfg["lasers"].items(), key=lambda kv: not kv[1].get("active", False))
    for name, lc in order:
        p = int(lc.get("wlm_port", 0) or 0)
        if p == 0:
            continue
        if p in seen:
            print(f"[MATISSE] WARNING: {name} and {seen[p]} both on HF ch{p}; "
                  f"unassigning {name} (set its channel in the panel).")
            lc["wlm_port"] = 0
        else:
            seen[p] = name


def load_config(path: str = CONFIG_PATH) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                saved = json.load(f)
        except Exception as e:
            print(f"[MATISSE] WARNING: could not read {path}: {e}; using defaults")
            saved = {}
        for k, v in saved.items():
            if k == "lasers" and isinstance(v, dict):
                for name, lc in v.items():
                    cfg["lasers"].setdefault(name, {}).update(lc)
            elif k in cfg:
                cfg[k] = v
    _dedupe_channels(cfg)
    return cfg


def save_config(cfg: dict, path: str = CONFIG_PATH) -> None:
    data = dict(cfg)
    data["saved_at"] = datetime.now().isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Matisse Commander TCP client
# ---------------------------------------------------------------------------

class MatisseError(RuntimeError):
    """Matisse Commander answered, but with an error reply."""


# Error reply shapes: "Error: ..." (MC), 'N,"msg"' with N != 0 (laser DSP, also
# after a header echo ":CMD: N,\"..\""), "!ERROR n", ":CMD: Error: ...", "Err: ...".
# Success texts that merely mention errors ("No error", '0,"no error"') pass.
_ERR_WORD = re.compile(r"\berr(or)?\b", re.IGNORECASE)
_NO_ERROR = re.compile(r"\bno\s+errors?\b", re.IGNORECASE)
_DSP_CODE = re.compile(r'(?:^|:)\s*(-?\d+)\s*,\s*"')


def _is_error_reply(reply: str) -> bool:
    r = reply.strip()
    m = _DSP_CODE.search(r)
    if m:
        return int(m.group(1)) != 0
    return bool(_ERR_WORD.search(_NO_ERROR.sub("", r))) or r.startswith("!")


class MatisseCommanderClient:
    """One persistent connection to one Matisse Commander Network Server.

    Used only from its laser's worker thread, except abort() (any thread),
    which shuts the socket down to unblock a hung recv at application exit.

    A transport failure (incl. timeout mid-reply) closes the socket, so a late
    reply can never be read as the answer to the next command. Idempotent
    writes retry once on a fresh connection; read-only polls and the slow
    plugin-open do not.
    """
    _HDR = ">I"
    _MAX_PAYLOAD = 1_000_000

    def __init__(self, host: str, port: int, connect_timeout_s: float = 2.0,
                 command_timeout_s: float = 10.0, open_timeout_s: float = 120.0):
        self.host = host
        self.port = int(port)
        self.connect_timeout_s = float(connect_timeout_s)
        self.command_timeout_s = float(command_timeout_s)
        self.open_timeout_s = float(open_timeout_s)
        self.sock = None
        self._aborted = False     # set by abort(); terminal (app is shutting down)

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def connect(self) -> None:
        if self._aborted:
            raise ConnectionAbortedError("client aborted (shutting down)")
        self.close(graceful=False)
        s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout_s)
        # Publish the socket BEFORE the drain so abort() can shut it down; then
        # re-check the flag (covers abort() having run just before this line).
        self.sock = s
        if self._aborted:
            self.close(graceful=False)
            raise ConnectionAbortedError("client aborted (shutting down)")
        # Drain any banner MC emits on connect (as the collaborator's client does);
        # otherwise every later reply would be read one frame late.
        s.settimeout(0.3)
        try:
            while s.recv(4096):
                pass
        except socket.timeout:
            pass
        except OSError:
            self.close(graceful=False)
            raise
        s.settimeout(self.command_timeout_s)

    def close(self, graceful: bool = True) -> None:
        if self.sock is None:
            return
        if graceful:
            # Matisse Commander wants an explicit close; dropping the socket
            # leaves its server VI in LabVIEW Error 56 (matisse_scpi_probe.py).
            try:
                self._send("Close_Network_Connection")
                time.sleep(0.3)
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None

    def abort(self) -> None:
        self._aborted = True
        s = self.sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _send(self, cmd: str) -> None:
        payload = cmd.encode("ascii")
        self.sock.sendall(struct.pack(self._HDR, len(payload)) + payload)

    def _recv_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Matisse Commander closed the connection")
            buf += chunk
        return buf

    def _read_reply(self) -> str:
        (n,) = struct.unpack(self._HDR, self._recv_exactly(4))
        if n > self._MAX_PAYLOAD:
            raise ConnectionError(f"implausible reply length {n}; framing mismatch?")
        text = self._recv_exactly(n).decode("ascii", "replace") if n else ""
        text = text.strip()
        if text.startswith("Matisse>"):
            text = text[len("Matisse>"):].strip()
        return text

    def ask(self, cmd: str, timeout_s: float = None, retry: bool = True) -> str:
        for attempt in (0, 1):
            if self._aborted:
                raise ConnectionAbortedError("client aborted (shutting down)")
            try:
                if self.sock is None:
                    self.connect()
                if self._aborted:
                    raise ConnectionAbortedError("client aborted (shutting down)")
                self.sock.settimeout(timeout_s or self.command_timeout_s)
                try:
                    self._send(cmd)
                    return self._read_reply()
                finally:
                    if self.sock is not None:
                        self.sock.settimeout(self.command_timeout_s)
            except OSError:
                self.close(graceful=False)
                if attempt or not retry:
                    raise
        raise AssertionError("unreachable")

    def mcp(self, cmd: str, timeout_s: float = None, retry: bool = True) -> str:
        """Send a Server-Only MCP command; raise MatisseError on an error reply."""
        full = cmd if cmd.lstrip().startswith("#SERVER") else f"#SERVER {cmd}"
        reply = self.ask(full, timeout_s=timeout_s, retry=retry)
        if _is_error_reply(reply):
            raise MatisseError(f"{cmd!r} -> {reply!r}")
        return reply

    # ---- CounterDrift wrappers (command strings from matisse_cd_controller.py) ----
    def cd_open(self) -> str:
        # Cold plugin start can take many seconds (collaborator used 120 s);
        # never re-send it on timeout.
        return self.mcp("MCP_WM_CounterDrift", timeout_s=self.open_timeout_s, retry=False)

    def cd_setpoint_nm(self, nm_str: str) -> str:
        return self.mcp(f"MCP_WM.Counterdrift Setpoint {nm_str}")

    def cd_activate(self, state: bool) -> str:
        return self.mcp(f"MCP_WM.Counterdrift Activate {'true' if state else 'false'}")

    def get_wavelength_nm(self) -> float:
        reply = self.mcp("MCP_WM_GET_WAVELENGTH", retry=False)
        for tok in reply.split():        # first numeric token (skips a ":CMD:" echo)
            try:
                return float(tok.replace(",", "."))
            except ValueError:
                continue
        raise MatisseError(f"unparseable wavelength reply {reply!r}")


# ---------------------------------------------------------------------------
# Cross-laser registry (shared by the per-laser workers)
# ---------------------------------------------------------------------------

class CDRegistry:
    """Which laser claims which HF channel. cd_active(port) is the OR over
    lasers, so an idle laser can never clear another laser's claim. Also
    serialises config edits + saves across worker threads."""

    def __init__(self, cfg: dict, save_fn=save_config):
        self.cfg = cfg
        self._save_fn = save_fn
        self._lock = threading.RLock()
        self._claims = {}   # name -> port (activating or active)

    def claim(self, name: str, port: int) -> bool:
        with self._lock:
            if any(p == port for n, p in self._claims.items() if n != name):
                return False
            self._claims[name] = port
            return True

    def release(self, name: str) -> None:
        with self._lock:
            self._claims.pop(name, None)

    def claimed(self, port: int) -> bool:
        with self._lock:
            return port in self._claims.values()

    def port_of(self, name: str) -> int:
        with self._lock:
            return int(self.cfg["lasers"][name].get("wlm_port", 0) or 0)

    def assign(self, name: str, port: int) -> str:
        """Set a laser's HF channel; returns an error string or ''."""
        with self._lock:
            if name in self._claims:
                return "deactivate CounterDrift before changing its HF channel"
            if port and any(int(lc.get("wlm_port", 0) or 0) == port
                            for n, lc in self.cfg["lasers"].items() if n != name):
                return f"ch{port} is already assigned to another Matisse"
            self.cfg["lasers"][name]["wlm_port"] = int(port)
            self._save()
            return ""

    def set(self, name: str, **kv) -> None:
        with self._lock:
            self.cfg["lasers"][name].update(kv)
            self._save()

    def _save(self):
        try:
            self._save_fn(self.cfg)
        except Exception as e:
            print(f"[MATISSE] config save failed: {e}")


# ---------------------------------------------------------------------------
# Worker (own QThread per laser; owns that laser's socket; never touches the DLL)
# ---------------------------------------------------------------------------

class MatisseCDWorker(QObject):
    # HF_Locking port, enable -> WavemeterWorker.handle_lock_toggle (QueuedConnection)
    request_hf_lock = pyqtSignal(int, bool)
    log_message = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, shared_state, cfg: dict, client_factory=MatisseCommanderClient,
                 save_fn=save_config, names=None, registry=None):
        super().__init__()
        self.state = shared_state
        self.cfg = cfg
        self.registry = registry or CDRegistry(cfg, save_fn)
        self._client_factory = client_factory
        self._timer = None
        self._stopping = threading.Event()
        self._mutex = QMutex()      # guards self._snap (GUI pulls it)
        self._lasers = {}
        self._snap = {}
        for name in (names or list(cfg["lasers"])):
            lc = cfg["lasers"][name]
            active = bool(lc.get("active", False))
            self._lasers[name] = {
                "client": None,
                "want_connected": bool(lc.get("connect", True)),
                "next_connect_t": 0.0,
                "cd_opened": False,
                "active": active,
                "restored": active,
                "runaway_since": None,
                "pending_sp": None,     # THz not yet delivered to CounterDrift
                "off_unconfirmed": False,   # OFF sent but not confirmed: port stays claimed
            }
            if active and self._port(name) in range(1, 9):
                self.registry.claim(name, self._port(name))
            self._snap[name] = {
                "host": lc.get("host"), "port": lc.get("port"),
                "wavelength_nm": None, "mismatch_mhz": None, "last_error": "",
            }
        for name in self._lasers:
            self._publish(name)

    # ---- helpers ----------------------------------------------------------
    def _lc(self, name):
        return self.cfg["lasers"][name]

    def _port(self, name) -> int:
        return self.registry.port_of(name)

    def _log(self, msg):
        self.log_message.emit(msg)

    def _publish_port(self, port):
        if port in range(1, 9):
            self.state.update_status(port, {"cd_active": self.registry.claimed(port)})

    def _publish(self, name, **extra):
        L = self._lasers[name]
        port = self._port(name)
        with QMutexLocker(self._mutex):
            s = self._snap[name]
            s.update(extra)
            s["wlm_port"] = port
            s["connected"] = bool(L["client"] is not None and L["client"].connected)
            s["active"] = L["active"]
            s["restored"] = L["restored"]
            s["want_connected"] = L["want_connected"]
            s["setpoint_pending"] = L["pending_sp"] is not None
            s["off_unconfirmed"] = L["off_unconfirmed"]
        self._publish_port(port)

    def get_snapshot(self) -> dict:
        """Thread-safe copy for the GUI's PULL refresh."""
        with QMutexLocker(self._mutex):
            return {n: dict(s) for n, s in self._snap.items()}

    def _client(self, name):
        L = self._lasers[name]
        if L["client"] is None:
            lc = self._lc(name)
            L["client"] = self._client_factory(
                lc["host"], lc["port"],
                connect_timeout_s=self.cfg["connect_timeout_s"],
                command_timeout_s=self.cfg["command_timeout_s"],
                open_timeout_s=self.cfg["open_timeout_s"])
        return L["client"]

    def abort_io(self):
        """Any thread: unblock a hung socket call so stop() can run."""
        self._stopping.set()
        for L in self._lasers.values():
            c = L["client"]
            if c is not None and hasattr(c, "abort"):
                c.abort()

    def _try_connect(self, name) -> bool:
        L = self._lasers[name]
        c = self._client(name)
        if c.connected:
            return True
        try:
            c.connect()
            L["cd_opened"] = False
            lc = self._lc(name)
            self._log(f"{name}: connected to Matisse Commander {lc['host']}:{lc['port']}")
            self._publish(name, last_error="")
            return True
        except OSError as e:
            L["next_connect_t"] = time.monotonic() + RECONNECT_BACKOFF_S
            self._publish(name, last_error=f"connect: {e}")
            return False

    def _hf_freq_thz(self, port):
        m = self.state.get_measurement(port)
        f = m.get("freq_display")
        if not m.get("valid", False) or f is None:
            return None
        return float(f)

    def _hf_lock_on(self, port) -> bool:
        return bool(self.state.get_status(port).get("lock_enabled", False))

    def _set_active(self, name, active: bool):
        L = self._lasers[name]
        L["active"] = bool(active)
        L["restored"] = False
        L["runaway_since"] = None
        L["off_unconfirmed"] = False
        if not active:
            L["pending_sp"] = None
            self.registry.release(name)
        self.registry.set(name, active=bool(active))
        self._publish(name)

    def _nm_str(self, f_thz):
        return format_nm(thz_to_nm(f_thz), self.cfg["setpoint_decimals"], self.cfg["decimal_sep"])

    # ---- lifecycle --------------------------------------------------------
    @pyqtSlot()
    def start(self):
        for name, L in self._lasers.items():
            if L["active"]:
                self._log(f"WARNING: {name} CounterDrift was ACTIVE when HF_Locking last "
                          f"exited; assuming it still runs in Matisse Commander. HF lock on "
                          f"ch{self._port(name)} is blocked until you Deactivate it.")
                self._publish(name)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLL_MS)
        QTimer.singleShot(0, self._poll)

    @pyqtSlot()
    def stop(self):
        self._stopping.set()
        if self._timer:
            self._timer.stop()
        for name, L in self._lasers.items():
            if L["active"]:
                self._log(f"{name}: CounterDrift left ACTIVE in Matisse Commander on exit.")
            if L["client"] is not None:
                L["client"].close()
        self.finished.emit()

    # ---- GUI commands (name-addressed; a worker ignores lasers it does not own) --
    @pyqtSlot(str, int)
    def handle_set_channel(self, name: str, wlm_port: int):
        if name not in self._lasers:
            return
        old = self._port(name)
        if wlm_port == old:
            return
        err = self.registry.assign(name, int(wlm_port))
        if err:
            self._log(f"{name}: {err}.")
            self._publish(name)
            return
        self._log(f"{name}: HF channel ch{old} -> ch{wlm_port}")
        self._publish(name, wavelength_nm=None, mismatch_mhz=None)

    @pyqtSlot(str, bool)
    def handle_connect(self, name: str, want: bool):
        if name not in self._lasers:
            return
        L = self._lasers[name]
        L["want_connected"] = bool(want)
        self.registry.set(name, connect=bool(want))
        if want:
            L["next_connect_t"] = 0.0
            self._try_connect(name)
        elif L["client"] is not None:
            L["client"].close()
            self._log(f"{name}: disconnected")
        self._publish(name)

    @pyqtSlot(str, bool)
    def handle_activate(self, name: str, enable: bool):
        if name not in self._lasers:
            return
        if enable:
            ok, why = self._activate(name)
            if not ok:
                self._log(f"{name}: CounterDrift NOT activated: {why}")
                self._publish(name, last_error=why)
        else:
            self._deactivate(name, reason="user")

    def _activate(self, name):
        L = self._lasers[name]
        port = self._port(name)
        if L["active"] and not L["restored"]:
            return True, ""
        if port not in range(1, 9):
            return False, "no HF channel assigned"
        if not self._try_connect(name):
            return False, "Matisse Commander not reachable"
        c = self._client(name)

        sp = float(self.state.get_status(port).get("setpoint", 0.0) or 0.0)
        if sp < MIN_VALID_SETPOINT_THZ:
            return False, f"ch{port} has no valid WS7 setpoint ({sp})"
        f_hf = self._hf_freq_thz(port)
        if f_hf is None:
            return False, f"no valid HF measurement on ch{port}"
        offset = (f_hf - sp) * 1e6
        if abs(offset) > float(self.cfg["max_activation_offset_mhz"]):
            return False, (f"laser is {offset:+.0f} MHz from the ch{port} setpoint "
                           f"(limit {self.cfg['max_activation_offset_mhz']} MHz). "
                           f"Set F near the current frequency first.")

        # Interlock: MC's wavemeter plugin must see the same laser in the same
        # (vacuum) convention as HF_Locking. Catches air/vacuum (~100 GHz),
        # wrong switch channel, and a non-nm reply.
        try:
            lam = c.get_wavelength_nm()
        except (MatisseError, OSError) as e:
            return False, f"wavelength readback failed: {e}"
        mism = (f_hf - thz_to_nm(lam)) * 1e6 if lam > 0 else float("inf")
        self._publish(name, wavelength_nm=lam, mismatch_mhz=mism)
        if not abs(mism) <= float(self.cfg["max_mismatch_mhz"]):
            return False, (f"Matisse Commander reads {lam:.6f} nm but HF ch{port} reads "
                           f"{thz_to_nm(f_hf):.6f} nm (vac), mismatch {mism:.0f} MHz > "
                           f"{self.cfg['max_mismatch_mhz']} MHz -- wrong channel or "
                           f"air/vacuum convention?")

        # Claim the port (blocks HF re-arm), THEN read the lock state fresh --
        # it may have changed during the wavelength round-trip.
        if not self.registry.claim(name, port):
            return False, f"ch{port} already driven by another CounterDrift"
        self._publish_port(port)
        hf_was_on = self._hf_lock_on(port)
        if hf_was_on:
            self.request_hf_lock.emit(port, False)
            if not self._wait_hf_off(port):
                self._abort_activation(name, port, hf_was_on)
                return False, f"HF lock on ch{port} did not switch off"
            self._log(f"{name}: HF (WS7 PID) lock on ch{port} switched OFF for CounterDrift")

        nm_str = self._nm_str(sp)
        activate_sent = False
        try:
            if not L["cd_opened"]:
                c.cd_open()
                L["cd_opened"] = True
            c.cd_setpoint_nm(nm_str)
            activate_sent = True
            c.cd_activate(True)
        except (MatisseError, OSError) as e:
            # "Activate true" may have executed although its reply failed: only
            # give the port back (and restore the HF lock) once OFF is confirmed.
            if not activate_sent or self._confirm_off(name):
                self._abort_activation(name, port, hf_was_on)
                return False, f"Matisse Commander command failed: {e}"
            self._mark_off_unconfirmed(name)
            return False, (f"Matisse Commander command failed ({e}) and CounterDrift OFF "
                           f"could not be confirmed -- ch{port} stays claimed, HF lock stays "
                           f"OFF; retrying OFF every poll.")

        self._set_active(name, True)
        L["pending_sp"] = None
        self._log(f"{name}: CounterDrift ACTIVE on ch{port}, setpoint {sp:.7f} THz = {nm_str} nm")
        if self._hf_lock_on(port):   # re-armed outside HF_Locking meanwhile
            self._log(f"WARNING: {name}: WS7 lock on ch{port} came back ON during "
                      f"activation; switching it off again.")
            self.request_hf_lock.emit(port, False)
        return True, ""

    def _wait_hf_off(self, port) -> bool:
        t_end = time.monotonic() + HF_LOCK_OFF_WAIT_S
        while self._hf_lock_on(port):
            if time.monotonic() > t_end or self._stopping.is_set():
                return False
            time.sleep(0.02)
        return True

    def _abort_activation(self, name, port, hf_was_on):
        self.registry.release(name)
        self._publish_port(port)
        if hf_was_on:
            self.request_hf_lock.emit(port, True)
            self._log(f"{name}: activation failed; restoring HF (WS7 PID) lock on ch{port}.")

    def _confirm_off(self, name) -> bool:
        """Send Activate false. True when CounterDrift is known OFF: MC acked,
        or MC is not running (connection refused -> its plugin cannot run)."""
        try:
            self._client(name).cd_activate(False)
            return True
        except ConnectionRefusedError:
            return True
        except (MatisseError, OSError) as e:
            self._last_off_error = str(e)
            return False

    def _mark_off_unconfirmed(self, name):
        L = self._lasers[name]
        L["active"] = False               # stop forwarding + watchdog ...
        L["off_unconfirmed"] = True       # ... but keep the port claimed
        L["pending_sp"] = None
        self.registry.set(name, active=True)   # still blocks the HF lock after a restart
        err = getattr(self, "_last_off_error", "")
        self._log(f"WARNING: {name}: CounterDrift OFF NOT confirmed ({err}). ch{self._port(name)} "
                  f"stays claimed (HF lock blocked); retrying OFF every poll. Closing Matisse "
                  f"Commander also counts as OFF.")
        self._publish(name, last_error=f"OFF unconfirmed: {err}")

    def _deactivate(self, name, reason):
        port = self._port(name)
        if not self._confirm_off(name):
            self._mark_off_unconfirmed(name)
            return
        self._set_active(name, False)
        self._log(f"{name}: CounterDrift deactivated ({reason}). ch{port} is now "
                  f"UNLOCKED -- enable the HF lock if needed.")
        self._publish(name, last_error="")

    # ---- setpoint forwarding ---------------------------------------------
    @pyqtSlot(int, float)
    def handle_setpoint_committed(self, port: int, f_thz: float):
        """WS7 course setpoint for `port` was written + read back. If a
        CounterDrift is active on that port, move its setpoint too."""
        if f_thz < MIN_VALID_SETPOINT_THZ:
            return
        for name, L in self._lasers.items():
            if L["active"] and self._port(name) == port:
                L["pending_sp"] = float(f_thz)
                self._deliver_setpoint(name)

    def _deliver_setpoint(self, name):
        L = self._lasers[name]
        f_thz = L["pending_sp"]
        nm_str = self._nm_str(f_thz)
        try:
            self._client(name).cd_setpoint_nm(nm_str)
        except (MatisseError, OSError) as e:
            self._log(f"ERROR: {name}: CounterDrift setpoint {nm_str} nm NOT delivered "
                      f"({e}); retrying every poll.")
            self._publish(name, last_error=f"setpoint not delivered: {e}")
            return
        if L["pending_sp"] == f_thz:
            L["pending_sp"] = None
        L["runaway_since"] = None
        self._log(f"{name}: CounterDrift setpoint -> {nm_str} nm ({f_thz:.7f} THz)")
        self._publish(name, last_error="")

    # ---- 1 Hz poll: reconnect, pending setpoint, wavelength readback, watchdog --
    def _poll(self):
        now = time.monotonic()
        for name, L in self._lasers.items():
            if self._stopping.is_set():
                return
            if L["off_unconfirmed"] and L["want_connected"]:
                if self._confirm_off(name):
                    self._set_active(name, False)
                    self._log(f"{name}: CounterDrift OFF confirmed; ch{self._port(name)} released.")
                    self._publish(name, last_error="")
            c = self._client(name)
            if not c.connected:
                if L["want_connected"] and now >= L["next_connect_t"]:
                    self._try_connect(name)
                if not c.connected:
                    self._publish(name)
                    continue
            if L["active"] and L["pending_sp"] is not None:
                self._deliver_setpoint(name)
            port = self._port(name)
            mism = None
            try:
                lam = c.get_wavelength_nm()
                f_hf = self._hf_freq_thz(port) if port in range(1, 9) else None
                if f_hf is not None and lam > 0:
                    mism = (f_hf - thz_to_nm(lam)) * 1e6
                self._publish(name, wavelength_nm=lam, mismatch_mhz=mism)
            except MatisseError as e:
                self._publish(name, wavelength_nm=None, mismatch_mhz=None,
                              last_error=f"wavelength: {e}")
            except OSError as e:
                self._log(f"{name}: lost Matisse Commander connection: {e}")
                L["next_connect_t"] = now + RECONNECT_BACKOFF_S
                self._publish(name, last_error=f"connection: {e}")
                continue
            if L["active"]:
                self._check_runaway(name, port, now)

    def _check_runaway(self, name, port, now):
        L = self._lasers[name]
        f_hf = self._hf_freq_thz(port)
        sp = float(self.state.get_status(port).get("setpoint", 0.0) or 0.0)
        if f_hf is None or sp < MIN_VALID_SETPOINT_THZ:
            return
        err_mhz = (f_hf - sp) * 1e6
        if abs(err_mhz) <= float(self.cfg["runaway_mhz"]):
            L["runaway_since"] = None
            return
        if L["runaway_since"] is None:
            L["runaway_since"] = now
        elif now - L["runaway_since"] > float(self.cfg["runaway_s"]):
            self._log(f"ERROR: {name}: ch{port} is {err_mhz:.0f} MHz from setpoint for "
                      f">{self.cfg['runaway_s']} s -- deactivating CounterDrift "
                      f"(mode hop, actuator at rail, or setpoint misparsed?)")
            self._deactivate(name, reason="runaway watchdog")


# ---------------------------------------------------------------------------
# One worker + QThread per laser, so a hung Matisse Commander only stalls itself
# ---------------------------------------------------------------------------

class MatisseCDGroup:
    def __init__(self, shared_state, cfg: dict, client_factory=MatisseCommanderClient,
                 save_fn=save_config, log_fn=None):
        self.cfg = cfg
        self.registry = CDRegistry(cfg, save_fn)
        self.workers, self.threads = [], []
        for name in cfg["lasers"]:
            w = MatisseCDWorker(shared_state, cfg, client_factory=client_factory,
                                names=[name], registry=self.registry)
            th = QtCore.QThread()
            w.moveToThread(th)
            th.started.connect(w.start)
            # DIRECT: quit() is thread-safe; a queued quit would wait for the GUI
            # thread, which is blocked in stop()'s wait() -> threads never finish.
            w.finished.connect(th.quit, QtCore.Qt.DirectConnection)
            if log_fn is not None:
                w.log_message.connect(log_fn)
            self.workers.append(w)
            self.threads.append(th)

    def start(self):
        for th in self.threads:
            th.start()

    def get_snapshot(self) -> dict:
        snap = {}
        for w in self.workers:
            snap.update(w.get_snapshot())
        return snap

    def stop(self, timeout_s: float = 3.0) -> bool:
        """Graceful stop; if a worker is stuck in socket I/O, abort its socket
        and wait again. Returns True when every thread has finished."""
        for w in self.workers:
            QtCore.QMetaObject.invokeMethod(w, "stop", QtCore.Qt.QueuedConnection)
        ok = True
        for w, th in zip(self.workers, self.threads):
            if not th.wait(int(timeout_s * 1000)):
                w.abort_io()
                if not th.wait(int(timeout_s * 1000)):
                    ok = False
                    print("[MATISSE] WARNING: CounterDrift worker thread did not stop.")
        return ok


# ---------------------------------------------------------------------------
# GUI panel (main thread; PULL-refreshed from MatisseCDWorker.get_snapshot())
# ---------------------------------------------------------------------------

class MatisseCDPanel(QtWidgets.QGroupBox):
    request_channel = pyqtSignal(str, int)
    request_connect = pyqtSignal(str, bool)
    request_activate = pyqtSignal(str, bool)

    def __init__(self, laser_names, parent=None):
        super().__init__("Matisse CounterDrift (digital lock via Matisse Commander)", parent)
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(4, 2, 4, 2)
        grid.setHorizontalSpacing(8)
        self._rows = {}
        for r, name in enumerate(laser_names):
            lbl = QtWidgets.QLabel(f"<b>{name}</b>")
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, 8)
            spin.setSpecialValueText("--")
            spin.setPrefix("HF ch ")
            spin.setToolTip("HF_Locking channel this Matisse is measured on (-- = unassigned)")
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda v, n=name: self.request_channel.emit(n, int(v)))
            btn_conn = QtWidgets.QPushButton("Connect")
            btn_conn.setCheckable(True)
            btn_conn.setChecked(True)
            btn_conn.clicked.connect(lambda chk, n=name: self.request_connect.emit(n, bool(chk)))
            lbl_state = QtWidgets.QLabel("--")
            lbl_wl = QtWidgets.QLabel("MC: -- THz")
            btn_act = QtWidgets.QPushButton("Activate CD")
            btn_act.setCheckable(True)
            btn_act.setMinimumWidth(140)
            btn_act.clicked.connect(lambda chk, n=name: self.request_activate.emit(n, bool(chk)))
            for col, w in enumerate((lbl, spin, btn_conn, lbl_state, lbl_wl, btn_act)):
                grid.addWidget(w, r, col)
            grid.setColumnStretch(4, 1)
            self._rows[name] = dict(spin=spin, conn=btn_conn, state=lbl_state, wl=lbl_wl, act=btn_act)

    def update_snapshot(self, snap: dict):
        for name, s in snap.items():
            w = self._rows.get(name)
            if w is None:
                continue
            spin = w["spin"]
            if not spin.hasFocus():
                spin.blockSignals(True)
                spin.setValue(int(s.get("wlm_port", 0)))
                spin.blockSignals(False)
            spin.setEnabled(not s.get("active", False))
            w["conn"].blockSignals(True)
            w["conn"].setChecked(bool(s.get("want_connected", True)))
            w["conn"].blockSignals(False)

            if s.get("connected"):
                txt, col = "Connected", "#27ae60"
            else:
                txt, col = "Disconnected", "#c0392b"
            err = s.get("last_error") or ""
            w["state"].setText(f"<span style='color:{col}'>{txt}</span>")
            w["state"].setToolTip(err)

            lam, mism = s.get("wavelength_nm"), s.get("mismatch_mhz")
            if lam is None or lam <= 0:
                wl_txt = "MC: -- THz"
                w["wl"].setToolTip("Matisse Commander's wavemeter reading (THz), and HF minus MC (MHz)")
            else:
                wl_txt = f"MC: {thz_to_nm(lam):.6f} THz"   # c/x is its own inverse
                w["wl"].setToolTip(f"Matisse Commander reports {lam:.6f} nm (vacuum assumed); "
                                   f"HF minus MC in MHz. Setpoints are sent to CounterDrift as nm.")
            if mism is not None:
                wl_txt += f"  (HF-MC {mism:+.0f} MHz)"
            if s.get("setpoint_pending"):
                wl_txt += "  <b style='color:#c0392b'>SP NOT DELIVERED (retrying)</b>"
            if err:
                wl_txt += f"  <span style='color:#c0392b'>{err[:80]}</span>"
            w["wl"].setText(wl_txt)

            unconf = bool(s.get("off_unconfirmed", False))
            act = bool(s.get("active", False)) or unconf   # checked -> a click retries OFF
            btn = w["act"]
            btn.blockSignals(True)
            btn.setChecked(act)
            if unconf:
                btn.setText("CD OFF? UNCONFIRMED")
            elif act and s.get("restored"):
                btn.setText("CD ACTIVE? (restored)")
            else:
                btn.setText("CD ACTIVE" if act else "Activate CD")
            btn.setStyleSheet(f"font-weight: bold; background-color: "
                              f"{'#2980b9' if act else '#7f8c8d'}; color: white;")
            btn.blockSignals(False)


# ---------------------------------------------------------------------------
# Wiring into HF_Locking (called by main_wlm.py; pinned by tests)
# ---------------------------------------------------------------------------

def wire_into_hf(worker_wlm, cd, panel=None):
    """Connect CounterDrift worker(s) to WavemeterWorker (and the GUI panel).
    `cd` is a MatisseCDGroup or a single MatisseCDWorker.

    Every connection is QUEUED: the objects live on different threads in
    production, and queued delivery is what keeps DLL calls on the WLM
    worker thread and each Matisse socket on its laser's thread.
    """
    q = QtCore.Qt.QueuedConnection
    for w in getattr(cd, "workers", [cd]):
        worker_wlm.setpoint_committed.connect(w.handle_setpoint_committed, q)
        w.request_hf_lock.connect(worker_wlm.handle_lock_toggle, q)
        if panel is not None:
            panel.request_channel.connect(w.handle_set_channel, q)
            panel.request_connect.connect(w.handle_connect, q)
            panel.request_activate.connect(w.handle_activate, q)


# ---------------------------------------------------------------------------
# Read-only bring-up probe
# ---------------------------------------------------------------------------

def _probe(host: str, port: int) -> int:
    """READ-ONLY: connect, ask the CounterDrift wavelength, close cleanly."""
    c = MatisseCommanderClient(host, port)
    try:
        c.connect()
    except OSError as e:
        print(f"CONNECT FAILED {host}:{port}: {e} "
              f"(Matisse Commander running? Communication Options -> Enable Server?)")
        return 1
    try:
        for cmd in ("MCP_WM_GET_WAVELENGTH",):
            try:
                print(f"  #SERVER {cmd:28s} -> {c.mcp(cmd)!r}")
            except (MatisseError, OSError) as e:
                print(f"  #SERVER {cmd:28s} -> <error: {e}>")
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe":
        host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
        port = int(sys.argv[3]) if len(sys.argv) > 3 else 30000
        sys.exit(_probe(host, port))
    print(__doc__)
