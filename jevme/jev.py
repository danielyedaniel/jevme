"""Minimal TypeSafe Jev (System One) client. One HTTPS POST per decision, ~300 ms."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from . import config

log = logging.getLogger("jevme.jev")

ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def choice(instructions: str | list[str], criteria: dict[str, Any]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true, "false": false}
    return q


@dataclass
class Answer:
    kind: str
    choice: str | None = None
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    noul: float = 0.0


@dataclass
class Decision:
    seq: int
    answers: dict[str, Answer]
    latency_ms: int


class JevClient:
    def __init__(self) -> None:
        if not config.TYPESAFE_API_KEY:
            raise RuntimeError("TYPESAFE_API_KEY missing: add it to .env in the repo (see .env.example) "
                               "or to ~/.config/jevme/.env")
        self._client = httpx.Client(
            timeout=httpx.Timeout(6.0, connect=3.0),
            headers={"authorization": f"Bearer {config.TYPESAFE_API_KEY}",
                     "content-type": "application/json", "user-agent": "jevme/0.1"},
            http2=False,
        )
        self._lock = threading.Lock()

    def ask(self, state: dict, questions: dict, retries: int = 2) -> tuple[dict[str, Answer], int]:
        """One decision. Transient failures (timeouts, 429, 5xx — the log showed a burst of 503s and a
        read timeout that crashed a whole agent task) are retried with a short backoff."""
        body = {"model": config.JEV_MODEL, "state": state, "questions": questions}
        t0 = time.monotonic()
        for attempt in range(retries + 1):
            try:
                r = self._client.post(ENDPOINT, json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    r.raise_for_status()
                break
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                if attempt == retries or (isinstance(e, httpx.HTTPStatusError)
                                          and e.response.status_code < 500 and e.response.status_code != 429):
                    raise
                log.info("jev %s; retrying", e.__class__.__name__)
                time.sleep(0.3 * (attempt + 1))
        r.raise_for_status()
        data = r.json()
        answers: dict[str, Answer] = {}
        for name, a in data.get("answers", {}).items():
            if a.get("type") == "choice":
                answers[name] = Answer("choice", a.get("choice"), float(a.get("confidence", 0)),
                                       {k: float(v) for k, v in a.get("probabilities", {}).items()})
            elif a.get("type") == "noul":
                answers[name] = Answer("noul", noul=float(a.get("noul", 0)))
        return answers, int((time.monotonic() - t0) * 1000)   # includes any retries

    def ask_async(self, seq: int, state: dict, questions: dict,
                  on_done: Callable[[Decision], None], on_error: Callable[[int, Exception], None]) -> None:
        def run() -> None:
            try:
                answers, ms = self.ask(state, questions)
                on_done(Decision(seq, answers, ms))
            except Exception as e:  # noqa: BLE001
                on_error(seq, e)
        threading.Thread(target=run, daemon=True, name=f"jev-{seq}").start()
