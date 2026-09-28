"""Mine and replay repeated routines using privacy-safe structured descriptors only."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import policy, ui_memory

log = logging.getLogger("jevme.workflows")

PATH = Path.home() / ".config" / "jevme" / "workflows.json"
SCHEMA_VERSION = 2
MIN_WORKFLOW_UNITS = 2
MAX_WORKFLOW_UNITS = 6
MAX_WORKFLOWS = 40
MIN_OCCURRENCES = 3
EPISODE_IDLE_S = 5 * 60
MAX_HISTORY_EVENTS = 240
PENDING_TTL_S = 24 * 60 * 60
DISMISS_FOR_S = 7 * 24 * 60 * 60
MAX_DISMISSED = 120
MAX_NAME_LEN = 60
MAX_ID_LEN = 48

# Only arguments that are identifiers from a bounded vocabulary are stored. Arbitrary text, paths,
# queries, URLs, output, screen contents, and accessibility labels never enter this file.
_TOOL_ARGS: dict[str, tuple[str, frozenset[str] | None]] = {
    "open_app": ("app", None),
    "open_site": ("site", None),
    "open_folder": ("folder", frozenset({"downloads", "desktop", "documents", "home",
                                            "applications", "pictures"})),
    "set_volume": ("level", frozenset({"mute", "unmute", "louder", "quieter", "max"})),
}
_ARGLESS_TOOLS = frozenset({
    "media_play_pause", "media_next", "media_previous", "new_tab", "close_tab",
    "reload_page", "go_back", "select_all", "copy", "undo", "save",
    "minimize_window", "fullscreen_window", "hide_others", "toggle_dark_mode",
    "take_screenshot",
})
_SAFE_TOOLS = frozenset(_TOOL_ARGS) | _ARGLESS_TOOLS
_NOISE_TOOLS = frozenset({"reload_page", "go_back", "copy", "undo", "select_all"})
_RECIPE_OPS = frozenset({"open_app", "click", "menu"})
_PRIVATE = re.compile(r"\b(password|passcode|secret|token|api[ -]?key|credit card|cvv|ssn)\b", re.I)
_IDENTIFIER = re.compile(r"[\w .+&'()-]{1,80}\Z", re.UNICODE)
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\.)*[a-z0-9][a-z0-9-]{0,62}\Z", re.I)
_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _clean_text(value: str, limit: int) -> str:
    return " ".join(str(value).split())[:limit]


def _finite_number(value: Any, *, minimum: float = 0) -> float:
    out = float(value)
    if not math.isfinite(out) or out < minimum:
        raise ValueError("invalid number")
    return out


def recipe_key(goal: str, app0: str, steps: list[ui_memory.RecipeStep]) -> str:
    """Return an opaque stable reference to a task-memory recipe identity.

    TaskMemory updates the steps of an existing (normalized goal, app) recipe in place, so steps
    deliberately do not participate in this key. Replay resolves the current steps and revalidates them.
    """
    del steps
    raw = {"goal": ui_memory._norm(goal), "app0": _clean_text(app0, 80)}
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class WorkflowUnit:
    kind: str
    tool_name: str = ""
    args: dict[str, str] = field(default_factory=dict)
    recipe_key: str = ""

    @property
    def label(self) -> str:
        if self.kind == "recipe":
            return "replay saved task"
        value = next(iter(self.args.values()), "")
        return f"{self.tool_name.replace('_', ' ')}: {value}" if value else self.tool_name.replace("_", " ")

    def signature(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "tool":
            return {"kind": "tool", "tool_name": self.tool_name, "args": dict(sorted(self.args.items()))}
        if self.kind == "recipe":
            return {"kind": "recipe", "recipe_key": self.recipe_key}
        raise ValueError("unknown workflow unit")

    @classmethod
    def from_dict(cls, data: Any) -> WorkflowUnit:
        if not isinstance(data, dict) or set(data) - {"kind", "tool_name", "args", "recipe_key"}:
            raise ValueError("invalid workflow unit fields")
        if data.get("kind") == "tool":
            name, args = data.get("tool_name"), data.get("args")
            if not isinstance(name, str) or not isinstance(args, dict) or not eligible_tool(name, args):
                raise ValueError("unsafe tool unit")
            return cls("tool", tool_name=name, args=dict(args))
        if data.get("kind") == "recipe" and isinstance(data.get("recipe_key"), str):
            key = data["recipe_key"]
            if not _HASH.fullmatch(key):
                raise ValueError("invalid recipe reference")
            return cls("recipe", recipe_key=key)
        raise ValueError("unknown workflow unit")


@dataclass
class HistoryEvent:
    unit: WorkflowUnit
    timestamp: float
    episode: int


@dataclass
class WorkflowCandidate:
    units: tuple[WorkflowUnit, ...]
    occurrences: int
    detected: float = field(default_factory=time.time)

    @property
    def signature(self) -> str:
        return _sequence_signature(self.units)

    @property
    def summary(self) -> str:
        return " → ".join(unit.label for unit in self.units)


@dataclass
class Workflow:
    id: str
    name: str
    units: tuple[WorkflowUnit, ...]
    created: float = field(default_factory=time.time)
    hits: int = 0

    @property
    def signature(self) -> str:
        return _sequence_signature(self.units)


class WorkflowPersistenceError(RuntimeError):
    pass


def _sequence_signature(units: tuple[WorkflowUnit, ...]) -> str:
    return json.dumps([unit.signature() for unit in units], separators=(",", ":"))


def _safe_name(name: Any) -> str:
    if not isinstance(name, str) or len(name) > MAX_NAME_LEN * 2:
        return ""
    name = _clean_text(name, MAX_NAME_LEN).strip(" .?!,:;-_")
    return name if len(name) >= 2 and re.search(r"[a-z0-9]", name, re.I) else ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "workflow"


def eligible_tool(name: str, args: dict[str, str]) -> bool:
    """Accept a small, strictly shaped descriptor; output/labels are intentionally not accepted."""
    if name not in _SAFE_TOOLS or not isinstance(args, dict):
        return False
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in args.items()):
        return False
    if policy.consequential_action(name, *args.keys(), *args.values()):
        return False
    if name in _ARGLESS_TOOLS:
        return not args
    key, choices = _TOOL_ARGS[name]
    if set(args) != {key}:
        return False
    value = args[key]
    if _PRIVATE.search(value) or (choices is not None and value not in choices):
        return False
    if name == "open_site":
        return bool(_DOMAIN.fullmatch(value)) and len(value) <= 80
    return bool(_IDENTIFIER.fullmatch(value)) and len(value) <= 80


def eligible_recipe(goal: str, steps: list[ui_memory.RecipeStep]) -> bool:
    """Validate in-memory content. Only its opaque hash may enter workflow storage."""
    if not isinstance(goal, str) or not goal or len(goal) > 500 or ui_memory.is_volatile_goal(goal):
        return False
    if _PRIVATE.search(goal) or policy.consequential_action(goal):
        return False
    if not isinstance(steps, list) or not 1 <= len(steps) <= 24:
        return False
    for step in steps:
        if not isinstance(step, ui_memory.RecipeStep) or step.op not in _RECIPE_OPS:
            return False
        if any(len(value) > 160 for value in (step.role, step.label, step.arg)):
            return False
        if _PRIVATE.search(" ".join((step.role, step.label, step.arg))):
            return False
        if policy.consequential_action(step.op, step.role, step.label, step.arg):
            return False
        if step.op == "click" and (not step.label or not ui_memory.is_stable_target(step.role, step.label)):
            return False
    return True


def tool_unit(name: str, args: dict[str, str]) -> WorkflowUnit | None:
    if not eligible_tool(name, args):
        return None
    return WorkflowUnit("tool", tool_name=name, args=dict(args))


def recipe_unit(goal: str, app0: str, steps: list[ui_memory.RecipeStep]) -> WorkflowUnit | None:
    if not eligible_recipe(goal, steps):
        return None
    return WorkflowUnit("recipe", recipe_key=recipe_key(goal, app0, steps))


class WorkflowStore:
    def __init__(self, path: Path = PATH) -> None:
        self.path = path
        self.workflows: list[Workflow] = []
        self.history: list[HistoryEvent] = []
        self.pending: WorkflowCandidate | None = None
        self.dismissed: dict[str, float] = {}
        self._writable = True
        self._finalized_episode = 0
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001
            self._writable = False
            log.warning("preserving malformed workflows file: %s", error)
            return
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            self._writable = False
            log.warning("preserving unsupported workflows schema")
            return
        shapes = (("workflows", list), ("history", list), ("dismissed", dict))
        if (any(key in data and not isinstance(data[key], expected) for key, expected in shapes)
                or (data.get("pending") is not None and not isinstance(data.get("pending"), dict))):
            self._writable = False
            log.warning("preserving malformed workflows schema")
            return

        raw_workflows = data.get("workflows", [])
        if isinstance(raw_workflows, list):
            for item in raw_workflows[:MAX_WORKFLOWS * 2]:
                try:
                    workflow = self._parse_workflow(item)
                    if workflow.id not in {old.id for old in self.workflows} and len(self.workflows) < MAX_WORKFLOWS:
                        self.workflows.append(workflow)
                except (KeyError, TypeError, ValueError):
                    log.warning("skipping malformed workflow entry")

        raw_history = data.get("history", [])
        if isinstance(raw_history, list):
            for item in raw_history[-MAX_HISTORY_EVENTS:]:
                try:
                    timestamp = _finite_number(item["timestamp"])
                    episode = int(item["episode"])
                    if episode < 1:
                        raise ValueError("invalid episode")
                    self.history.append(HistoryEvent(WorkflowUnit.from_dict(item["unit"]), timestamp, episode))
                except (KeyError, TypeError, ValueError, OverflowError):
                    log.warning("skipping malformed workflow history entry")

        try:
            finalized = int(data.get("finalized_episode", 0))
            newest_episode = self.history[-1].episode if self.history else 0
            if not 0 <= finalized <= newest_episode:
                raise ValueError("invalid finalized episode")
            self._finalized_episode = finalized
        except (TypeError, ValueError, OverflowError):
            log.warning("ignoring malformed finalized episode")

        raw_pending = data.get("pending")
        if raw_pending is not None:
            try:
                units = self._parse_units(raw_pending["units"])
                detected = _finite_number(raw_pending["detected"])
                occurrences = int(raw_pending["occurrences"])
                if occurrences < MIN_OCCURRENCES:
                    raise ValueError("invalid occurrence count")
                if time.time() - detected <= PENDING_TTL_S:
                    self.pending = WorkflowCandidate(units, occurrences, detected)
                elif self.history:
                    # An ignored candidate stays expired across restarts instead of immediately being
                    # rediscovered from the same last episode.
                    self._finalized_episode = self.history[-1].episode
            except (KeyError, TypeError, ValueError, OverflowError):
                log.warning("skipping malformed pending workflow")

        raw_dismissed = data.get("dismissed", {})
        if isinstance(raw_dismissed, dict):
            valid: list[tuple[str, float]] = []
            for signature, when in raw_dismissed.items():
                try:
                    if not isinstance(signature, str) or len(signature) > 5000:
                        raise ValueError("invalid dismissal")
                    timestamp = _finite_number(when)
                    if time.time() - timestamp < DISMISS_FOR_S:
                        valid.append((signature, timestamp))
                except (TypeError, ValueError):
                    continue
            self.dismissed = dict(sorted(valid, key=lambda item: item[1], reverse=True)[:MAX_DISMISSED])

    def _parse_units(self, raw: Any) -> tuple[WorkflowUnit, ...]:
        if not isinstance(raw, list) or not MIN_WORKFLOW_UNITS <= len(raw) <= MAX_WORKFLOW_UNITS:
            raise ValueError("invalid workflow length")
        return tuple(WorkflowUnit.from_dict(unit) for unit in raw)

    def _parse_workflow(self, item: Any) -> Workflow:
        if not isinstance(item, dict):
            raise ValueError("invalid workflow")
        ident, name = item["id"], _safe_name(item["name"])
        if not isinstance(ident, str) or len(ident) > MAX_ID_LEN or not _ID.fullmatch(ident) or not name:
            raise ValueError("invalid workflow identity")
        created = _finite_number(item.get("created", 0))
        hits = int(item.get("hits", 0))
        if not 0 <= hits <= 2_000_000_000:
            raise ValueError("invalid hits")
        return Workflow(ident, name, self._parse_units(item["units"]), created, hits)

    def _data(self) -> dict[str, Any]:
        pending = None
        if self.pending:
            pending = {"units": [unit.to_dict() for unit in self.pending.units],
                       "occurrences": self.pending.occurrences, "detected": self.pending.detected}
        return {
            "version": SCHEMA_VERSION,
            "workflows": [{"id": workflow.id, "name": workflow.name,
                           "units": [unit.to_dict() for unit in workflow.units],
                           "created": workflow.created, "hits": workflow.hits}
                          for workflow in self.workflows[:MAX_WORKFLOWS]],
            "history": [{"unit": event.unit.to_dict(), "timestamp": event.timestamp,
                         "episode": event.episode} for event in self.history[-MAX_HISTORY_EVENTS:]],
            "pending": pending,
            "dismissed": dict(list(self.dismissed.items())[:MAX_DISMISSED]),
            "finalized_episode": self._finalized_episode,
        }

    def save(self) -> bool:
        if not self._writable:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._data(), indent=2), encoding="utf-8")
            temporary.replace(self.path)
            return True
        except Exception as error:  # noqa: BLE001
            log.warning("workflow save failed: %s", error)
            return False

    def record(self, unit: WorkflowUnit, now: float | None = None) -> None:
        """Record an eligible completed unit; publication waits for an idle boundary."""
        if unit.kind == "tool" and not eligible_tool(unit.tool_name, unit.args):
            return
        if unit.kind == "recipe" and not _HASH.fullmatch(unit.recipe_key):
            return
        now = time.time() if now is None else now
        with self._lock:
            last = self.history[-1] if self.history else None
            new_episode = bool(last and now - last.timestamp > EPISODE_IDLE_S)
            episode = last.episode if last and not new_episode else (
                last.episode + 1 if last else 1)
            self.history.append(HistoryEvent(unit, now, episode))
            self.history = self.history[-MAX_HISTORY_EVENTS:]
            self.dismissed = dict(sorted(
                ((sig, when) for sig, when in self.dismissed.items() if now - when < DISMISS_FOR_S),
                key=lambda item: item[1], reverse=True)[:MAX_DISMISSED])
            # Persist on the episode boundary, not after every command on the ordered worker.
            # finalize_candidate() persists the last episode once it becomes idle.
            if new_episode:
                self.save()

    def finalize_candidate(self, now: float | None = None) -> WorkflowCandidate | None:
        """Publish after idle so an A-B prefix cannot pre-empt A-B-C."""
        now = time.time() if now is None else now
        with self._lock:
            changed = False
            if self.pending and now - self.pending.detected > PENDING_TTL_S:
                self.pending = None
                changed = True
            if not self.history or now - self.history[-1].timestamp <= EPISODE_IDLE_S:
                if changed:
                    self.save()
                return None
            episode = self.history[-1].episode
            if episode == self._finalized_episode:
                if changed:
                    self.save()
                return None
            self._finalized_episode = episode
            found = self._mine(now)
            if found and (self.pending is None or found.signature != self.pending.signature):
                self.pending = found
                self.save()
                return found
            # This is also the persistence boundary for an episode with no candidate.
            self.save()
            return None

    def needs_finalization(self, now: float | None = None) -> bool:
        """Cheap timer-thread gate before dispatching mining and persistence to a worker."""
        now = time.time() if now is None else now
        with self._lock:
            if self.pending and now - self.pending.detected > PENDING_TTL_S:
                return True
            return bool(self.history and now - self.history[-1].timestamp > EPISODE_IDLE_S
                        and self.history[-1].episode != self._finalized_episode)

    def _mine(self, now: float) -> WorkflowCandidate | None:
        saved = [[unit.signature() for unit in workflow.units] for workflow in self.workflows]
        event_signatures = [event.unit.signature() for event in self.history]
        for length in range(MAX_WORKFLOW_UNITS, MIN_WORKFLOW_UNITS - 1, -1):
            winners: list[tuple[int, WorkflowCandidate]] = []
            seen: set[str] = set()
            for start in range(len(self.history) - length + 1):
                window = self.history[start:start + length]
                if window[0].episode != window[-1].episode:
                    continue
                units = tuple(event.unit for event in window)
                unit_signatures = event_signatures[start:start + length]
                signature = json.dumps(unit_signatures, separators=(",", ":"))
                covered = any(any(existing[i:i + length] == unit_signatures
                                  for i in range(len(existing) - length + 1)) for existing in saved)
                if signature in seen or covered or signature in self.dismissed:
                    continue
                seen.add(signature)
                if len(set(unit_signatures)) < 2 or all(
                        unit.kind == "tool" and unit.tool_name in _NOISE_TOOLS for unit in units):
                    continue
                occurrences, last_end = self._occurrence_stats(
                    units, event_signatures, wanted_signatures=unit_signatures)
                if occurrences >= MIN_OCCURRENCES:
                    winners.append((last_end, WorkflowCandidate(units, occurrences, now)))
            if winners:
                return max(winners, key=lambda item: (item[0], item[1].occurrences))[1]
        return None

    def _occurrence_stats(self, units: tuple[WorkflowUnit, ...],
                          event_signatures: list[str] | None = None, *,
                          wanted_signatures: list[str] | None = None) -> tuple[int, int]:
        wanted = wanted_signatures or [unit.signature() for unit in units]
        count, index, last_end = 0, 0, -1
        signatures = event_signatures or [event.unit.signature() for event in self.history]
        while index <= len(self.history) - len(wanted):
            chunk = self.history[index:index + len(wanted)]
            if (chunk[0].episode == chunk[-1].episode
                    and signatures[index:index + len(wanted)] == wanted):
                count += 1
                last_end = index + len(wanted)
                index += len(wanted)
            else:
                index += 1
        return count, last_end

    def _non_overlapping_count(self, units: tuple[WorkflowUnit, ...]) -> int:
        return self._occurrence_stats(units)[0]

    def save_pending(self, name: str) -> tuple[Workflow | None, str]:
        name = _safe_name(name)
        if not name:
            return None, "Give the workflow a short name"
        with self._lock:
            if self.pending is None:
                return None, "No routine is waiting to be saved"
            if len(self.workflows) >= MAX_WORKFLOWS:
                return None, "The saved workflow limit has been reached"
            if any(workflow.name.casefold() == name.casefold() for workflow in self.workflows):
                return None, f'A workflow named "{name}" already exists'
            if any(workflow.signature == self.pending.signature for workflow in self.workflows):
                return None, "That routine is already saved"
            used, ident, suffix = {workflow.id for workflow in self.workflows}, _slug(name), 2
            base = ident
            while ident in used:
                ident = f"{base[:MAX_ID_LEN - len(str(suffix)) - 1]}-{suffix}"
                suffix += 1
            previous = self.pending
            workflow = Workflow(ident, name, previous.units)
            self.workflows.append(workflow)
            self.pending = None
            if not self.save():
                self.workflows.pop()
                self.pending = previous
                raise WorkflowPersistenceError("Couldn't save workflow")
            return workflow, f"Saved workflow: {name}"

    def dismiss_pending(self, now: float | None = None) -> bool:
        with self._lock:
            if not self.pending:
                return True
            previous = self.pending
            when = time.time() if now is None else now
            self.dismissed[previous.signature] = when
            self.dismissed = dict(sorted(self.dismissed.items(), key=lambda item: item[1], reverse=True)[:MAX_DISMISSED])
            self.pending = None
            if self.save():
                return True
            self.pending = previous
            self.dismissed.pop(previous.signature, None)
            return False

    def by_name(self, name: str) -> Workflow | None:
        key = _clean_text(name, MAX_NAME_LEN).casefold()
        return next((workflow for workflow in self.workflows if workflow.name.casefold() == key), None)

    def names(self) -> dict[str, str | None]:
        return {workflow.name: None for workflow in self.workflows} or {
            "__none__": "No workflows have been saved yet."}

    def hit(self, workflow: Workflow) -> bool:
        with self._lock:
            old = workflow.hits
            workflow.hits = min(workflow.hits + 1, 2_000_000_000)
            if self.save():
                return True
            workflow.hits = old
            return False


class WorkflowRunner:
    def __init__(self, store: WorkflowStore, *, run_tool: Callable[..., bool],
                 replay_recipe: Callable[..., bool], cancelled: Callable[[], bool],
                 settle_tool: Callable[[str], None] | None = None) -> None:
        self.store = store
        self.run_tool = run_tool
        self.replay_recipe = replay_recipe
        self.cancelled = cancelled
        self.settle_tool = settle_tool

    def run(self, name: str, progress: Callable[[str], None] | None = None) -> str:
        workflow = self.store.by_name(name)
        if workflow is None:
            raise RuntimeError(f'no saved workflow named "{name}"')
        for index, unit in enumerate(workflow.units, 1):
            if self.cancelled():
                raise RuntimeError("workflow cancelled")
            if progress:
                progress(f"workflow {index}/{len(workflow.units)}: {unit.label}")
            # Revalidate immediately before every action in case policy changed after save.
            if unit.kind == "tool":
                if not eligible_tool(unit.tool_name, unit.args):
                    raise RuntimeError(f"workflow stopped at step {index}: action is no longer eligible")
                ok = self.run_tool(unit.tool_name, dict(unit.args), unit.label, record=False)
            elif unit.kind == "recipe" and _HASH.fullmatch(unit.recipe_key):
                ok = self.replay_recipe(unit.recipe_key, progress)
            else:
                ok = False
            if not ok:
                raise RuntimeError(f"workflow stopped at step {index}: {unit.label}")
            if index < len(workflow.units) and unit.kind == "tool" and self.settle_tool:
                self.settle_tool(unit.tool_name)
        self.store.hit(workflow)
        return f"Finished workflow: {workflow.name}"


def register_tools(store: WorkflowStore, run_workflow: Callable[[str], str], *,
                   tools_module: Any = None, vocab_module: Any = None) -> None:
    """Register built-ins lazily so storage/mining remains platform-independent."""
    if tools_module is None:
        from . import tools as tools_module
    if vocab_module is None:
        from . import vocab as vocab_module

    if store.workflows:
        vocab_module.learn(*(workflow.name for workflow in store.workflows))

    def run(values: dict[str, str]) -> str:
        name = values.get("workflow", "")
        return run_workflow(name)

    def install(tool: Any) -> None:
        if tool.name in tools_module.BY_NAME:
            tools_module.TOOLS[:] = [old for old in tools_module.TOOLS if old.name != tool.name]
        tools_module.TOOLS.append(tool)
        tools_module.BY_NAME[tool.name] = tool

    def install_run_tool() -> None:
        install(tools_module.Tool(
            "run_workflow", "Run one explicitly saved workflow from start to finish.",
            ["run morning standup", "start morning standup"], run=run,
            enum_args=[tools_module.EnumArg("workflow", "Which saved workflow?", store.names)]))

    def save(values: dict[str, str]) -> str:
        workflow, message = store.save_pending(values.get("name", ""))
        if workflow:
            vocab_module.learn(workflow.name)
            install_run_tool()
        return message

    install(tools_module.Tool(
        "save_workflow", "Name and save the repeated routine Jevme most recently suggested.",
        ["save workflow as morning standup", "call this workflow morning standup"], run=save,
        text_arg=tools_module.TextArg("name", "The exact name for the pending workflow."), instant=False))
    if store.workflows:
        install_run_tool()
    elif "run_workflow" in tools_module.BY_NAME:
        tools_module.TOOLS[:] = [old for old in tools_module.TOOLS if old.name != "run_workflow"]
        tools_module.BY_NAME.pop("run_workflow", None)
