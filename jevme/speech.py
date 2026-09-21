"""Streaming speech recognition with Apple's Speech framework.

Partial results arrive every ~100-300 ms. Apple caps one recognition request at about a minute, so
the engine quietly starts a fresh request when the user pauses, and the router's cursor resets.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable

import AVFoundation
import Foundation
import Speech
from PyObjCTools import AppHelper

from . import config

log = logging.getLogger("jevme.speech")

_VOCAB_CACHE: list[str] | None = None


def _contextual_vocab() -> list[str]:
    """Words to bias the recognizer toward: known site names, installed app names, and command verbs."""
    global _VOCAB_CACHE
    if _VOCAB_CACHE is not None:
        return _VOCAB_CACHE
    from . import actions as A
    sites = list(A.WELL_KNOWN_SITES) + list(A.SITE_SEARCH)
    dotted = [f"{s} dot com" for s in ("leetcode", "piazza", "github", "amazon", "youtube")]
    verbs = ["leetcode", "piazza", "gradescope", "neetcode", "codeforces", "Waterloo", "open", "close",
             "search", "scroll", "click", "type", "paste", "new tab", "new note", "dark mode"]
    try:
        apps = A.installed_apps()
    except Exception:  # noqa: BLE001
        apps = []
    _VOCAB_CACHE = list(dict.fromkeys(sites + dotted + verbs + apps))[:400]
    return _VOCAB_CACHE


class SpeechEngine:
    def __init__(self, *, on_partial: Callable[[str, bool], None], on_level: Callable[[float], None],
                 on_session_reset: Callable[[], None], on_status: Callable[[str], None]) -> None:
        self.on_partial = on_partial
        self.on_level = on_level
        self.on_session_reset = on_session_reset
        self.on_status = on_status

        locale = Foundation.NSLocale.alloc().initWithLocaleIdentifier_(config.LOCALE)
        self.recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(locale)
        if self.recognizer is None:
            raise RuntimeError(f"No speech recognizer for {config.LOCALE}")
        self.on_device = bool(self.recognizer.supportsOnDeviceRecognition())
        self.engine = AVFoundation.AVAudioEngine.alloc().init()
        self.request = None
        self.task = None
        self.session_started = 0.0
        self.last_text = ""
        self.last_text_t = 0.0
        self.running = False
        self._lock = threading.Lock()
        self._level_t = 0.0
        self._gen = 0

    # ---------- lifecycle ----------

    def authorize(self, done: Callable[[bool], None]) -> None:
        status = Speech.SFSpeechRecognizer.authorizationStatus()
        if status == 3:
            done(True)
            return

        def cb(s):
            AppHelper.callAfter(done, s == 3)
        Speech.SFSpeechRecognizer.requestAuthorization_(cb)

    def start(self) -> None:
        if self.running:
            return
        node = self.engine.inputNode()
        fmt = node.outputFormatForBus_(0)
        node.installTapOnBus_bufferSize_format_block_(0, 2048, fmt, self._tap)
        self.engine.prepare()
        ok, err = self.engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"audio engine: {err}")
        self.running = True
        self._new_request()
        self.on_status("listening" + (" (on-device)" if self.on_device else ""))
        log.info("speech started, on_device=%s", self.on_device)

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self._end_request()
        self.engine.inputNode().removeTapOnBus_(0)
        self.engine.stop()
        self.on_status("paused")

    # ---------- audio ----------

    def _tap(self, buffer, when) -> None:
        with self._lock:
            req = self.request
        if req is not None:
            req.appendAudioPCMBuffer_(buffer)
        now = time.monotonic()
        if now - self._level_t >= 0.05:
            self._level_t = now
            AppHelper.callAfter(self.on_level, self._rms(buffer))

    @staticmethod
    def _rms(buffer) -> float:
        try:
            n = int(buffer.frameLength())
            if n == 0:
                return 0.0
            data = buffer.floatChannelData()[0]
            step = max(1, n // 256)
            acc = 0.0
            cnt = 0
            for i in range(0, n, step):
                v = data[i]
                acc += v * v
                cnt += 1
            rms = math.sqrt(acc / max(cnt, 1))
            db = 20 * math.log10(rms + 1e-7)
            return max(0.0, min(1.0, (db + 58) / 42))
        except Exception:  # noqa: BLE001
            return 0.0

    # ---------- recognition ----------

    def _new_request(self) -> None:
        self._end_request()
        req = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
        req.setShouldReportPartialResults_(True)
        try:
            req.setAddsPunctuation_(True)
        except Exception:  # noqa: BLE001
            pass
        # Bias recognition toward vocabulary the user actually says: site names, app names, command words.
        # This is why "leetcode" was heard as "lico" — the recognizer had no hint it was a real word.
        try:
            req.setContextualStrings_(_contextual_vocab())
        except Exception:  # noqa: BLE001
            pass
        if self.on_device:
            req.setRequiresOnDeviceRecognition_(True)
        self._gen += 1
        gen = self._gen
        with self._lock:
            self.request = req
        self.session_started = time.monotonic()
        self.last_text = ""
        self.last_text_t = time.monotonic()
        self.task = self.recognizer.recognitionTaskWithRequest_resultHandler_(
            req, lambda result, error: self._on_result(gen, result, error))
        self.on_session_reset()

    def _end_request(self) -> None:
        with self._lock:
            req, self.request = self.request, None
        if req is not None:
            req.endAudio()
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def _on_result(self, gen: int, result, error) -> None:
        if gen != self._gen:
            return
        if result is not None:
            text = str(result.bestTranscription().formattedString())
            final = bool(result.isFinal())
            if text != self.last_text:
                self.last_text = text
                self.last_text_t = time.monotonic()
            AppHelper.callAfter(self.on_partial, text, final)
            if final and self.running:
                AppHelper.callAfter(self._new_request)
        if error is not None and self.running:
            desc = str(error.localizedDescription())
            if "canceled" in desc.lower() or "cancelled" in desc.lower():
                return
            log.info("recognition ended: %s", desc)
            AppHelper.callAfter(self._restart_soon)

    def _restart_soon(self) -> None:
        if self.running and self.request is None:
            self._new_request()

    def maybe_roll_session(self, pending_empty: bool) -> None:
        """Start a fresh request at a quiet moment so we never hit Apple's per-request limit."""
        if not self.running or self.request is None:
            return
        age = time.monotonic() - self.session_started
        idle = time.monotonic() - self.last_text_t
        if (pending_empty and idle >= 1.2 and age >= 4.0 and self.last_text) or (age >= config.SESSION_MAX_S and idle >= 0.5):
            log.debug("rolling recognition session (age=%.0fs idle=%.1fs)", age, idle)
            self._new_request()
