"""Screenshot fallback: when the accessibility tree has nothing useful, ask Claude to look.

Captures the front window (or screen), sends it with the request, gets back one action with image
coordinates, and performs it. Slower (a few seconds) and only used when the fast path fails.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time

from AppKit import NSScreen

from . import actions as A
from . import ax

log = logging.getLogger("jevme.vision")

# Haiku is the fast/cheap vision tier for "where do I click"; Opus only if you set it.
MODEL = os.environ.get("JEVME_VISION_MODEL", "claude-haiku-4-5")
CLI_MODEL = os.environ.get("JEVME_VISION_CLI_MODEL", "haiku")
MAX_W = 1400

PROMPT = """You are the eyes of a macOS voice assistant. The user said: "{request}"
The image is the current front window of {app}. Decide the ONE next action that carries out the request.
Reply with ONLY a JSON object:
{{"action": "click" | "double_click" | "right_click" | "type" | "scroll_down" | "scroll_up" | "done" | "none",
  "x": <int px in the image>, "y": <int px in the image>, "text": "<text to type, if action is type>",
  "target": "<what you are clicking, a few words>", "reason": "<why, briefly>"}}
Coordinates are pixels in the image as given (top-left origin). Use "done" if the screen already shows the
request fully carried out — but text typed into a message box is NOT sent: if the request is to send
something and it is still sitting in the compose box, click Send (or choose none). Never click Submit,
Send, Post, Buy/Pay, or Delete unless the request itself asks for that. If nothing on screen matches, use
"none"."""

# Structured outputs (documented for claude-haiku-4-5): the API guarantees the reply is valid JSON matching
# this schema, so no text repair is needed. The log showed two failure shapes from free-text JSON — "Extra
# data" (the greedy {.*} grabbed past the object) and broken quoting — each falling back to an 11 s CLI call.
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["click", "double_click", "right_click", "type",
                                              "scroll_down", "scroll_up", "done", "none"]},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "text": {"type": "string"},
        "target": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "x", "y", "text", "target", "reason"],
    "additionalProperties": False,
}


def _capture(snap: ax.Snapshot) -> tuple[str, float, float, float]:
    """Screenshot the front window. Returns (path, origin_x, origin_y, points_per_image_px)."""
    path = os.path.join(tempfile.gettempdir(), "jevme_look.png")
    if snap.window:
        x, y, w, h = snap.window
        r = subprocess.run(["screencapture", "-x", "-R", f"{int(x)},{int(y)},{int(w)},{int(h)}", path],
                           capture_output=True, text=True)
    else:
        f = NSScreen.mainScreen().frame()
        x, y, w, h = 0.0, 0.0, f.size.width, f.size.height
        r = subprocess.run(["screencapture", "-x", path], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(path):
        raise RuntimeError("cannot capture the screen (locked, asleep, or Screen Recording permission missing)")
    # Downscale for the model; remember the ratio back to screen points.
    info = subprocess.run(["sips", "-g", "pixelWidth", path], capture_output=True, text=True).stdout
    px_w = int(re.search(r"pixelWidth:\s*(\d+)", info).group(1))
    if px_w > MAX_W:
        subprocess.run(["sips", "--resampleWidth", str(MAX_W), path], capture_output=True, check=True)
        px_w = MAX_W
    return path, x, y, w / px_w


def _ask(path: str, request: str, app: str) -> dict:
    prompt = PROMPT.format(request=request.replace('"', "'"), app=app or "the front app")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        # With an API key, never fall back to the ~11 s CLI: a failure here should surface fast.
        import anthropic
        client = anthropic.Anthropic()
        with open(path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode()
        output_config: dict = {"format": {"type": "json_schema", "schema": SCHEMA}}
        if "haiku" not in MODEL:                     # effort is not accepted on Haiku 4.5
            output_config["effort"] = "low"
        r = client.messages.create(
            model=MODEL, max_tokens=600, output_config=output_config,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                {"type": "text", "text": prompt}]}])
        if r.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"vision stopped: {r.stop_reason}")
        text = next((b.text for b in r.content if b.type == "text"), "")
        return json.loads(text)
    # No API key: the Claude Code CLI can view an image file with its Read tool.
    cli_prompt = f"Read the image file at {path} (it is a screenshot), then answer this.\n\n{prompt}"
    r = subprocess.run(["claude", "-p", cli_prompt, "--model", CLI_MODEL, "--output-format", "text",
                        "--allowedTools", "Read"],
                       capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200] or "claude cli failed")
    return _parse(r.stdout)


def _parse(text: str) -> dict:
    """Parse the first JSON object in free text (CLI path only; the API path uses structured outputs).

    raw_decode stops at the end of the first complete object, so trailing prose or a second object can't
    cause "Extra data" the way a greedy {.*} regex did."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj
    raise ValueError("no JSON action object from vision model")


def act(request: str, snap: ax.Snapshot, intent: str = "click") -> str:
    return act_located(request, snap, intent)[0]


def act_located(request: str, snap: ax.Snapshot, intent: str = "click"):
    """Like act(), but also returns the screen point acted on (or None), so the caller can learn it."""
    t0 = time.monotonic()
    path, ox, oy, ratio = _capture(snap)
    d = _ask(path, request, snap.app)
    action = str(d.get("action", "none"))
    log.info("vision: %s at (%s,%s) target=%r reason=%r in %d ms", action, d.get("x"), d.get("y"),
             d.get("target"), d.get("reason"), int((time.monotonic() - t0) * 1000))
    if action == "done":
        return "Done", None
    if action == "none":
        return "Couldn't find that on screen", None
    sx = ox + float(d.get("x", 0)) * ratio
    sy = oy + float(d.get("y", 0)) * ratio
    point = (sx, sy)
    target = str(d.get("target") or "it")
    from . import policy
    if action in ("click", "double_click") and (what := policy.unrequested_commit(target, request)):
        log.info("vision: refused to %s (%r) — not requested", what, target)
        return f"Didn't {what}: not asked to", None
    if action in ("click", "double_click", "right_click"):
        ax._click_at(sx, sy, "right" if action == "right_click" else "left", 2 if action == "double_click" else 1)
        return f"Clicked {target}", point
    if action == "type":
        ax._click_at(sx, sy)
        time.sleep(0.12)
        A.type_text(str(d.get("text", "")))
        return f"Typed into {target}", point
    if action in ("scroll_down", "scroll_up"):
        ax.scroll_at(sx, sy, -10 if action == "scroll_down" else 10)
        return f"Scrolled {action.split('_')[1]}", None
    return "Did nothing", None
