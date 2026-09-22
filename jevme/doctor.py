"""`jevme-doctor` / `./run.sh doctor`: check everything jevme needs and say exactly how to fix what's missing.

The usual first-run failure is silent: a permission wasn't granted, so jevme hears nothing or can't click.
This names the problem and the fix instead.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

OK, WARN, FAIL = "✓", "•", "✗"
PRIVACY = "System Settings ▸ Privacy & Security ▸ {}"


def _host_app() -> str:
    """The app macOS attributes permissions to (the terminal jevme was launched from)."""
    term = os.environ.get("TERM_PROGRAM", "")
    return {"iTerm.app": "iTerm", "Apple_Terminal": "Terminal", "vscode": "Visual Studio Code"}.get(term, term or
                                                                                                   "your terminal app")


def check_permissions() -> list[tuple[str, str, str]]:
    host = _host_app()
    out = []
    try:
        import ApplicationServices as AS
        ok = bool(AS.AXIsProcessTrusted())
        out.append((OK if ok else FAIL, "Accessibility (read controls, click, type)",
                    "" if ok else f"enable {host} in {PRIVACY.format('Accessibility')}, then restart {host}"))
    except Exception as e:  # noqa: BLE001
        out.append((FAIL, "Accessibility", f"could not check: {e}"))
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        st = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        # 0 not determined, 1 restricted, 2 denied, 3 authorized
        mark, fix = {3: (OK, ""), 0: (WARN, "macOS will ask the first time jevme starts: click Allow")}.get(
            st, (FAIL, f"enable {host} in {PRIVACY.format('Microphone')}"))
        out.append((mark, "Microphone", fix))
    except Exception as e:  # noqa: BLE001
        out.append((FAIL, "Microphone", f"could not check: {e}"))
    try:
        from Speech import SFSpeechRecognizer
        st = int(SFSpeechRecognizer.authorizationStatus())
        mark, fix = {3: (OK, ""), 0: (WARN, "macOS will ask the first time jevme starts: click Allow")}.get(
            st, (FAIL, f"enable {host} in {PRIVACY.format('Speech Recognition')}"))
        out.append((mark, "Speech Recognition", fix))
    except Exception as e:  # noqa: BLE001
        out.append((FAIL, "Speech Recognition", f"could not check: {e}"))
    try:
        import Quartz
        ok = bool(Quartz.CGPreflightScreenCaptureAccess())
        out.append((OK if ok else WARN, "Screen Recording (screenshot fallback only)",
                    "" if ok else f"optional: enable {host} in {PRIVACY.format('Screen Recording')} so apps "
                                  "without accessibility info can still be controlled"))
    except Exception as e:  # noqa: BLE001
        out.append((WARN, "Screen Recording", f"could not check: {e}"))
    return out


def check_keys(live: bool = True) -> list[tuple[str, str, str]]:
    from . import config
    out = []
    if not config.TYPESAFE_API_KEY:
        out.append((FAIL, "TYPESAFE_API_KEY", "required: add it to .env (see .env.example)"))
    elif live:
        try:
            from .jev import JevClient, choice
            t0 = time.monotonic()
            a, _ = JevClient().ask({"utterance": "open chrome"},
                                   {"intent": choice("Which app?", {"Chrome": None, "Notes": None})}, retries=1)
            ms = int((time.monotonic() - t0) * 1000)
            ok = a.get("intent") is not None and a["intent"].choice == "Chrome"
            out.append((OK if ok else WARN, f"TYPESAFE_API_KEY (Jev answered in {ms} ms)",
                        "" if ok else "Jev answered unexpectedly; check the key"))
        except Exception as e:  # noqa: BLE001
            msg = str(e).splitlines()[0][:120]
            hint = "the key was rejected, so check it" if any(c in msg for c in ("401", "403")) else \
                "Jev may be down; try again in a minute"
            out.append((FAIL, "TYPESAFE_API_KEY", f"{msg} ({hint})"))
    else:
        out.append((OK, "TYPESAFE_API_KEY (set)", ""))
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        out.append((OK, "ANTHROPIC_API_KEY (set)", ""))
    elif shutil.which("claude"):
        out.append((WARN, "ANTHROPIC_API_KEY", "not set; using the `claude` CLI instead (slower)"))
    else:
        out.append((WARN, "ANTHROPIC_API_KEY", "not set: screenshot fallback, code writing and new tools are off"))
    return out


def main() -> int:
    offline = "--offline" in sys.argv
    print(f"jevme doctor — permissions belong to {_host_app()}\n")
    rows = check_permissions() + check_keys(live=not offline)
    for mark, name, fix in rows:
        print(f"  {mark} {name}" + (f"\n      → {fix}" if fix else ""))
    failed = sum(1 for m, _, _ in rows if m == FAIL)
    print("\n" + ("All set: ./run.sh start" if not failed else f"{failed} thing(s) to fix above, then run this again."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
