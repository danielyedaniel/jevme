"""When no tool fits, have Claude write one: a small AppleScript / JXA / shell tool with typed args.

Gates before anything runs: hard policy → syntax compile → Jev "would this really do it?" review
(repair loop with the compiler's message) → confirmation by voice if the script is consequential.

Provider: the Anthropic SDK when credentials resolve (ANTHROPIC_API_KEY or an `ant auth login`
profile), otherwise the Claude Code CLI (`claude -p`), which is already signed in on this Mac.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass

from . import actions as A
from . import learned as L
from . import policy
from . import tools as T
from .jev import JevClient, noul

log = logging.getLogger("jevme.generator")

MODEL = os.environ.get("JEVME_CODEGEN_MODEL", "claude-opus-5")
CLI_MODEL = os.environ.get("JEVME_CODEGEN_CLI_MODEL", "opus")
MAX_ATTEMPTS = 3
REVIEW_THRESHOLD = 0.45   # macbrow's calibration: invented scripts score 0.05-0.37, real ones 0.49-0.83

SYSTEM = """You write tiny macOS automation tools for a voice assistant called Jevme.
The user spoke a request that none of the existing tools can do. Write ONE new tool that does exactly
that, generalised just enough to be reused: anything the user might say differently next time (an app,
a name, a number, a piece of text) becomes an argument with a {{placeholder}} in the script.

Environment: macOS 14 (Sonoma), Apple Silicon. The script runs from a background process with
Accessibility and Automation permissions. Prefer, in order:
  1. AppleScript against the app's own dictionary (Notes, Mail, Messages, Calendar, Reminders, Finder,
     Safari, Google Chrome, Music, Spotify, Photos, System Events...).
  2. `shell` for system things (brightness via `brightness` is NOT installed; use AppleScript key codes
     144/145 for brightness, `osascript -e 'set volume ...'` style is unnecessary since AppleScript is
     available directly; `screencapture`, `open`, `pbcopy`, `say`, `caffeinate`, `pmset -g`, `mdfind`...).
  3. JXA (`jxa`) when JavaScript is clearer.
  4. System Events UI scripting (menus, keystrokes) only when an app has no dictionary for the action.
Never use sudo, never touch passwords/keychain, never wipe disks. Finish in under 5 seconds. The script
must actually perform the action (not just compute something). Do not `activate` apps needlessly, but
do bring an app forward when the user will look at the result.

Reply with ONLY a JSON object, no prose, no code fences:
{
  "name": "snake_case_tool_name",
  "what": "One sentence: what this tool does, phrased like a capability.",
  "examples": ["three", "spoken phrasings", "that should trigger it"],
  "app": "App name the tool is about, or null",
  "kind": "applescript" | "jxa" | "shell",
  "script": "the script, with {{arg}} placeholders",
  "args": [ {"name": "arg", "kind": "text" | "enum", "question": "What Jev should ask to fill it",
             "options": {"value": "description"}   // enum only
           } ],
  "args_now": {"arg": "the value for THIS request, taken from the user's words"},
  "label": "Short past-tense result for a status pill, may use {{arg}}",
  "risky": false,
  "risk_note": "why, if risky (deletes / sends / pays / changes settings)"
}
At most one text arg. Placeholders inside AppleScript strings will be escaped for you; do not add quotes
around them beyond the surrounding string literal. Keep names and examples in the user's own words."""


@dataclass
class Generated:
    spec: L.LearnedSpec
    args_now: dict[str, str]
    review: float
    attempts: int
    latency_ms: int
    provider: str


