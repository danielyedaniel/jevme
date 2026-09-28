"""The ordered task queue in main: order, cancel epochs, no stranded units (no UI, no network)."""
from __future__ import annotations

import collections
import threading
import time

import pytest

from jevme import main as M


class FakeRouter:
    def __init__(self, delay: float = 0.0):
        self.ran: list[str] = []
        self.delay = delay

    def run_tool(self, name, args, label, prog, **kwargs):
        time.sleep(self.delay)
        self.ran.append(name)

    def execute_clause(self, clause, prog):
        time.sleep(self.delay)
        self.ran.append(clause)

    def is_settle_tool(self, name):
        return False


class Nop:
    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(M, "dispatch_main", lambda fn, args: fn(*args))
    d = M.AppDelegate.alloc().init()
    d.task_q = collections.deque()
    d.task_lock = threading.Lock()
    d.task_worker = None
    d.task_running = False
    d.cancel_epoch = 0
    d.router = FakeRouter()
    d.agent = Nop()
    d.overlay = Nop()
    from jevme.watch import Watcher
    d.watcher = Watcher()
    return d


def _wait(d, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with d.task_lock:
            if not d.task_running and not d.task_q:
                return
        time.sleep(0.01)
    raise AssertionError("queue did not drain")


def test_runs_in_spoken_order(app):
    for c in ["open chrome", "go to amazon", "search rice cooker"]:
        app._on_stream(c)
    _wait(app)
    assert app.router.ran == ["open chrome", "go to amazon", "search rice cooker"]


def test_stop_with_idle_queue_does_not_eat_next_command(app):
    # Logged/reviewed bug: "stop" left a flag set, so the next command was silently skipped.
    app._on_cancel()
    app._on_commit("open_app", {"app": "Safari"}, "open app: Safari")
    _wait(app)
    assert app.router.ran == ["open_app"]


def test_stop_drops_queued_work_but_not_later_work(app):
    app.router.delay = 0.05
    for c in ["a", "b", "c", "d"]:
        app._on_general(c)
    time.sleep(0.02)          # "a" is running
    app._on_cancel()          # b, c, d are dropped
    app._on_general("after")
    _wait(app)
    assert "after" in app.router.ran
    assert not {"c", "d"} & set(app.router.ran)


def test_no_stranded_unit_under_rapid_enqueue(app):
    # Reviewed race: a unit enqueued while the worker is exiting sat unrun until the next command.
    for i in range(200):
        app._on_general(f"c{i}")
        if i % 7 == 0:
            time.sleep(0.001)
    _wait(app)
    assert len(app.router.ran) == 200


def test_workflow_uses_admitted_queue_epoch_so_concurrent_stop_is_not_lost(app, tmp_path):
    from jevme.workflows import Workflow, WorkflowStore, tool_unit

    slack, jira = tool_unit("open_app", {"app": "Slack"}), tool_unit("open_app", {"app": "Jira"})
    assert slack is not None and jira is not None
    app.workflow_store = WorkflowStore(tmp_path / "workflows.json")
    app.workflow_store.workflows.append(Workflow("morning", "morning", (slack, jira)))
    app.agent.cancelled = True
    app._active_epoch = app.cancel_epoch
    app.cancel_epoch += 1  # stop lands after _drain admitted the run_workflow queue unit
    with pytest.raises(RuntimeError, match="workflow cancelled"):
        app._run_workflow("morning")
    assert app.router.ran == []


def test_workflow_candidate_mining_runs_off_tick_thread(app, monkeypatch):
    from jevme import config

    entered, release, threads = threading.Event(), threading.Event(), []

    class Store:
        @staticmethod
        def needs_finalization():
            return True

        def finalize_candidate(self):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(timeout=2)
            return None

    monkeypatch.setattr(config, "WORKFLOW_LEARNING", True)
    app.workflow_store = Store()
    caller = threading.get_ident()
    app._check_workflow_candidate()
    assert entered.wait(timeout=2)
    assert threads == [threads[0]] and threads[0] != caller
    assert app._workflow_check_busy
    release.set()
    end = time.monotonic() + 2
    while app._workflow_check_busy and time.monotonic() < end:
        time.sleep(0.01)
    assert not app._workflow_check_busy


def test_termination_persists_unfinalized_workflow_history(app):
    saved = []
    app.workflow_store = type("Store", (), {"save": lambda self: saved.append(True)})()
    app.applicationWillTerminate_(None)
    assert saved == [True]
