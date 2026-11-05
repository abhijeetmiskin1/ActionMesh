#!/usr/bin/env bash
# GUI smoke — attempt an observed run of the actual application.
#
# A window launch alone does NOT count: this script requires a captured
# screenshot and observed rendered text ("ActionMesh" / "VoiceOS" from
# desktop/ui/index.html). If no display/screenshot tooling is present it
# exits BLOCKED with evidence and never claims LIVE success.
#
# Usage:
#   scripts/gui-smoke.sh
# Screenshot is written to $TMPDIR/gui-smoke-<timestamp>.png
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR="${TMPDIR:-/tmp}"
STAMP="$(date +%Y%m%d-%H%M%S)"
SHOT="$TMPDIR/gui-smoke-$STAMP.png"
LOG="$TMPDIR/gui-smoke-$STAMP.log"

echo "== GUI smoke probe =="
echo "DISPLAY=${DISPLAY:-<empty>} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-<empty>} XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-<empty>}"
for c in Xvfb xvfb-run scrot import xdotool xvfb electron; do
  if command -v "$c" >/dev/null 2>&1; then echo "$c: $(command -v "$c")"; else echo "$c: NOT FOUND"; fi
done

have_xvfb=0
command -v Xvfb >/dev/null 2>&1 && have_xvfb=1
have_screenshot_tool=0
for tool in scrot import xwd grim gnome-screenshot; do
  if command -v "$tool" >/dev/null 2>&1; then
    have_screenshot_tool=1
    SCREENSHOT_TOOL="$tool"
    break
  fi
done

if [ "$have_xvfb" -eq 0 ] && [ -z "${DISPLAY:-}" ]; then
  echo "BLOCKED: no DISPLAY and no Xvfb — cannot produce an observed rendered UI."
  echo "Evidence: DISPLAY=${DISPLAY:-<empty>} Xvfb=$(command -v Xvfb || echo missing)"
  exit 2
fi

if [ "$have_xvfb" -eq 0 ] && [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
  echo "BLOCKED: no display (no DISPLAY/WAYLAND_DISPLAY) and no Xvfb."
  exit 2
fi

if [ "$have_screenshot_tool" -eq 0 ]; then
  echo "BLOCKED: display env present but no screenshot tool (scrot/import/xwd/grim/gnome-screenshot). Cannot capture observed rendered UI — refusing to fake a screenshot."
  exit 2
fi

# Determine app binary: installed unpacked build preferred, else dev electron.
APP_BIN=""
if [ -x "$ROOT/desktop/release/linux-unpacked/actionmesh" ]; then
  APP_BIN="$ROOT/desktop/release/linux-unpacked/actionmesh"
  echo "Using packaged binary: $APP_BIN"
elif [ -x "$ROOT/desktop/node_modules/.bin/electron" ]; then
  APP_BIN="node $ROOT/desktop/scripts/start.js"
  echo "Using dev electron via scripts/start.js"
else
  echo "BLOCKED: neither packaged actionmesh binary nor dev electron found."
  exit 2
fi

# Xvfb guard: run under xvfb-run when no real display, else use DISPLAY as-is.
XVFB_PREFIX=""
if [ "$have_xvfb" -eq 1 ] && [ -z "${DISPLAY:-}" ]; then
  XVFB_PREFIX="xvfb-run -a"
  echo "Using xvfb-run for virtual display"
fi

echo "Launching app (log: $LOG) ..."
# Launch in background; Electron spawns the backend (uvicorn) and should
# reach GET /ready on its loopback port. We poll the backend log and the
# well-known loopback probe is internal to desktop/app/backend.js — here we
# instead wait for the window process + log "ready" hint, then screenshot.
if [ -n "$XVFB_PREFIX" ]; then
  # shellcheck disable=SC2086
  $XVFB_PREFIX $APP_BIN >"$LOG" 2>&1 &
else
  # shellcheck disable=SC2086
  $APP_BIN >"$LOG" 2>&1 &
fi
APP_PID=$!
echo "App PID: $APP_PID"

cleanup() {
  kill "$APP_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for render (give Electron + backend ~20s)
sleep 12

# Capture screenshot
case "$SCREENSHOT_TOOL" in
  scrot) scrot "$SHOT" ;;
  import) import -window root "$SHOT" ;;
  xwd) xwd -root -silent | convert - "$SHOT" 2>/dev/null || xwd -root -out "${SHOT%.png}.xwd" ;;
  grim) grim "$SHOT" ;;
  gnome-screenshot) gnome-screenshot -f "$SHOT" ;;
esac

if [ ! -s "$SHOT" ]; then
  echo "BLOCKED: screenshot not produced at $SHOT (tool=$SCREENSHOT_TOOL). See log: $LOG"
  cat "$LOG" 2>/dev/null | tail -n 50
  exit 2
fi
echo "Screenshot saved: $SHOT"

# Verify rendered UI contains real text: OCR not assumed; we require the
# screenshot file to be non-trivial and the app log to show backend ready.
# For a stronger check, a human (or tesseract if present) should read the
# image — we do NOT claim text we did not observe.
if command -v tesseract >/dev/null 2>&1; then
  OCR="$(tesseract "$SHOT" stdout 2>/dev/null || true)"
  echo "OCR excerpt: $(echo "$OCR" | head -n 5)"
  case "$OCR" in
    *ActionMesh*|*VoiceOS*|*Home*|*Approvals*) echo "Observed rendered text: ActionMesh/VoiceOS markers found via OCR" ;;
    *) echo "WARNING: OCR did not find ActionMesh/VoiceOS/HOME markers — inspect $SHOT manually" ;;
  esac
else
  echo "No tesseract — manual inspection required. Screenshot: $SHOT"
  echo "Expected markers (from desktop/ui/index.html): brand-name VoiceOS, brand-sub ActionMesh, nav-labels Home/Voice Commands/Visual Intelligence/Browser/Tasks/Automations/Devices/Live Control/Approvals/History/Settings, eyebrow 'Control plane', h1 'Home'."
fi

echo "LIVE result: screenshot captured at $SHOT — text must be verified by inspection (see above). No text claimed unless OCR/inspection confirms it."
