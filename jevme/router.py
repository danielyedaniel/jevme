"""Turn a stream of partial transcripts into ordered actions.

Two stages, kept separate:

  DECIDE (this file)   partial → one Jev request (intent + speculative args) → pick a PATH and enqueue it.
  EXECUTE (main queue) run enqueued units one at a time via `execute_clause` / `run_tool`.

Every completed command takes exactly one of these paths:

  · cancel / chat / not_yet   → nothing.
  · shortcut (a built-in tool) → enqueue the tool. Fires mid-sentence once stable (the reactive path).
  · plan (several clauses)     → enqueue each clause in order.
  · agent (a screen task)      → enqueue the clause; the agent reasons over the accessibility tree.
  · learn (no app can do it)   → write a new tool (codegen), then run it.

Three triggers decide WHEN a command is ready to act, all funnelling into `_hand_off` or `_commit`:
  · stable shortcut          — the same confident tool across two partials (or a brief pause).
  · streamed leading clause  — a complete clause followed by a conjunction, fired while still talking.
  · settle                   — a pause after the sentence ends, for the plan / agent / learn paths.

Free-text arguments (a search query, a note title) are *selected* from the utterance by Jev, never
generated, so nothing is invented. Memory (see.recall, task recipes) is an accelerator inside the
agent path, not a separate path.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import actions as A
from . import config
from .jev import Decision, JevClient, choice, noul
from . import tools as T

log = logging.getLogger("jevme.router")

MAX_TAIL_WORDS = 14
MAX_FOCUS_WORDS = 18
ABANDON_AFTER_S = 2.6   # a clause that produced no action is dropped after this much silence
                        # (must exceed the 2.2 s patience _looks_incomplete gives send/message commands)
SETTLE_S = 1.0          # silence after which a finished sentence is planned / handed to the agent

COMMAND_VERBS = (
    "open|go|click|type|search|close|add|make|create|send|play|press|scroll|put|take|delete|reply|find|look|"
    "switch|quit|turn|set|write|show|hide|pause|skip|mute|unmute|select|copy|paste|save|minimize|maximize|"
    "refresh|reload|start|stop|remove|check|uncheck|enable|disable|pull|bring|launch|tell|text|message|email|"
    "call|book|order|buy|read|name|title|rename|drag|move|zoom|sort|filter|choose|pick|enter|submit|log|sign"
)
_SPLIT = re.compile(rf"\s*(?:,|;)?\s*\b(?:and then|then|and|after that|next|afterwards)\b\s+(?=(?:{COMMAND_VERBS})\b)", re.I)


# A command that ends on one of these is still being spoken ("send a message to…", "search for…").
_TRAILING = re.compile(
    r"\b(to|for|with|about|saying|that|the|a|an|and|or|of|on|in|into|at|my|your|called|titled|named)"
    r"[.?!,]?\s*$", re.I)
# …but these endings are the command's own object or particle, not a dangling preposition: "zoom in",
# "sign out", "turn it on", and a two-word "bold that" / "delete this". (The eval showed "zoom in" and
# "bold that" never firing: the trailing-word check read them as unfinished.)
_COMPLETE_TAIL = re.compile(
    r"(\b(zoom|log|sign|check|opt|clock)\s+(in|on)"
    r"|\b(it|them|this|that)\s+(in|on)"
    r"|^\s*(?:(?:ok|okay|now|please|just)\s+)*[a-z]+\s+(that|this))[.?!,]?\s*$", re.I)
# A lone verb that can't be acted on without an object ("open", "search"). Deliberately NOT "send" —
# a bare "send" / "send it" is complete: it submits whatever is drafted.
_BARE_VERB = re.compile(r"^\s*(?:(?:ok|okay|now|please)\s+)*(open|search|type|go|make|create|add|set|put|"
                        r"tell|write|text|email|message)[.?!,]?\s*$", re.I)
# Verbs that usually take an object. An instant tool (play/pause, …) waits a beat when the text ends on one.
_OBJECT_VERB_END = re.compile(r"\b(play|open|search|find|show|go|type|put|start)[.?!,]?\s*$", re.I)
# Commands that need a recipient/content: give them extra patience when still short.
_NEEDS_MORE = re.compile(r"^\s*(send|text|message|email|reply|tell|write|draft|call|ask)\b", re.I)
_SEND_DRAFT = re.compile(r"^\s*(?:(?:ok|okay|now|please|just)\s+)*(?:hit\s+|press\s+|click\s+)?"
                         r"(send|submit)(\s+(it|that|this|the (message|draft|email|reply|text)))?[.?!,]?\s*$", re.I)


def _looks_incomplete(focus: str, idle: float) -> bool:
    """True if the utterance is probably still mid-sentence, so we shouldn't hand a fragment to the agent.

    'send a message' fires prematurely without a recipient/content; wait for the rest (or a real pause).
    A bare 'send' is not incomplete: it means submit the draft."""
    if _SEND_DRAFT.match(focus):
        return False
    if _BARE_VERB.match(focus):
        return True
    if _TRAILING.search(focus) and not _COMPLETE_TAIL.search(focus):
        return True
    words = focus.split()
    if _NEEDS_MORE.match(focus) and len(words) < 6 and idle < 2.2:
        return True
    return False


DANGLING_ASK_AFTER_S = 0.8   # a fragment the user has paused on this long is asked anyway
ANSWER_TTL_S = 4.0           # reuse Jev's answer for identical text within this window


def _dangling(focus: str) -> bool:
    """Ends mid-phrase ("go to the", "search for", a lone "open"): certainly not a finished command."""
    f = focus.strip()
    if _SEND_DRAFT.match(f):
        return False
    return bool(_BARE_VERB.match(f) or (_TRAILING.search(f) and not _COMPLETE_TAIL.search(f)))


def _norm_text(t: str) -> str:
    return " ".join(re.sub(r"[^\w' ]+", " ", t.lower()).split())


def split_clauses(text: str) -> list[str]:
    """'open chrome and go to amazon and add a rice cooker to my cart' → three commands, in order."""
    parts = [p.strip(" ,.;!?") for p in _SPLIT.split(text)]
    parts = [re.sub(r"^(?:(?:okay|ok|alright|now|so|please|can you|could you|um|uh)\s+)+", "", p, flags=re.I).strip() for p in parts]
    verbs = set(COMMAND_VERBS.split("|"))
    return [p for p in parts if len(p.split()) >= 2 or p.lower() in verbs] or [text.strip()]


@dataclass
class Proposal:
    tool: str
    args: dict[str, str]
    confidence: float
    seq: int
    utterance: str = ""    # the text Jev decided on; later speech isn't part of this command

    def key(self) -> tuple:
        return (self.tool, tuple(sorted(self.args.items())))


class Router:
    """Owns the transcript cursor and decides when to act. All public methods run on the main thread."""

    def __init__(self, jev: JevClient, *, on_preview: Callable[[str | None], None],
                 on_action: Callable[[str], None], on_error: Callable[[str], None],
                 dispatch_main: Callable[[Callable, tuple], None],
                 on_learn: Callable[[str], None] | None = None,
                 on_general: Callable[[str], None] | None = None,
                 on_cancel: Callable[[], None] | None = None,
                 on_plan: Callable[[list[str]], None] | None = None,
                 on_commit: Callable[[str, dict, str], None] | None = None,
                 on_stream: Callable[[str], None] | None = None) -> None:
        self.jev = jev
        self.on_learn = on_learn
        self.on_general = on_general
        self.on_cancel = on_cancel
        self.on_plan = on_plan
        self.on_commit = on_commit        # (tool_name, args, label): main enqueues; harness records
        self.on_stream = on_stream        # (clause): a completed leading clause, fired mid-sentence
        self.agent = None                 # set by main: executes general clauses
        self.learn_now: Callable[[str, Callable[[str], None]], None] | None = None   # set by main: sync codegen
        self.on_failed: Callable[[str, str, str], None] | None = None   # (goal, app0, why): main watches a demo
        self.last_probs: dict[str, float] = {}
        self.plan_cancelled = False
        self.pending_confirm: tuple[Callable[[], None], str, float] | None = None  # (run, label, asked_at)
        self.learning = False
        self.on_preview = on_preview
        self.on_action = on_action
        self.on_error = on_error
        self.dispatch_main = dispatch_main

        self.transcript = ""
        self.cursor = 0
        self.last_change_t = 0.0
        self.seq = 0
        self.inflight = False
        self.queued = False
        self.last_request_t = 0.0

        self.prev: Proposal | None = None
        self.stable_count = 0
        self.recent_action: str | None = None
        self.stats = {"asked": 0, "reused": 0}      # Jev calls made vs answers reused (see _fire_request)
        self.recent_fire: dict[tuple, float] = {}
        self.timer: threading.Timer | None = None

    # ---------- transcript feed ----------

    def reset_session(self) -> None:
        self.transcript = ""
        self.cursor = 0
        self.prev = None
        self.stable_count = 0
        self.on_preview(None)

    # ---------- cursor: how much of the transcript has been acted on ----------
    #
    # Apple revises earlier words as it hears more ("Hello open visual" → "Open Visual Studio Code"), so a
    # character offset lands mid-word after a revision — the log showed fragments like "o Code" and
    # "s tab close this" being routed as commands. The cursor is therefore stored as a WORD count plus the
    # last consumed words, and re-anchored on those words whenever the transcript is rewritten.

    _WORD = re.compile(r"\S+")

    @staticmethod
    def _norm_word(w: str) -> str:
        return re.sub(r"[^\w']", "", w.lower())

    @property
    def cursor(self) -> int:
        spans = [(m.start(), m.group()) for m in self._WORD.finditer(self.transcript)]
        n = self._anchor([w for _, w in spans])
        return spans[n][0] if n < len(spans) else len(self.transcript)

    @cursor.setter
    def cursor(self, char_offset: int) -> None:
        words = [(m.start(), m.end(), m.group()) for m in self._WORD.finditer(self.transcript)]
        n = sum(1 for s, e, _ in words if s < char_offset)       # a word straddling the offset counts as used
        self._consumed = n
        self._tail = [self._norm_word(w) for _, _, w in words[max(0, n - 3):n]]

    def _anchor(self, words: list[str]) -> int:
        """Where the consumed part ends in the CURRENT transcript. If the words just before the stored count
        still match what we consumed, keep it; otherwise find the consumed tail again after a revision."""
        n = min(getattr(self, "_consumed", 0), len(words))
        tail = getattr(self, "_tail", [])
        if not tail:
            return n
        norm = [self._norm_word(w) for w in words]
        k = len(tail)
        if norm[max(0, n - k):n] == tail:
            return n
        for end in range(min(len(norm), n + 3), k - 1, -1):     # nearest re-occurrence, allowing drift
            if norm[end - k:end] == tail:
                self._consumed = end
                return end
        return n

    def pending_text(self) -> str:
        return self.transcript[self.cursor:].strip()

    def focus_text(self) -> str:
        """The part of the pending text worth judging: the last sentence, capped to a few words.

        Apple punctuates partials, so an earlier clause that produced no action ('delete the groceries
        note.') stops polluting the next one ('open chrome')."""
        pending = self.pending_text()
        parts = re.split(r"(?<=[.?!])\s+", pending)
        focus = parts[-1].strip() if parts and parts[-1].strip() else pending
        words = focus.split()
        if len(words) > MAX_FOCUS_WORDS:
            focus = " ".join(words[-MAX_FOCUS_WORDS:])
        return focus

    _SHORT_HEADS = {"undo", "redo", "stop", "cancel", "wait", "never", "no", "yes", "nope", "yeah", "sure",
                    "next", "back", "previous", "enter", "escape", "again", "more", "less", "louder", "quieter"}

    def _absorbable(self) -> bool:
        """'close this' can commit (it's a complete command) a beat before 'tab' is heard. A short tail right
        after a commit that isn't a command of its own finishes the one that ran (replay: a lone 'tab'
        pressed Tab; 'open VS' + 'code' opened a second app). Side-effect free: see tick() for the absorb."""
        pending = self.pending_text()
        words = re.sub(r"[.?!,]", " ", pending).lower().split()
        if not words or len(words) > 2 or self.pending_confirm is not None:
            return False
        if time.monotonic() - getattr(self, "last_commit_t", -9.0) > 1.5:
            return False
        heads = self._SHORT_HEADS | {e.lower().split()[0] for t in T.TOOLS for e in t.examples if e.strip()}
        return not (words[0] in heads or self._NEW_CLAUSE.match(pending))

    def on_partial(self, text: str, is_final: bool) -> None:
        if text != self.transcript:
            self.transcript = text
            self.last_change_t = time.monotonic()
        if not self.pending_text():
            return
        self._stream()          # fire a completed leading clause now, while the user keeps talking
        if self.pending_text():
            self._schedule(reason="partial")

    def _stream(self) -> None:
        """If the live transcript already contains a complete leading command (there is a conjunction with
        another command after it), run that clause now and advance past it. This makes multi-part requests
        happen as they are spoken instead of after the sentence ends."""
        if self.on_stream is None:
            return
        pending = self.focus_text()
        clauses = split_clauses(pending)
        if len(clauses) < 2:
            return
        lead = clauses[0]
        if len(lead.split()) < 2:
            return
        idx = self.transcript.lower().find(lead.lower(), self.cursor)
        if idx < 0:
            return
        self.cursor = idx + len(lead)
        self.prev = None
        self.stable_count = 0
        log.info("STREAM «%s»", lead)
        self.on_stream(lead)

    def tick(self) -> None:
        """Called ~every 150 ms by the app so pause-based commits can happen without new audio."""
        if self.pending_confirm is not None and time.monotonic() - self.pending_confirm[2] > 15:
            self.pending_confirm = None
            self.on_error("not confirmed")
        pending = self.pending_text()
        if not pending:
            return
        if self._absorbable():
            # Words are still arriving one partial at a time; only a tail that has stopped growing
            # (and is still a short non-command) is the end of the command that just ran.
            if time.monotonic() - self.last_change_t >= 0.45:
                log.info("ABSORB «%s» into «%s»", pending, self.recent_action)
                self._consume()
            return
        if self.prev is not None:
            self._maybe_commit(self.prev, from_tick=True)
            if self.prev is None:  # committed
                return
        idle = time.monotonic() - self.last_change_t
        if (getattr(self, "_skipped", None) and self._skipped == self.focus_text() and not self.inflight
                and idle >= DANGLING_ASK_AFTER_S):
            self._force_ask = True        # they stopped on "…go to the": ask after all
            self._schedule(reason="paused")
        if getattr(self, "unsupported_count", 0) >= 1 and idle >= 0.9 and not self.inflight and idle < ABANDON_AFTER_S:
            self._schedule(reason="settle")   # let a pending general/unsupported decision settle
            return
        if idle >= SETTLE_S and not self.inflight and self.pending_confirm is None:
            # The sentence is over and no shortcut fired. Was it a command at all?
            focus = self.focus_text()
            probs = self.last_probs
            inert = probs.get(T.NOT_YET, 0) + probs.get(T.CHAT, 0) + probs.get(T.CANCEL, 0)
            clauses = split_clauses(focus)
            if len(clauses) > 1 and inert < 0.6:
                self._hand_off(focus)
                return
            # A shortcut that clearly beats the general path wins even when two shortcuts split the vote
            # ("open chatgpt": the app or the site are both right).
            if self.prev is not None and inert < 0.4:
                best_tool = max((v for k, v in probs.items() if k in T.BY_NAME), default=0.0)
                if best_tool >= 0.4 and best_tool >= probs.get(T.GENERAL, 0) and self.prev.confidence >= 0.35:
                    tool = T.BY_NAME[self.prev.tool]
                    needs = tool.text_arg and not tool.text_arg.optional and tool.text_arg.name not in self.prev.args
                    missing_enum = any(a.name not in self.prev.args for a in tool.enum_args)
                    fragment = (len(focus.split()) == 1 and
                                len(re.sub(r"[.?!,]", " ", self.pending_text()).split()) > 1)
                    if not needs and not missing_enum and not fragment:
                        log.info("→ shortcut (best-guess) %s (p=%.2f)", self.prev.tool, best_tool)
                        self._commit(self.prev)
                        return
            if inert < 0.4 and len(focus.split()) >= 2 and not _looks_incomplete(focus, idle):
                self._hand_off(focus)
                return
        if idle >= ABANDON_AFTER_S and not self.inflight:
            log.info("ABANDON «%s»", pending)
            self.cursor = len(self.transcript)
            self.prev = None
            self.stable_count = 0
            self.on_preview(None)

    # ---------- jev ----------

    def _schedule(self, reason: str) -> None:
        if self.inflight:
            self.queued = True
            return
        gap = time.monotonic() - self.last_request_t
        if gap < config.DEBOUNCE_S:
            if self.timer is None:
                self.timer = threading.Timer(config.DEBOUNCE_S - gap,
                                             lambda: self.dispatch_main(self._fire_request, ()))
                self.timer.start()
            return
        self._fire_request()

    def _fire_request(self) -> None:
        self.timer = None
        pending = self.focus_text()
        if not pending or self.inflight:
            return
        idle = time.monotonic() - self.last_change_t
        # 1) Obviously unfinished ("open", "go to the", "search for"): the answer is "still talking", so don't
        #    ask. Half the calls in the usage log were this. If the user stops on it, tick() asks after a pause.
        forced = getattr(self, "_force_ask", False)
        self._force_ask = False
        if not forced and self.pending_confirm is None and idle < DANGLING_ASK_AFTER_S and _dangling(pending):
            self._skipped = pending
            self.prev = None
            self.stable_count = 0
            return
        self._skipped = None
        # 2) Exactly what Jev just answered (a settle tick, a punctuation-only revision): reuse the answer.
        key = (_norm_text(pending), self.pending_confirm is not None, self.recent_action)
        cached = getattr(self, "_last_answer", None)
        if cached and cached[0] == key and time.monotonic() - cached[1] < ANSWER_TTL_S and not getattr(self, "_reusing", False):
            self.stats["reused"] += 1
            self._reusing = True
            try:
                self._on_decision(cached[2], pending)
            finally:
                self._reusing = False
            return
        self.stats["asked"] += 1
        self.seq += 1
        seq = self.seq
        self.inflight = True
        self.queued = False
        self.last_request_t = time.monotonic()
        state, questions = self._build(pending)
        def done(d, key=key):
            self._last_answer = (key, time.monotonic(), d)
            self._on_decision(d, pending)
        self.jev.ask_async(seq, state, questions,
                           on_done=lambda d: self.dispatch_main(done, (d,)),
                           on_error=lambda s, e: self.dispatch_main(self._on_jev_error, (s, e)))

    def _build(self, pending: str) -> tuple[dict, dict]:
        state = {
            "utterance": pending,
            "front_app": A.frontmost_app(),
            "front_window": A.front_window_title() or "untitled",
            "running_apps": A.running_apps(),
            "recent_action": self.recent_action or "none",
        }
        from . import vocab
        on_screen = vocab.screen_names()
        if on_screen:
            # Names of the controls on screen: "open two sum" is a click on the "Two Sum" link, not an app.
            state["on_screen"] = on_screen[:40]
        intent_criteria: dict = {t.name: t.criteria() for t in T.TOOLS}
        intent_criteria[T.NOT_YET] = T.NOT_YET_CRITERIA
        intent_criteria[T.CHAT] = T.CHAT_CRITERIA
        intent_criteria[T.UNSUPPORTED] = T.UNSUPPORTED_CRITERIA
        intent_criteria[T.GENERAL] = T.GENERAL_CRITERIA
        intent_criteria[T.CANCEL] = T.CANCEL_CRITERIA
        questions: dict = {"intent": choice(T.INTENT_INSTRUCTIONS, intent_criteria)}
        if self.pending_confirm is not None:
            state["pending_action"] = self.pending_confirm[1]
            questions["confirm"] = noul("`pending_action` is waiting for approval. Does `utterance` say yes, go ahead, "
                                        "do it, confirm, sure, or otherwise approve it?")
            questions["deny"] = noul("`pending_action` is waiting for approval. Does `utterance` say no, cancel, stop, "
                                     "never mind, don't, or otherwise decline it?")

        for t in T.TOOLS:
            for arg in t.enum_args:
                questions[f"arg::{t.name}::{arg.name}"] = choice(
                    [f"Assume the user wants to run '{t.name}' ({t.what})", arg.question,
                     "Base the answer on `utterance`; use `front_app` when the target is implicit."],
                    arg.options())

        spans = self._span_candidates(pending)
        if spans:
            span_criteria: dict = {s: None for s in spans}
            span_criteria["__none__"] = "The value has not been spoken yet, or none of these is exactly it."
            for t in T.TOOLS:
                if t.text_arg:
                    questions[f"text::{t.name}::{t.text_arg.name}"] = choice(
                        [f"Assume the user wants to run '{t.name}' ({t.what}).", t.text_arg.question,
                         "Choose the option that is exactly and only that value as spoken, with no leading "
                         "command words like 'search', 'open', 'type', 'say', 'called', 'that says'."],
                        span_criteria)
        return state, questions

    @staticmethod
    def _span_candidates(pending: str) -> list[str]:
        words = pending.split()
        tail = words[-MAX_TAIL_WORDS:]
        out = []
        for i in range(len(tail)):
            s = " ".join(tail[i:]).strip().strip(".?!,")
            if s:
                out.append(s)
        return list(dict.fromkeys(out))

    def _on_jev_error(self, seq: int, err: Exception) -> None:
        self.inflight = False
        log.warning("jev error: %s", str(err).splitlines()[0])
        # One message per outage, not one flash per partial (a 503 burst flashed six errors in two seconds).
        now = time.monotonic()
        if now - getattr(self, "_last_jev_err", 0.0) > 10:
            self.on_error("Jev unavailable — retrying")
        self._last_jev_err = now

    def _on_decision(self, d: Decision, utterance: str) -> None:
        self.inflight = False
        intent = d.answers.get("intent")
        if intent is None or intent.choice is None:
            return
        log.info("route %-24s conf=%.2f %4dms  «%s»", intent.choice, intent.confidence, d.latency_ms, utterance)
        self.last_probs = intent.probabilities

        if self.pending_confirm is not None:
            run, label, _ = self.pending_confirm
            yes = d.answers["confirm"].noul if "confirm" in d.answers else 0.0
            no = d.answers["deny"].noul if "deny" in d.answers else 0.0
            if yes >= 0.6 and yes > no:
                self.pending_confirm = None
                self.cursor = len(self.transcript)
                log.info("CONFIRMED %s", label)
                threading.Thread(target=run, daemon=True, name="confirmed").start()
            elif no >= 0.6:
                self.pending_confirm = None
                self.cursor = len(self.transcript)
                self.on_error("cancelled")
            return  # while waiting for yes/no, nothing else is acted on

        if intent.choice == T.CANCEL and intent.confidence >= 0.7:
            self.cursor = len(self.transcript)
            self.prev = None
            if self.on_cancel:
                self.on_cancel()
            return

        if intent.choice in (T.UNSUPPORTED, T.GENERAL) and intent.confidence >= 0.7:
            self.unsupported_count = getattr(self, "unsupported_count", 0) + 1
            idle = time.monotonic() - self.last_change_t
            settled = re.search(r"[.?!]\s*$", utterance) or idle >= 0.9
            if self.unsupported_count >= 2 and settled and not _looks_incomplete(utterance, idle):
                self._hand_off(utterance)
            else:
                self.on_preview("working on it" if intent.choice == T.GENERAL else "new tool")
                self.prev = None   # keep polling so the pause can settle it
            return
        self.unsupported_count = 0
        if intent.choice in (T.NOT_YET, T.CHAT) or intent.choice not in T.BY_NAME:
            self.prev = None
            self.stable_count = 0
            self.on_preview(None)
        else:
            tool = T.BY_NAME[intent.choice]
            args, ok = self._extract(tool, d.answers)
            prop = Proposal(tool.name, args, intent.confidence, d.seq, utterance)
            if self.prev is not None and self.prev.key() == prop.key():
                self.stable_count += 1
            else:
                self.stable_count = 1
            self.prev = prop
            self.on_preview(self._label(prop))
            if ok:
                self._maybe_commit(prop, from_tick=False)

        # If the transcript moved while we were waiting, ask again right away.
        # (Compared normalized: a punctuation-only revision is the same text, and re-asking it would just
        # reuse this answer again — the cache made that an infinite loop.)
        if self.queued or (self.focus_text() and _norm_text(self.focus_text()) != _norm_text(utterance)):
            self._schedule(reason="stale")

    @staticmethod
    def _extract(tool: T.Tool, answers: dict) -> tuple[dict[str, str], bool]:
        args: dict[str, str] = {}
        ok = True
        for arg in tool.enum_args:
            a = answers.get(f"arg::{tool.name}::{arg.name}")
            if a is None or a.choice in (None, "__none__"):
                ok = False
            else:
                args[arg.name] = a.choice
        if tool.text_arg:
            a = answers.get(f"text::{tool.name}::{tool.text_arg.name}")
            if a is not None and a.choice not in (None, "__none__"):
                args[tool.text_arg.name] = a.choice
            elif not tool.text_arg.optional:
                ok = False
        return args, ok

    _NEW_CLAUSE = re.compile(rf"^\s*(?:[.?!,;]\s*)?(?:(?:and then|and|then|also|now)\s+)?(?:{COMMAND_VERBS})\b", re.I)

    def _end_of(self, utterance: str) -> int:
        """Where the command Jev decided on ends in the transcript.

        Speech that arrived while the decision was in flight is kept only if it starts a NEW command
        ('open spotify' … 'play drake'). If it merely finishes the same one ('close this' … 'tab',
        'open VS' … 'code'), it is consumed with it — replay showed a kept 'tab' being pressed as a key."""
        u = utterance.strip().strip(".?!,").lower()
        if not u:
            return len(self.transcript)
        idx = self.transcript.lower().find(u, self.cursor)
        if idx < 0:
            return len(self.transcript)
        end = idx + len(u)
        rest = self.transcript[end:]
        if not rest.strip(" .?!,"):
            return len(self.transcript)
        if re.match(r"^[.?!]", rest.lstrip()) or self._NEW_CLAUSE.match(rest):
            return end
        # A continuation: take it up to the next command boundary, if any.
        clauses = split_clauses(rest)
        if len(clauses) > 1:
            j = self.transcript.lower().find(clauses[0].lower(), end)
            if j >= 0:
                return j + len(clauses[0])
        return len(self.transcript)

    def _consume(self, utterance: str = "") -> None:
        self.cursor = self._end_of(utterance) if utterance else len(self.transcript)
        self.prev = None
        self.stable_count = 0
        self.unsupported_count = 0
        self.on_preview(None)

    def _hand_off(self, focus: str) -> None:
        """A completed command that isn't a shortcut. Pick its path — plan, agent, or learn — and enqueue.

        This is the single place the non-shortcut paths are chosen, so `_on_decision` (when a general/
        unsupported intent settles) and `tick` (the pause path) can't disagree."""
        probs = self.last_probs
        self._consume(focus)
        clauses = split_clauses(focus)
        if len(clauses) > 1 and self.on_plan:
            log.info("→ plan %s", clauses)
            self.on_plan(clauses)
            return
        wants_learn = probs.get(T.UNSUPPORTED, 0) > probs.get(T.GENERAL, 0)
        if wants_learn and self.on_learn and not self.learning:
            log.info("→ learn «%s»", focus)
            self.learning = True
            self.on_learn(focus)
            return
        if self.on_general:
            log.info("→ agent «%s»", focus)
            self.on_general(focus)
            return
        if self.on_learn and not self.learning:
            self.learning = True
            self.on_learn(focus)

    # ---------- plans (worker thread) ----------

    # ---------- execution primitives (called on main's task-queue worker) ----------

    def run_tool(self, name: str, args: dict, label: str, progress: Callable[[str], None]) -> None:
        tool = T.BY_NAME[name]
        spec = getattr(tool, "learned", None)
        if spec is not None and spec.risky:
            # A generated tool that deletes/sends/pays/changes settings asks for a spoken yes EVERY time it
            # runs, not just the first time it was written.
            self.dispatch_main(self.ask_confirmation, (label, lambda: self._run_tool_now(tool, name, args, label)))
            return
        progress(label)
        self._run_tool_now(tool, name, args, label)

    def _run_tool_now(self, tool: T.Tool, name: str, args: dict, label: str) -> None:
        try:
            out = tool.run(args) or label
            self.recent_action = out
            self.dispatch_main(self.on_action, (out,))
            # It worked: the names in it (site, app, target, search, title) are words this user says.
            from . import vocab
            vocab.learn(*(str(v) for k, v in args.items() if k in ("site", "app", "target", "query", "title")))
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", name)
            self.dispatch_main(self.on_error, (f"{name}: {str(e)[:50]}",))

    def execute_clause(self, clause: str, progress: Callable[[str], None]) -> None:
        """Run one spoken clause through the tier pipeline: shortcut → agent → learn (→ nothing)."""
        progress(clause)
        try:
            state, questions = self._build(clause)
            answers, _ = self.jev.ask(state, questions)
        except Exception as e:  # noqa: BLE001
            self.dispatch_main(self.on_error, (f"jev: {e.__class__.__name__}",))
            return
        intent = answers.get("intent")
        name = intent.choice if intent else T.NOT_YET
        conf = intent.confidence if intent else 0.0
        log.info("exec %-20s conf=%.2f «%s»", name, conf, clause)

        # Tier 0 — non-actions.
        if name in (T.CHAT, T.NOT_YET, T.CANCEL):
            return
        # Tier 1 — a built-in shortcut tool.
        if name in T.BY_NAME and conf >= 0.5:
            tool = T.BY_NAME[name]
            args, ok = self._extract(tool, answers)
            if ok:
                self.run_tool(name, args, self._tool_label(tool, args), progress)
                return
        # Tier 3 — nothing an app window can do: write a tool.
        if name == T.UNSUPPORTED and (self.learn_now or self.on_learn):
            if self.learn_now:           # on the queue worker: write + run the tool here, in spoken order
                self.learn_now(clause, progress)
            else:
                self.dispatch_main(self.on_learn, (clause,))
            return
        # Tier 2 — a screen task: the agent reasons over the accessibility tree (with memory as accelerator).
        if self.agent is not None:
            res = self.agent.run(clause, progress=progress)
            self.recent_action = res.summary
            if (not res.ok and self.on_failed and not self.agent.cancelled
                    and not res.summary.startswith("Jev unavailable")):   # an outage isn't a task to learn
                # Failed: offer to learn it by watching the user do it by hand.
                self.dispatch_main(self.on_failed, (clause, getattr(self.agent, "app0", ""), res.summary))
            else:
                self.dispatch_main(self.on_action if res.ok else self.on_error, (res.summary,))

    def is_settle_tool(self, name: str) -> bool:
        return name in ("open_app", "open_site", "youtube_play", "site_search", "web_search", "new_tab", "open_folder")

    @staticmethod
    def _tool_label(tool: T.Tool, args: dict) -> str:
        val = args.get(tool.text_arg.name) if tool.text_arg else None
        val = val or next(iter(args.values()), None)
        nice = tool.name.replace("_", " ")
        return f"{nice}: {val}" if val else nice

    # kept for the replay/eval harness, which drives clauses synchronously
    def run_plan(self, clauses: list[str], progress: Callable[[str], None]) -> None:
        for clause in clauses:
            if self.plan_cancelled:
                return
            self.execute_clause(clause, progress)

    # ---------- commit ----------

    def _label(self, p: Proposal) -> str:
        tool = T.BY_NAME[p.tool]
        val = p.args.get(tool.text_arg.name) if tool.text_arg else None
        val = val or next(iter(p.args.values()), None)
        nice = p.tool.replace("_", " ")
        return f"{nice}: {val}" if val else nice

    def _maybe_commit(self, p: Proposal, *, from_tick: bool) -> None:
        if self._absorbable():
            return      # may be the tail of the command that just ran; tick() decides
        tool = T.BY_NAME[p.tool]
        idle = time.monotonic() - self.last_change_t
        pending = self.focus_text()
        ends_sentence = bool(re.search(r"[.?!]\s*$", pending))
        # Once the user has paused and nothing better arrived, a clear-enough best guess beats dropping it.
        settled = idle >= (0.5 if tool.instant else 0.9) or (ends_sentence and idle >= 0.4)
        threshold = config.COMMIT_CONFIDENCE - 0.15 if settled else config.COMMIT_CONFIDENCE
        if p.confidence < threshold:
            return
        # A single word that Apple's interim punctuation split off a longer phrase ("This. Tab" → "Tab")
        # is a fragment, not a command: the log showed it pressing Tab in the middle of "close this tab".
        # A genuine one-word command ("pause", "undo") is the WHOLE pending text, so it still fires at once.
        if len(pending.split()) == 1:
            whole = re.sub(r"[.?!,]", " ", self.pending_text()).split()
            if len(whole) > 1:
                return
        # The decision is about the text Jev saw. If the user has said more since ("and play" → "and play
        # Drake."), it's stale: the re-ask _on_decision already scheduled will judge the full text.
        norm = lambda t: re.sub(r"[^\w']+", " ", t.lower()).strip()  # noqa: E731
        if p.utterance and norm(p.utterance) != norm(pending):
            return
        if tool.instant and _OBJECT_VERB_END.search(pending) and idle < 0.8:
            return   # "and play…" is usually "and play Drake": give the object a moment to arrive
        if tool.instant:
            ready = self.stable_count >= config.STABLE_PARTIALS or idle >= 0.35 or ends_sentence
        else:
            needs = tool.text_arg and not tool.text_arg.optional and tool.text_arg.name not in p.args
            if needs:
                return
            ready = idle >= config.TEXT_PAUSE_S or (ends_sentence and self.stable_count >= 2)
        if not ready:
            return
        last = self.recent_fire.get(p.key(), 0.0)
        if time.monotonic() - last < config.REFIRE_GUARD_S:
            return
        self._commit(p)

    def _commit(self, p: Proposal) -> None:
        tool = T.BY_NAME[p.tool]
        self.recent_fire[p.key()] = time.monotonic()
        self.last_commit_t = time.monotonic()
        # Consume only the clause that fired: "open discord and go to…" keeps "and go to…" for the next decision.
        focus = p.utterance or self.focus_text()
        clauses = split_clauses(focus)
        end = self._end_of(focus)
        if len(clauses) > 1:
            idx = self.transcript.lower().rfind(clauses[0].lower())
            if idx >= self.cursor:
                end = idx + len(clauses[0])
        self.cursor = end
        self.last_change_t = time.monotonic()   # the remainder gets a fresh settle window
        self.prev = None
        self.stable_count = 0
        self.recent_action = self._label(p)
        self.on_preview(None)
        log.info("COMMIT %s %s", p.tool, p.args)
        if self.on_commit:
            self.on_commit(p.tool, p.args, self._label(p))   # main enqueues in order; harness records
        else:
            threading.Thread(target=lambda: self.run_tool(p.tool, p.args, self._label(p), lambda s: None),
                             daemon=True).start()

    # ---------- learning ----------

    def ask_confirmation(self, label: str, run: Callable[[], None]) -> None:
        """Park a consequential action until the user says yes (or no / 15 s pass)."""
        self.pending_confirm = (run, label, time.monotonic())
        self.cursor = len(self.transcript)
        self.on_preview(f"say yes: {label}")

    def learning_done(self) -> None:
        self.learning = False
