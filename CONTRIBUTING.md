# Contributing to jevme

Thanks for helping! The quickest way in is to fix something that failed for you.

## Setup

```bash
git clone https://github.com/danielyedaniel/jevme.git && cd jevme
./run.sh setup        # deps + .env + permission check
uv run pytest         # unit tests: no mic, no network, no keys
```

## Reproduce without a microphone

```bash
uv run jevme-replay "open spotify and play drake"      # real Jev routing, dry-run actions
uv run python -m jevme.evalrun                        # routing eval against real Jev (needs a key)
```

Most fixes start from a log line. Run `./run.sh log 80` after something goes wrong and look for `route`,
`COMMIT`, `→ agent`, `step N`, and `jev error`.

## Easy first contributions

- **A new shortcut tool** (`jevme/tools.py`): a name, a one-sentence `what`, a few spoken `examples`, and a
  `run`. It's in the next Jev request automatically. Add a routing case to `jevme/evalrun.py`.
- **Site names** (`WELL_KNOWN_SITES` in `jevme/actions.py`) and **app hints** (`APP_HINTS` in `jevme/agent.py`).
- **Routing eval cases** for commands you use, with the tools that are acceptable answers.

## Ground rules

- **Jev classifies; models reason; results are memoized.** Don't put an LLM call where a Jev Choice over
  listed options would do. If you add a slow path, make its result something Jev can match next time.
- Nothing consequential (send, submit, pay, delete) without the user asking for it in words.
- Never log or store what the user types, keys, or secure-field contents.
- Add a test for the bug you fixed (`tests/`), and keep `uv run pytest` green.
