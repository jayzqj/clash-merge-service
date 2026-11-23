"""Thread-safe live progress. Estimates are advisory, never schedule deadlines."""
import threading
import time


class RunProgress:
    def __init__(self, previous=None, rounds=3, workers=1):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.phase = "拉取订阅与规则"
        self.done = 0
        self.total = 0
        self.previous = previous or {}
        self.rounds = rounds
        self.workers = workers
        self.check_started = None

    def update(self, *, phase=None, units=0, total=None, add_total=0):
        with self.lock:
            if phase: self.phase = phase
            if total is not None:
                self.total = total
                self.check_started = time.monotonic()
            self.total += add_total
            self.done += units

    def snapshot(self):
        with self.lock:
            elapsed = max(0, time.monotonic() - self.started)
            estimated = None
            basis = "等待检测样本"
            if self.done and self.total and self.check_started:
                check_elapsed = time.monotonic() - self.check_started
                estimated = elapsed + check_elapsed / self.done * max(0, self.total - self.done) + 10
                basis = "按当前采样进度估算；链路重试会调整"
            elif self.previous.get("ok") and self.previous.get("kind") != "reapply" and self.previous.get("duration_seconds"):
                estimated = self.previous["duration_seconds"] * (self.rounds + 1) / (self.previous.get("connectivity_rounds", 2) + 1)
                estimated *= self.previous.get("purity_workers", 1) / self.workers
                basis = "参考上一轮耗时与并发数"
            return {"elapsed_seconds": round(elapsed, 1),
                    "estimated_total_seconds": round(max(elapsed, estimated), 1) if estimated is not None else None,
                    "estimate_basis": basis, "phase": self.phase,
                    "completed_units": self.done, "total_units": self.total}