class Generator:
    def __init__(self, jev: JevClient) -> None:
        self.jev = jev
        self._sdk = None
        try:
            import anthropic  # noqa: F401
            client = anthropic.Anthropic()
            # Only trust it if credentials actually resolve.
            client._client  # noqa: B018
            self._sdk = client if (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                                   or (os.path.expanduser("~/.config/anthropic") and os.path.isdir(os.path.expanduser("~/.config/anthropic")))) else None
        except Exception:  # noqa: BLE001
            self._sdk = None
        self.provider = "anthropic-sdk" if self._sdk else "claude-cli"

    # ---------- llm ----------

    def _complete(self, messages: list[dict]) -> str:
        if self._sdk is not None:
            try:
                r = self._sdk.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM,
                                              output_config={"effort": "low"}, messages=messages)
                return "".join(b.text for b in r.content if b.type == "text")
            except Exception as e:  # noqa: BLE001
                log.warning("sdk failed (%s); falling back to claude cli", e.__class__.__name__)
                self._sdk = None
                self.provider = "claude-cli"
        prompt = SYSTEM + "\n\n" + "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)
        r = subprocess.run(["claude", "-p", prompt, "--model", CLI_MODEL, "--output-format", "text"],
                           capture_output=True, text=True, timeout=120,
                           env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200] or "claude cli failed")
        return r.stdout

    @staticmethod
    def _parse(text: str) -> dict:
        """First JSON object in the reply. raw_decode stops at the object's end, so trailing prose or a
        second object doesn't break it the way a greedy {.*} did."""
        dec = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            try:
                obj, _ = dec.raw_decode(text, m.start())
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
        raise ValueError("no JSON in reply")

    # ---------- gates ----------

    def _review(self, request: str, spec: L.LearnedSpec, args_now: dict[str, str]) -> float:
        answers, _ = self.jev.ask(
            {"request": request, "script": spec.render(args_now)[0], "kind": spec.kind},
            {"works": noul(
                "`script` is a macOS automation script (`kind` says AppleScript, JXA or shell) written to carry out "
                "`request`. Would running it actually accomplish the request? Every command, application, "
                "property and flag used must really exist and do what the script assumes; a script that only "
                "computes values, invents commands or properties, or drives the wrong UI does not accomplish it.",
                true="It would really perform the request as written.",
                false="It would fail, do nothing, do something else, or relies on things that do not exist.")})
        return answers["works"].noul if "works" in answers else 0.0

    # ---------- main ----------

    def generate(self, utterance: str) -> Generated:
        t0 = time.monotonic()
        ctx = {
            "request": utterance,
            "front_app": A.frontmost_app(),
            "running_apps": A.running_apps(),
            "installed_apps": A.installed_apps(),
            "existing_tools": [t.name for t in T.TOOLS],
        }
        messages = [{"role": "user", "content": json.dumps(ctx)}]
        last_err = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            reply = self._complete(messages)
            try:
                data = self._parse(reply)
            except ValueError:
                # One malformed reply shouldn't end the whole attempt budget: ask again.
                last_err = "the reply was not a JSON object"
                log.info("attempt %d rejected: %s", attempt, last_err)
                messages.append({"role": "assistant", "content": reply[:4000] or "(empty)"})
                messages.append({"role": "user", "content": "That was not a JSON object. Reply with the JSON object only."})
                continue
            spec = L.LearnedSpec(
                name=L.safe_name(str(data.get("name", "learned"))),
                what=str(data.get("what", utterance)),
                examples=[str(x) for x in (data.get("examples") or [utterance])][:5],
                kind=str(data.get("kind", "applescript")).lower(),
                script=str(data.get("script", "")),
                label=str(data.get("label") or data.get("what") or "Done"),
                args=[a for a in (data.get("args") or []) if isinstance(a, dict) and a.get("name")],
                app=data.get("app") or None,
                risky=bool(data.get("risky", False)),
                risk_note=data.get("risk_note") or None,
            )
            args_now = {str(k): str(v) for k, v in (data.get("args_now") or {}).items()}
            if spec.kind not in L.KINDS or not spec.script.strip():
                last_err = "invalid kind or empty script"
            elif (why := policy.check(spec.script)):
                raise PermissionError(f"blocked: {why}")
            elif (err := L.compile_check(spec)):
                last_err = f"compile error: {err}"
            else:
                score = self._review(utterance, spec, args_now)
                log.info("attempt %d: %s kind=%s review=%.2f", attempt, spec.name, spec.kind, score)
                if score >= REVIEW_THRESHOLD:
                    if policy.needs_confirmation(spec.script) and not spec.risky:
                        spec.risky = True
                        spec.risk_note = spec.risk_note or policy.needs_confirmation(spec.script)
                    return Generated(spec, args_now, score, attempt, int((time.monotonic() - t0) * 1000), self.provider)
                last_err = (f"A reviewer judged this script unlikely to actually perform the request "
                            f"(confidence {score:.2f}). Rewrite it using commands and properties that certainly exist.")
            log.info("attempt %d rejected: %s", attempt, last_err)
            messages.append({"role": "assistant", "content": json.dumps(data)})
            messages.append({"role": "user", "content": f"That was rejected: {last_err}\nReply with a corrected JSON object only."})
        raise RuntimeError(last_err or "could not write a working tool")
