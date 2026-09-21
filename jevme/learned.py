"""Learned tools: scripts an LLM wrote for a request no built-in tool covered.

Stored in ~/.config/jevme/learned.json and loaded into the catalog at startup, so the next
time you say it, Jev routes straight to the script with no LLM in the loop.
"""
from __future__ import annotations

import json
import logging
import re
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import actions as A
from . import tools as T

log = logging.getLogger("jevme.learned")

PATH = Path.home() / ".config" / "jevme" / "learned.json"
KINDS = {"applescript": ["osascript", "-l", "AppleScript"], "jxa": ["osascript", "-l", "JavaScript"], "shell": ["/bin/zsh", "-c"]}


@dataclass
class LearnedSpec:
    name: str
    what: str
    examples: list[str]
    kind: str                       # applescript | jxa | shell
    script: str                     # with {{arg}} placeholders
    label: str                      # short past-tense label for the pill, may contain {{arg}}
    args: list[dict[str, Any]] = field(default_factory=list)   # {name, kind: text|enum, question, options?}
    app: str | None = None
    risky: bool = False
    risk_note: str | None = None

    def render(self, values: dict[str, str]) -> tuple[str, dict[str, str]]:
        """The script with values filled in, plus environment variables it reads.

        Values are spoken text and must never become code. Each placeholder is filled according to the
        quoting it sits in: shell values are passed as environment variables (so `$(...)` or quotes in the
        text are never parsed), AppleScript/JXA values are escaped for the literal they are inside, or
        become a literal of their own when the template left the placeholder bare."""
        env: dict[str, str] = {}
        out = self.script
        for a in self.args:
            name = a["name"]
            v = values.get(name, "")
            var = "JEVME_ARG_" + re.sub(r"\W", "_", name)
            if self.kind == "shell":
                env[var] = v
            out = _fill(out, "{{" + name + "}}", self.kind, v, var)
        return out, env

    def render_label(self, values: dict[str, str]) -> str:
        out = self.label
        for a in self.args:
            out = out.replace("{{" + a["name"] + "}}", values.get(a["name"], ""))
        return out

    def run(self, values: dict[str, str], timeout: float = 20.0) -> str:
        script, env = self.render(values)
        cmd = KINDS[self.kind]
        if self.kind == "shell":
            r = subprocess.run(cmd + [script], capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, **env})
        else:
            r = subprocess.run(cmd + ["-e", script], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}")
        return self.render_label(values)


# Opening quote → closing quote, per language. Backslash escapes inside all of them except shell '…'.
_QUOTES = {"shell": "\"'", "applescript": "\"", "jxa": "\"'`"}
_LINE_COMMENT = {"shell": "#", "applescript": "--", "jxa": "//"}


def _quote_state(src: str, end: int, kind: str) -> str | None:
    """Which quote character is open at src[end] (None if outside any string literal)."""
    q: str | None = None
    i = 0
    comment = _LINE_COMMENT[kind]
    while i < end:
        c = src[i]
        if q is None:
            if src.startswith(comment, i) and (kind != "shell" or i == 0 or src[i - 1].isspace()):
                nl = src.find("\n", i)
                if nl < 0 or nl >= end:
                    return None
                i = nl + 1
                continue
            if c in _QUOTES[kind]:
                q = c
        elif c == "\\" and not (kind == "shell" and q == "'"):
            i += 2
            continue
        elif c == q:
            q = None
        i += 1
    return q


def _js_escape(v: str) -> str:
    return (json.dumps(v)[1:-1].replace("'", "\\'").replace("`", "\\`").replace("${", "\\${"))


def _fill(src: str, ph: str, kind: str, v: str, var: str) -> str:
    out, pos = [], 0
    while True:
        i = src.find(ph, pos)
        if i < 0:
            out.append(src[pos:])
            return "".join(out)
        q = _quote_state(src, i, kind)
        if kind == "shell":
            rep = {None: f'"${{{var}}}"', '"': f"${{{var}}}", "'": f"'\"${{{var}}}\"'"}[q]
        elif kind == "applescript":
            rep = A.esc(v).replace("\n", "\\n") if q else '"' + A.esc(v).replace("\n", "\\n") + '"'
        else:
            rep = _js_escape(v) if q else json.dumps(v)
        out.append(src[pos:i] + rep)
        pos = i + len(ph)


def compile_check(spec: LearnedSpec) -> str | None:
    """Syntax-check without running. Returns an error string or None."""
    script, _ = spec.render({a["name"]: "x" for a in spec.args})
    try:
        if spec.kind == "shell":
            r = subprocess.run(["/bin/zsh", "-n", "-c", script], capture_output=True, text=True, timeout=5)
        else:
            lang = "AppleScript" if spec.kind == "applescript" else "JavaScript"
            r = subprocess.run(["osacompile", "-l", lang, "-o", "/tmp/jevme_check.scpt", "-e", script],
                               capture_output=True, text=True, timeout=10)
        return None if r.returncode == 0 else (r.stderr.strip() or "compile failed")[:300]
    except Exception as e:  # noqa: BLE001
        return str(e)


def to_tool(spec: LearnedSpec) -> T.Tool:
    enum_args = []
    text_arg = None
    for a in spec.args:
        if a.get("kind") == "enum":
            enum_args.append(T.EnumArg(a["name"], a.get("question", f"Which {a['name']}?"),
                                       {str(k): (v or None) for k, v in (a.get("options") or {}).items()}))
        elif text_arg is None:
            text_arg = T.TextArg(a["name"], a.get("question", f"The {a['name']} the user spoke."),
                                 optional=bool(a.get("optional", False)))
    tool = T.Tool(spec.name, spec.what, spec.examples,
                  run=lambda values, s=spec: s.run(values),
                  enum_args=enum_args, text_arg=text_arg, instant=text_arg is None)
    tool.learned = spec  # type: ignore[attr-defined]
    return tool


def register(spec: LearnedSpec) -> T.Tool:
    if spec.name in T.BY_NAME:
        T.TOOLS[:] = [t for t in T.TOOLS if t.name != spec.name]
    tool = to_tool(spec)
    T.TOOLS.append(tool)
    T.BY_NAME[spec.name] = tool
    return tool


def load_all() -> int:
    if not PATH.exists():
        return 0
    try:
        data = json.loads(PATH.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("learned.json unreadable: %s", e)
        return 0
    n = 0
    from . import policy
    for item in data:
        try:
            spec = LearnedSpec(**item)
            # learned.json is a plain file anyone can edit: re-apply the same gates generation used.
            if (why := policy.check(spec.script)):
                log.warning("skip learned tool %s: blocked (%s)", spec.name, why)
                continue
            if (note := policy.needs_confirmation(spec.script)) and not spec.risky:
                spec.risky, spec.risk_note = True, spec.risk_note or note
            register(spec)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("skip learned tool %s: %s", item.get("name"), e)
    return n


def save(spec: LearnedSpec) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    data = []
    if PATH.exists():
        try:
            data = json.loads(PATH.read_text())
        except Exception:  # noqa: BLE001
            data = []
    data = [d for d in data if d.get("name") != spec.name]
    data.append(asdict(spec))
    PATH.write_text(json.dumps(data, indent=2))


def safe_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return (s or "learned")[:40]
