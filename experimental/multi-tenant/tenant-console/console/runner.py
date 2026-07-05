"""Threaded provisioning runner that streams executor events over a queue.

The P1 executor (:func:`tenantconsole.steps.run_provision` /
:func:`tenantconsole.decommission.run_decommission`) is synchronous and reports
progress through an ``on_event(name, phase, result)`` callback. The console runs
one job at a time in a background thread and pushes each event onto a
thread-safe queue that the SSE endpoint drains — so the browser sees each step's
✓/✗ live, exactly like the Forge Console's subprocess stream.

Only one job runs at a time (``start`` raises if one is in flight), mirroring the
single-tenant, operator-at-the-keyboard model.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from tenantconsole.steps import StepResult

# Sentinel pushed once a job finishes; carries the final report.
_DONE = "__done__"


@dataclass
class RunState:
    kind: str = ""          # "provision" | "decommission"
    tenant: str = ""
    running: bool = False
    outcome: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    report: Optional[Dict[str, Any]] = None
    error: str = ""


class Runner:
    """Runs one provisioning/decommission job in a thread, streaming events."""

    def __init__(self) -> None:
        self._q: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.state = RunState()

    def is_running(self) -> bool:
        with self._lock:
            return self.state.running

    def start(self, kind: str, tenant: str, job: Callable[[Callable], Dict[str, Any]]) -> None:
        """Begin ``job`` in a background thread.

        *job* receives the ``on_event`` callback and must return the report dict
        (i.e. a thunk that calls run_provision/run_decommission with on_event).
        Raises ``RuntimeError`` if a job is already running.
        """
        with self._lock:
            if self.state.running:
                raise RuntimeError("a provisioning job is already running")
            # drain any stale events from a previous job
            while not self._q.empty():
                self._q.get_nowait()
            self.state = RunState(kind=kind, tenant=tenant, running=True)

        def on_event(name: str, phase: str, result: Optional[StepResult]) -> None:
            evt: Dict[str, Any] = {"name": name, "phase": phase}
            if result is not None:
                evt.update({"status": result.status, "detail": result.detail, "data": result.data})
                if phase == "end":
                    self.state.steps.append(
                        {"name": name, "status": result.status, "detail": result.detail}
                    )
            self._q.put(evt)

        def worker() -> None:
            try:
                report = job(on_event)
                self.state.report = report
                self.state.outcome = report.get("outcome", "")
            except Exception as exc:  # noqa: BLE001 — surfaced to the UI, never crashes the server
                self.state.error = str(exc)
                self.state.outcome = "error"
                self._q.put({"name": "error", "phase": "error", "detail": str(exc)})
            finally:
                self.state.running = False
                self._q.put({"name": _DONE, "phase": "done",
                             "outcome": self.state.outcome, "error": self.state.error})

        self._thread = threading.Thread(target=worker, name="tenant-console-job", daemon=True)
        self._thread.start()

    def subscribe(self):
        """Yield events until the terminal ``__done__`` event, then stop."""
        while True:
            try:
                evt = self._q.get(timeout=1.0)
            except queue.Empty:
                yield {"name": "", "phase": "keepalive"}
                continue
            yield evt
            if evt.get("name") == _DONE:
                return
