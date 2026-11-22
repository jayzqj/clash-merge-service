"""Manage only children started by this application, on Windows and Linux."""
import os
import signal
import subprocess
import threading

STOPPING = threading.Event()
_lock = threading.Lock()
_children = set()


def process_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def start_process(args, **kwargs):
    with _lock:
        if STOPPING.is_set():
            raise RuntimeError("application is shutting down")
        proc = subprocess.Popen(args, **process_options(), **kwargs)
        _children.add(proc)
        return proc


def stop_process(proc) -> None:
    try:
        if proc.poll() is None:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
    except ProcessLookupError:
        pass
    finally:
        with _lock:
            _children.discard(proc)


def stop_all() -> None:
    STOPPING.set()
    with _lock:
        children = list(_children)
    for proc in children:
        stop_process(proc)


def run_process(args, *, timeout, env=None):
    proc = start_process(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", env=env)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        stop_process(proc)
        stdout, stderr = proc.communicate(timeout=5)
        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
    finally:
        stop_process(proc)
