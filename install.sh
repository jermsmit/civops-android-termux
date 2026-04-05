#!/data/data/com.termux/files/usr/bin/bash
# ─────────────────────────────────────────────
# CIVOPS — Termux Install Script v1.0.0
# ─────────────────────────────────────────────

set -e

CIVOPS_DIR="$HOME/civops"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
BOLD=$(tput bold 2>/dev/null || echo '')
RESET=$(tput sgr0 2>/dev/null || echo '')

banner() {
  echo ""
  echo -e "${GREEN}${BOLD}"
  echo "  ██████╗██╗██╗   ██╗ ██████╗ ██████╗ ███████╗"
  echo " ██╔════╝██║██║   ██║██╔═══██╗██╔══██╗██╔════╝"
  echo " ██║     ██║██║   ██║██║   ██║██████╔╝███████╗"
  echo " ██║     ██║╚██╗ ██╔╝██║   ██║██╔═══╝ ╚════██║"
  echo " ╚██████╗██║ ╚████╔╝ ╚██████╔╝██║     ███████║"
  echo "  ╚═════╝╚═╝  ╚═══╝   ╚═════╝ ╚═╝     ╚══════╝"
  echo -e "  Signal Recon Platform — Termux Edition v1.0.0${RESET}"
  echo ""
}

info()    { echo -e "${GREEN}[+]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }

check_termux() {
  if [ ! -d "/data/data/com.termux" ]; then
    warn "Not running in Termux. Some features may not work."
  else
    info "Termux environment detected."
  fi
}

install_packages() {
  info "Updating package lists..."
  pkg update -y -q 2>/dev/null || warn "pkg update had warnings (continuing)"
  info "Installing required packages..."
  pkg install -y python termux-api 2>/dev/null || error "Failed to install packages."
  success "Packages installed."
}

install_python_deps() {
  info "Verifying Python stdlib (no pip deps required)..."
  python3 -c "import sqlite3, json, threading, http.server, subprocess" \
    && success "Python stdlib verified." \
    || error "Python stdlib check failed."
}

setup_dirs() {
  info "Setting up CIVOPS directory structure..."

  # Detect where files currently are and move if needed
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

  mkdir -p "$CIVOPS_DIR/backend"
  mkdir -p "$CIVOPS_DIR/frontend"
  mkdir -p "$HOME/.civops"

  # Move files into correct subdirectories if they're flat in civops/
  if [ -f "$SCRIPT_DIR/server.py" ] && [ ! -f "$CIVOPS_DIR/backend/server.py" ]; then
    cp "$SCRIPT_DIR/server.py" "$CIVOPS_DIR/backend/server.py"
    info "Moved server.py → backend/"
  fi
  if [ -f "$SCRIPT_DIR/index.html" ] && [ ! -f "$CIVOPS_DIR/frontend/index.html" ]; then
    cp "$SCRIPT_DIR/index.html" "$CIVOPS_DIR/frontend/index.html"
    info "Moved index.html → frontend/"
  fi

  # Verify required files exist
  [ -f "$CIVOPS_DIR/backend/server.py" ]  || error "backend/server.py not found in $CIVOPS_DIR"
  [ -f "$CIVOPS_DIR/frontend/index.html" ] || error "frontend/index.html not found in $CIVOPS_DIR"

  success "Directories ready."
}

request_permissions() {
  info "Checking permissions..."
  info "Requesting location permission (required by Android for Wi-Fi scanning)..."
  termux-location -p network -r once &>/dev/null &
  LPID=$!
  sleep 3
  kill $LPID 2>/dev/null || true
  success "Location permission check complete."
}

verify_api() {
  info "Testing Termux:API — Wi-Fi scan..."
  WIFI_RESULT=$(termux-wifi-scaninfo 2>/dev/null || echo "[]")
  if echo "$WIFI_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if isinstance(d,list) else 1)" 2>/dev/null; then
    COUNT=$(echo "$WIFI_RESULT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
    if [ "$COUNT" -gt 0 ]; then
      success "Wi-Fi scan OK — $COUNT networks found."
    else
      warn "Wi-Fi scan returned empty. Check:"
      warn "  1. Termux:API app installed from F-Droid"
      warn "  2. Settings → Apps → Termux:API → Permissions → Location → Allow all the time"
      warn "  3. Settings → Apps → Termux → Permissions → Location → Allow all the time"
      warn "  4. Wi-Fi radio is enabled"
    fi
  else
    warn "Wi-Fi scan unavailable. Termux:API may need permissions."
  fi

  info "Testing Termux:API — Cell info..."
  if termux-telephony-cellinfo 2>/dev/null | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    success "Cell scan OK."
  else
    warn "Cell scan unavailable. Ensure Termux:API has Phone permission."
  fi
}

create_launcher() {
  cat > "$CIVOPS_DIR/civops.sh" << 'LAUNCHER'
#!/data/data/com.termux/files/usr/bin/bash
CIVOPS_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${CIVOPS_PORT:-8888}"

echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║  CIVOPS Signal Recon — Starting   ║"
echo "  ╚═══════════════════════════════════╝"
echo ""
echo "  URL : http://127.0.0.1:${PORT}"
echo "  DB  : ~/.civops/civops.db"
echo "  Stop: Ctrl+C"
echo ""

(sleep 2 && termux-open-url "http://127.0.0.1:${PORT}" 2>/dev/null) &

CIVOPS_PORT="${PORT}" python3 "$CIVOPS_DIR/backend/server.py"
LAUNCHER
  chmod +x "$CIVOPS_DIR/civops.sh"
  success "Launcher created: $CIVOPS_DIR/civops.sh"
}

create_alias() {
  ALIAS_LINE="alias civops='bash $CIVOPS_DIR/civops.sh'"
  if ! grep -q "alias civops=" "$HOME/.bashrc" 2>/dev/null; then
    echo "$ALIAS_LINE" >> "$HOME/.bashrc"
    success "Alias 'civops' added to ~/.bashrc"
  else
    # Update existing alias in case path changed
    sed -i "s|alias civops=.*|$ALIAS_LINE|" "$HOME/.bashrc"
    success "Alias 'civops' updated in ~/.bashrc"
  fi
}

main() {
  banner
  check_termux
  install_packages
  install_python_deps
  setup_dirs
  request_permissions
  verify_api
  create_launcher
  create_alias

  echo ""
  echo -e "${GREEN}${BOLD}═══════════════════════════════════════${RESET}"
  echo -e "${GREEN}${BOLD}  CIVOPS installed successfully.${RESET}"
  echo -e "${GREEN}${BOLD}═══════════════════════════════════════${RESET}"
  echo ""
  echo "  To start:"
  echo "    source ~/.bashrc && civops"
  echo ""
  echo "  Or directly:"
  echo "    bash $CIVOPS_DIR/civops.sh"
  echo ""
  echo "  Custom port:"
  echo "    CIVOPS_PORT=9000 bash $CIVOPS_DIR/civops.sh"
  echo ""
  echo -e "${YELLOW}  IMPORTANT:${NC} If scans show no data, go to:"
  echo "  Android Settings → Apps → Termux:API → Permissions"
  echo "  → Location → Allow all the time"
  echo ""
}

main
