"""Conservative worker recommendations from current Windows resources."""
import ctypes
import math
import os
import threading
import time

_lock = threading.Lock()
_cache = None
_cached_at = 0.0
GIB = 1024 ** 3


def recommend_workers(available_bytes, total_bytes, logical_cpus, cpu_percent):
    # Leave RAM for foreground apps and validation; do not fill all idle CPUs.
    reserve = max(2 * GIB, total_bytes * 0.1)
    memory_limit = max(1, int((available_bytes - reserve) // (256 * 1024 ** 2)))
    cpu_limit = max(1, math.floor(logical_cpus * (1 - cpu_percent / 100) / 2))
    return {
        "recommended_workers": max(1, min(8, memory_limit, cpu_limit)),
        "available_memory_gib": round(available_bytes / GIB, 2),
        "total_memory_gib": round(total_bytes / GIB, 2),
        "reserved_memory_gib": round(reserve / GIB, 2),
        "logical_cpus": logical_cpus,
        "cpu_percent": round(cpu_percent, 1),
        "memory_tight": available_bytes <= reserve + 256 * 1024 ** 2,
    }


def _sample_windows():
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total_phys", "avail_phys", "total_page", "avail_page",
                "total_virtual", "avail_virtual", "avail_extended")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
    kernel.GetSystemTimes.restype = wintypes.BOOL
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL

    def times():
        values = [wintypes.FILETIME() for _ in range(3)]
        if not kernel.GetSystemTimes(*(ctypes.byref(v) for v in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return [(v.dwHighDateTime << 32) | v.dwLowDateTime for v in values]

    first = times()
    time.sleep(0.25)
    second = times()
    idle, system, user = [b - a for a, b in zip(first, second)]
    total = system + user  # Windows kernel time includes idle time.
    if total <= 0:
        raise OSError("CPU sample unavailable")
    busy = max(0, min(100, 100 * (1 - idle / total)))
    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    return recommend_workers(memory.avail_phys, memory.total_phys, os.cpu_count() or 1, busy)


def resource_recommendation():
    global _cache, _cached_at
    if os.name != "nt":
        return {"available": False, "reason": "仅 Windows 桌面版提供本机资源建议"}
    with _lock:
        if _cache is not None and time.monotonic() - _cached_at < 5:
            return dict(_cache)
        try:
            value = {"available": True, **_sample_windows()}
        except OSError:
            value = {"available": False, "reason": "暂时无法读取本机资源，请稍后刷新"}
        _cache, _cached_at = value, time.monotonic()
        return dict(value)
