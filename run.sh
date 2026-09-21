#!/bin/zsh
# Set up / start / stop / log jevme.  Usage: ./run.sh setup|start|stop|status|log
cd "$(dirname "$0")"
LOG="${JEVME_LOG_FILE:-$HOME/Library/Logs/jevme/jevme.log}"
mkdir -p "$(dirname "$LOG")"
case "${1:-start}" in
  setup)
    command -v uv >/dev/null || { echo "Install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
    uv sync || exit 1
    if [[ ! -f .env && ! -f "$HOME/.config/jevme/.env" ]]; then
      cp .env.example .env
      echo "Created .env — open it and paste your TYPESAFE_API_KEY (and ANTHROPIC_API_KEY)."
    fi
    grep -qE '^TYPESAFE_API_KEY=.+' .env "$HOME/.config/jevme/.env" 2>/dev/null \
      && echo "✓ TYPESAFE_API_KEY set" || echo "✗ TYPESAFE_API_KEY is empty (required)"
    grep -qE '^ANTHROPIC_API_KEY=.+' .env "$HOME/.config/jevme/.env" 2>/dev/null \
      && echo "✓ ANTHROPIC_API_KEY set" || echo "• ANTHROPIC_API_KEY empty (optional; uses the claude CLI if present)"
    cat <<'MSG'

Grant these to the app you launch jevme from (Terminal / iTerm / VS Code), in
System Settings ▸ Privacy & Security:  Microphone, Speech Recognition, Accessibility,
Screen Recording (screenshot fallback). macOS prompts for the first two on first run.

Then:  ./run.sh start    (logs: ./run.sh log)
MSG
    ;;
  start)
    pgrep -f "jevme.main|bin/jevme" >/dev/null && { echo "already running"; exit 0; }
    # Append, never truncate: the log is the usage history. Rotate at 20 MB, keep 5.
    if [[ -f "$LOG" && $(stat -f%z "$LOG") -gt 20000000 ]]; then
      for i in 4 3 2 1; do [[ -f "$LOG.$i" ]] && mv "$LOG.$i" "$LOG.$((i+1))"; done
      mv "$LOG" "$LOG.1"
    fi
    echo "===== jevme start $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"
    nohup uv run jevme >> "$LOG" 2>&1 &
    sleep 2; echo "started, log: $LOG" ;;
  stop)   pkill -f "jevme.main|bin/jevme"; echo "stopped" ;;
  status) pgrep -fl "jevme.main|bin/jevme" || echo "not running" ;;
  log)    grep -vE "httpx|HTTP Request" "$LOG" | tail -${2:-40} ;;
  *) echo "usage: $0 setup|start|stop|status|log"; exit 1 ;;
esac
