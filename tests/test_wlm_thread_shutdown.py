"""S1: WavemeterWorker thread must actually stop on shutdown.

closeEvent invokes worker.stop() (queued) and then BLOCKS the GUI thread in
thread.wait(). If finished->quit is an auto (= queued, the QThread object
lives on the GUI thread) connection, quit() can only run once the GUI thread
is free again -- so wait() always times out, and config.save_config() then
calls the DLL from the GUI thread while the worker thread still exists
(violates the DLL Thread Safety Rule in CLAUDE.md).

Real WavemeterWorker + real QThread; DLL is the autospec stub.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.conftest import require_workers, make_wlm_stub  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt5 import QtWidgets  # noqa: PLC0415
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_S1_wlm_worker_thread_stops_within_timeout(qapp):
    w, _ = require_workers()
    from PyQt5 import QtCore  # noqa: PLC0415

    thread = QtCore.QThread()
    worker = w.WavemeterWorker(make_wlm_stub(), w.SharedExperimentState())
    w.bind_worker_thread(worker, thread, worker.start_polling)   # as main_wlm.py
    thread.start()
    try:
        time.sleep(0.2)                                          # polling running
        t0 = time.monotonic()
        ok = w.stop_worker_thread(worker, thread, timeout_ms=1000)  # as closeEvent
        dt = time.monotonic() - t0
        assert ok is True, "thread.wait() timed out: finished->quit never ran"
        assert not thread.isRunning()
        assert dt < 0.5, f"stop took {dt:.2f}s"
    finally:
        if thread.isRunning():
            thread.quit()
            thread.wait(2000)
