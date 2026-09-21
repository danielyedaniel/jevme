"""What generated tools may never do, and what needs a spoken "yes" first.

Jevme is meant to do almost anything you say, so this list is short and aimed at the
irreversible: privilege escalation, wiping storage, credentials, and the machine's own
security posture. Reversible-but-consequential things (deleting a note, sending a message,
quitting everything) run after you confirm out loud.
"""
from __future__ import annotations

import re

HARD_BLOCK: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsudo\b|with administrator privileges|\bsu\s+-", re.I), "admin privileges"),
    (re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b|\brm\s+-rf\b|\bshred\b|\bsrm\b|\bdd\s+if=", re.I), "recursive delete / wipe"),
    (re.compile(r"\bdiskutil\b|\bhdiutil\s+(erase|create)|\bcsrutil\b|\bnvram\b|\bfdesetup\b|\bbless\b", re.I), "disk / firmware"),
    (re.compile(r"\bkeychain\b|\bsecurity\s+(find|add|delete|dump)-|\bpassword\b|\bpasswd\b", re.I), "credentials"),
    (re.compile(r"\btccutil\b|\bspctl\b|\bsoftwareupdate\b|\blaunchctl\s+(unload|bootout|remove)", re.I), "system security / services"),
    (re.compile(r"\bshut\s*down\b|\breboot\b|\bhalt\b|\bkillall\s+(Finder|WindowServer|loginwindow|kernel)", re.I), "power / kill core services"),
    (re.compile(r"\bcrontab\b\s+-r|/System/|/usr/(bin|sbin|lib)|/private/etc|/etc/", re.I), "system paths"),
    (re.compile(r"\bcurl\b[^\n]*\|\s*(sh|bash|zsh)\b|\bwget\b[^\n]*\|\s*(sh|bash|zsh)\b", re.I), "pipe-to-shell"),
]

NEEDS_CONFIRM: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdelete\b|\bmove\s+(to\s+)?trash\b|\brm\s|\bempty\s+(the\s+)?trash", re.I), "deletes something"),
    (re.compile(r"\bsend\b|\bsubmit\b|\bpost\b|\bpublish\b|\bpay\b|\bpurchase\b|\bbuy\b|\bcheckout\b", re.I), "sends or pays"),
    (re.compile(r"\bquit\s+(every|all)\b|\bkillall\b|\bpkill\b|\bkill\s", re.I), "kills processes"),
    (re.compile(r"\bdefaults\s+write\b|\bnetworksetup\b|\bsystemsetup\b|\bpmset\s+(?!-g)", re.I), "changes a system setting"),
    (re.compile(r"\bmv\b|\bcp\s+-r|\bchmod\b|\bchown\b|\bln\s+-s", re.I), "moves or changes files"),
    (re.compile(r"\blog\s*out\b|\block\b.*\bscreen\b", re.I), "ends your session"),
]


def check(script: str) -> str | None:
    """Return a reason if the script is forbidden outright, else None."""
    for pat, why in HARD_BLOCK:
        if pat.search(script):
            return why
    return None


def needs_confirmation(script: str) -> str | None:
    for pat, why in NEEDS_CONFIRM:
        if pat.search(script):
            return why
    return None


# ---------- on-screen actions the user didn't ask for ----------

# A control that commits something, and the words that count as asking for it. The log: "implement the
# solution" ended with the agent clicking LeetCode's Submit — the user only wanted the code written.
_COMMIT_CONTROLS: list[tuple[re.Pattern, re.Pattern]] = [
    (re.compile(r"\bsubmit\b", re.I), re.compile(r"\bsubmit", re.I)),
    # "message sarah" / "text mom that…" ask to send; "compose a new message" / "draft an email" do not.
    (re.compile(r"\b(send|send now|reply all)\b", re.I),
     re.compile(r"\b(send|reply|tell|dm)\b|^(?!.*\b(draft|compose|write)\b).*\b(message|text|e-?mail)\s+"
                r"(?!(a|an|the|new|to|draft|app|box)\b)\w", re.I)),
    (re.compile(r"\b(post|publish|tweet)\b", re.I), re.compile(r"\b(post|publish|tweet|share)\b", re.I)),
    (re.compile(r"\b(pay|buy|purchase|place (your )?order|checkout|check out)\b", re.I),
     re.compile(r"\b(pay|buy|purchase|order|checkout|check out)\b", re.I)),
    (re.compile(r"\b(delete|remove|trash|discard|erase)\b", re.I),
     re.compile(r"\b(delete|remove|trash|discard|erase|clear|get rid|throw away)\b", re.I)),
    (re.compile(r"\b(sign out|log out|logout|unsubscribe|deactivate)\b", re.I),
     re.compile(r"\b(sign out|log out|logout|unsubscribe|deactivate)\b", re.I)),
]


def unrequested_commit(label: str, request: str) -> str | None:
    """If clicking `label` would commit something (submit, send, pay, delete...) that `request` never asked
    for, return what it would do; else None."""
    for control, asked in _COMMIT_CONTROLS:
        m = control.search(label or "")
        if m and not asked.search(request or ""):
            return m.group(0).lower()
    return None
