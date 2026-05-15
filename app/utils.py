"""
app/utils.py — shared platform utilities.
"""

import os
import sys
import contextlib


@contextlib.contextmanager
def quiet_macos():
    """
    Redirect fd 2 to /dev/null for the duration of the block.

    Suppresses C-level stderr noise emitted by macOS system libraries
    (libsystem_trace.dylib / CoreAnalytics) when the ONNX runtime or
    matplotlib's font renderer runs on macOS:
        'Context leak detected, CoreAnalytics returned false'

    No-op on Linux / Windows — safe to use unconditionally.
    """
    if sys.platform != "darwin":
        yield
        return
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_fd   = os.dup(2)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)
    try:
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
