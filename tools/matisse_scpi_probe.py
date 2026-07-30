#!/usr/bin/env python
"""READ-ONLY SCPI probe of the Matisse Commander Network Server.

Uses the CORRECT LabVIEW length-prefixed framing (per Sirah's own reference
client nelsond/sirah-matisse-commander):
  send:  struct.pack('>L', len(payload)) + payload   (ASCII, no newline)
  recv:  n = unpack('>L', recv(4)); body = recv(n)
  close: send 'Close_Network_Connection'; sleep 0.3s   (required by MC)

Sends ONLY query commands (all end in '?') that READ state. It does NOT move
any actuator or change any setting. Safe against a live, locked laser.

Enable the channel first: Matisse Commander > Communication Options >
  Network Client = Use VISA, Network Server = Enable Server (port 30000).
Two Matisse Commander instances on one PC must use distinct ports.

Usage:  python matisse_scpi_probe.py [host] [port]   (defaults 127.0.0.1 30000)
"""
import socket, struct, sys, time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 30000

QUERIES = [
    "IDN?",                          # unreliable on some units (serial bug)
    "MOTBI:WL?",                     # birefringent filter wavelength (coarse tune)
    "SCAN:DEVICE?",                  # 0=none 1=slow piezo 2=ref cell (expect 2 on C-S)
    "SCAN:STATUS?",                  # RUN/STOP
    "SCAN:NOW?",                     # current scan position
    "SCAN:LOWERLIMIT?",
    "SCAN:UPPERLIMIT?",
    "SCAN:RISINGSPEED?",
    "REFERENCECELL:NOW?",            # current ref-cell piezo position
    "FASTPIEZO:LOCK?",               # TRUE iff tweeter in 5..95% range
    "FASTPIEZO:CONTROLSTATUS?",      # RUN/STOP
    "FASTPIEZO:NOW?",                # tweeter position [0,1]
    "SLOWPIEZO:CONTROLSTATUS?",
    "SLOWPIEZO:NOW?",
]

def _recvall(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("connection closed mid-read")
        buf += chunk
    return buf

def send_cmd(sock, cmd):
    payload = cmd.encode("ascii")
    sock.sendall(struct.pack(">L", len(payload)) + payload)

def recv_resp(sock):
    n = struct.unpack(">L", _recvall(sock, 4))[0]
    return "" if n == 0 else _recvall(sock, n).decode("ascii", "replace")

def main():
    print(f"connecting to Network Server {HOST}:{PORT} (length-prefixed framing) ...")
    try:
        s = socket.create_connection((HOST, PORT), timeout=3.0)
    except Exception as e:
        print(f"CONNECT FAILED: {e}")
        print("-> Network Server not enabled/listening, or Matisse Commander not running.")
        return
    s.settimeout(2.0)
    print("connected. sending READ-ONLY queries:\n")
    try:
        for cmd in QUERIES:
            try:
                send_cmd(s, cmd)
                resp = recv_resp(s)
            except Exception as e:
                resp = f"<error: {e}>"
            print(f"  {cmd:30s} -> {resp!r}")
    finally:
        # graceful close required by Matisse Commander (avoids server Error 56)
        try:
            send_cmd(s, "Close_Network_Connection")
            time.sleep(0.3)
        except Exception:
            pass
        s.close()
    print("\ndone (read-only; nothing was changed; connection closed cleanly).")

if __name__ == "__main__":
    main()
