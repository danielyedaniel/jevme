#!/bin/zsh
# Wrap Jevme in a .app so it can live in /Applications, be launched from Spotlight, and own its
# own microphone / speech / accessibility permissions instead of borrowing the terminal's.
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
APP="$ROOT/dist/Jevme.app"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Jevme</string>
  <key>CFBundleDisplayName</key><string>Jevme</string>
  <key>CFBundleIdentifier</key><string>dev.jevme.app</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Jevme</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Jevme listens so you can talk to your Mac.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Jevme turns your speech into commands.</string>
  <key>NSAppleEventsUsageDescription</key><string>Jevme controls apps on your behalf.</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/Jevme" <<SH
#!/bin/zsh
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
cd "$ROOT"
mkdir -p "$HOME/Library/Logs/jevme"
exec "$ROOT/.venv/bin/python" -m jevme.main >> "$HOME/Library/Logs/jevme/jevme.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/Jevme"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "built $APP"
echo "open it:  open \"$APP\"     (first launch asks for Microphone, Speech Recognition, Accessibility)"
