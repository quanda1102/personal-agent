"""
Ensure libonnxruntime is loaded before sherpa_onnx.

The sherpa-onnx wheel links with @rpath to libonnxruntime but does not always
ship or resolve it next to _sherpa_onnx.so. The onnxruntime Python wheel places
the dylib under onnxruntime/capi/. Preloading via ctypes registers it for the
process so dlopen from sherpa succeeds (macOS / Linux).
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

_done = False


def ensure_onnxruntime_loaded() -> None:
    global _done
    if _done:
        return
    _done = True
    try:
        import onnxruntime as ort
    except ImportError:
        return
    capi = Path(ort.__file__).resolve().parent / "capi"
    if not capi.is_dir():
        return
    if sys.platform == "darwin":
        candidates = sorted(capi.glob("libonnxruntime.*.dylib"))
    elif sys.platform.startswith("linux"):
        candidates = sorted(capi.glob("libonnxruntime.so*"))
    elif sys.platform == "win32":
        candidates = sorted(capi.glob("onnxruntime.dll"))
    else:
        return
    if not candidates:
        return
    try:
        ctypes.CDLL(str(candidates[0]), mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass
