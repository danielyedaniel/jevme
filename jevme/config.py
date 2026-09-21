import os
from pathlib import Path

# Keys are read from the first of these that exists (later files only fill in what's missing):
#   1. <repo>/.env            — simplest after cloning: `cp .env.example .env` and fill it in
#   2. ~/.config/jevme/.env   — keeps keys out of the checkout entirely
# Real environment variables always win over both. Neither file is ever committed (see .gitignore).
REPO_ENV = Path(__file__).resolve().parent.parent / ".env"
ENV_PATH = Path.home() / ".config" / "jevme" / ".env"
DATA_DIR = Path.home() / ".config" / "jevme"      # learned tools + memory live here, never in the repo


def load_env() -> None:
    """Load KEY=VALUE lines from the .env files into os.environ (without overriding)."""
    for path in (REPO_ENV, ENV_PATH):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


load_env()

TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
JEV_MODEL = os.environ.get("JEVME_JEV_MODEL", "jev-latest")
LOCALE = os.environ.get("JEVME_LOCALE", "en-US")

# Decision thresholds
COMMIT_CONFIDENCE = float(os.environ.get("JEVME_COMMIT_CONFIDENCE", "0.80"))
STABLE_PARTIALS = int(os.environ.get("JEVME_STABLE_PARTIALS", "2"))      # same decision N partials in a row
TEXT_PAUSE_S = float(os.environ.get("JEVME_TEXT_PAUSE_S", "0.65"))       # pause before committing a free-text arg
DEBOUNCE_S = float(os.environ.get("JEVME_DEBOUNCE_S", "0.10"))           # min gap between Jev requests
REFIRE_GUARD_S = 4.0                                                     # ignore identical action within this window
SESSION_MAX_S = 50.0                                                     # Apple caps a recognition request near 60 s
