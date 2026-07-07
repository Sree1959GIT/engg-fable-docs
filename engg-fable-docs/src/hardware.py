"""src/hardware.py — Detect the host hardware and derive an orchestration profile.

The pipeline sizes its agent pools from what the machine can actually do:
  - diagram/analysis workers scale with CPU cores (pure Python, no LLM)
  - LLM workers stay pinned to the llama.cpp --parallel slot count
  - LLM timeouts stretch on machines without a discrete GPU (iGPU prefill
    is minutes-slow on big prompts; killing the request just wastes work)

No third-party deps: ctypes on Windows, sysconf on POSIX.
"""
import os
import platform
import subprocess
from functools import lru_cache

from src.config import LLM_MAX_CONCURRENT, LLM_TIMEOUT_SEC


def _total_ram_gb() -> float:
    try:
        if platform.system() == "Windows":
            import ctypes

            class _MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = _MEM()
            m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 2 ** 30
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except Exception:
        return 8.0


def _gpu_kind() -> str:
    """'nvidia' | 'igpu' | 'unknown' — a coarse hint, never a hard failure."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True,
                           text=True, timeout=3)
        if r.returncode == 0 and "GPU" in r.stdout:
            return "nvidia"
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            r = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, text=True, timeout=5)
            names = r.stdout.lower()
            if "nvidia" in names or "radeon rx" in names:
                return "nvidia"
            if "intel" in names or "iris" in names or "uhd" in names:
                return "igpu"
        except Exception:
            pass
    return "unknown"


@lru_cache(maxsize=1)
def detect_profile() -> dict:
    """One-shot hardware probe → agent-pool sizing. Cached per process."""
    cores = os.cpu_count() or 4
    ram = _total_ram_gb()
    gpu = _gpu_kind()

    # CPU-bound agents (diagrams, analysis): leave headroom for the OS + LLM
    diagram_workers = max(2, min(8, cores - 2))
    # LLM-bound agents: never exceed the server's --parallel slots
    llm_workers = max(1, LLM_MAX_CONCURRENT)
    # Weak-GPU machines need patient timeouts: prefill dominates latency
    timeout = LLM_TIMEOUT_SEC if gpu == "nvidia" else max(LLM_TIMEOUT_SEC, 180)
    if ram < 12:
        timeout = max(timeout, 240)

    return {
        "platform": platform.system(),
        "cores": cores,
        "ram_gb": round(ram, 1),
        "gpu": gpu,
        "diagram_workers": diagram_workers,
        "llm_workers": llm_workers,
        "llm_timeout": timeout,
    }


def describe_profile(p: dict = None) -> str:
    p = p or detect_profile()
    gpu_txt = {"nvidia": "discrete GPU", "igpu": "integrated GPU"}.get(
        p["gpu"], "GPU unknown")
    return (f"{p['platform']} · {p['cores']} cores · {p['ram_gb']} GB RAM · "
            f"{gpu_txt} → {p['diagram_workers']} diagram workers, "
            f"{p['llm_workers']} LLM workers, {p['llm_timeout']}s LLM timeout")
