#!/usr/bin/env bash
# install_cloak.sh — provision the full Cloak stack for the Hermes Cloak Edition.
#
# Brings up EVERYTHING needed for Cloak in one shot:
#   1. CloakBrowser-Manager (Docker) on 127.0.0.1:8080
#   2. /etc/cloak/manager.env  (generated CLOAK_AUTH_TOKEN + CLOAK_MANAGER_URL +
#      CLOAK_CDP_PROXY_BASE + empty captcha keys). Auto-merged by the bundled
#      Cloak provider, so Hermes picks up the URL + token with no manual entry.
#   3. nginx CDP auth-proxy on 127.0.0.1:8081 (injects the Bearer header on the
#      WebSocket upgrade that native agent-browser can't set).
#   4. Python client stack into the Hermes venv (cloakbrowser, playwright, httpx,
#      pydoll) without downloading a local Chromium binary. Cloak connects to
#      the Manager-owned browser over CDP.
#
# Idempotent. In --strict mode, the installer exits nonzero when the Manager,
# protected bridge, core dependencies, browser driver, or provider configuration
# is not ready.
#
# Usage:
#   sudo bash scripts/install_cloak.sh                 # full bring-up
#   HERMES_INSTALL_DIR=/usr/local/lib/hermes-agent sudo bash scripts/install_cloak.sh
#   bash scripts/install_cloak.sh --no-manager         # python stack only
#   sudo bash scripts/install_cloak.sh --regenerate-token
#   sudo bash scripts/install_cloak.sh --token-env=AUTH_TOKEN
set -u

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${CYAN}[cloak]${NC} $*"; }
ok()   { echo -e "${GREEN}[cloak]${NC} $*"; }
warn() { echo -e "${YELLOW}[cloak]${NC} $*"; }
err()  { echo -e "${RED}[cloak]${NC} $*" >&2; }

# ---- options ----
WITH_MANAGER=1
WITH_NGINX=1
KEEP_TOKEN=1
CONFIGURE_PROVIDER=0
STRICT=0
DOCKER_TOKEN_ENV="${CLOAK_DOCKER_TOKEN_ENV:-}"
DOCKER_IMAGE="${CLOAK_DOCKER_IMAGE:-cloakhq/cloakbrowser-manager:latest}"
DOCKER_NAME="${CLOAK_DOCKER_NAME:-cloakbrowser-manager}"
DOCKER_VOLUME="${CLOAK_DOCKER_VOLUME:-cloak-profiles}"
MANAGER_PORT="${CLOAK_MANAGER_PORT:-8080}"
ENV_FILE="${CLOAK_ENV_FILE:-/etc/cloak/manager.env}"
ETC_DIR="/etc/cloak"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-manager) WITH_MANAGER=0; shift ;;
    --no-nginx) WITH_NGINX=0; shift ;;
    --regenerate-token) KEEP_TOKEN=0; shift ;;
    --configure-provider) CONFIGURE_PROVIDER=1; shift ;;
    --strict) STRICT=1; shift ;;
    --token-env=*) DOCKER_TOKEN_ENV="${1#*=}"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) warn "unknown option: $1"; shift ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HERMES_INSTALL_DIR:-}"
[[ -z "$INSTALL_DIR" ]] && INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HERMES_DATA_DIR="${HERMES_HOME:-${HOME:-/root}/.hermes}"
export PATH="$HERMES_DATA_DIR/bin:$HERMES_DATA_DIR/node/bin:$PATH"

is_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]]; }

# ---- locate venv python ----
find_py() {
  for c in "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/.venv/bin/python"; do
    [[ -x "$c" ]] && { echo "$c"; return 0; }
  done
  command -v python3 || command -v python || true
}
PY="$(find_py)"
[[ -n "$PY" ]] || { err "No Python interpreter found."; exit 1; }
log "Using Python: $PY"
log "Install dir:  $INSTALL_DIR"
CDP_BRIDGE_PORT="${CLOAK_CDP_BRIDGE_PORT:-8081}"
CDP_BRIDGE_URL="http://127.0.0.1:${CDP_BRIDGE_PORT}"
CDP_BRIDGE_SCRIPT="$SCRIPT_DIR/cloak/cdp_bridge.py"
CDP_BRIDGE_READINESS_SCRIPT="$SCRIPT_DIR/cloak/bridge_readiness.py"
HTTP_PROBE="$SCRIPT_DIR/cloak/http_probe.py"
CDP_BRIDGE_PID_FILE="$ETC_DIR/cdp_bridge.pid"
CDP_BRIDGE_LOG_FILE="$ETC_DIR/cdp_bridge.log"

clear_cdp_proxy_base() {
  if grep -q '^CLOAK_CDP_PROXY_BASE=' "$ENV_FILE"; then
    sed -i '/^CLOAK_CDP_PROXY_BASE=/d' "$ENV_FILE"
  fi
  unset CLOAK_CDP_PROXY_BASE
}

publish_cdp_proxy_base() {
  if grep -q '^CLOAK_CDP_PROXY_BASE=' "$ENV_FILE"; then
    sed -i "s|^CLOAK_CDP_PROXY_BASE=.*|CLOAK_CDP_PROXY_BASE=${CDP_BRIDGE_URL}|" "$ENV_FILE"
  else
    echo "CLOAK_CDP_PROXY_BASE=${CDP_BRIDGE_URL}" >> "$ENV_FILE"
  fi
  export CLOAK_CDP_PROXY_BASE="$CDP_BRIDGE_URL"
}

manager_host_from_url() {
  "$PY" -c 'from urllib.parse import urlparse; import sys; print((urlparse(sys.argv[1]).hostname or "").lower())' "$1"
}

ensure_manager_host_allowed() {
  local manager_url="$1" host current item found=0 merged
  host="$(manager_host_from_url "$manager_url" 2>/dev/null || true)"
  [[ -n "$host" ]] || { warn "Could not derive Manager hostname for allowlist"; return 1; }
  current="${CLOAK_ALLOWED_HOSTS:-}"
  while IFS= read -r item; do
    [[ "${item,,}" == "${host,,}" ]] && { found=1; break; }
  done < <(tr ',' '\n' <<< "$current")
  if [[ $found -eq 0 ]]; then
    merged="${current:+${current},}${host}"
    if grep -q '^CLOAK_ALLOWED_HOSTS=' "$ENV_FILE"; then
      sed -i "s|^CLOAK_ALLOWED_HOSTS=.*|CLOAK_ALLOWED_HOSTS=${merged}|" "$ENV_FILE"
    else
      echo "CLOAK_ALLOWED_HOSTS=${merged}" >> "$ENV_FILE"
    fi
    current="$merged"
  fi
  export CLOAK_ALLOWED_HOSTS="$current"
}

stop_external_cdp_bridge() {
  [[ -f "$CDP_BRIDGE_PID_FILE" ]] || return 0
  local old_pid command
  old_pid="$(head -n 1 "$CDP_BRIDGE_PID_FILE" 2>/dev/null || true)"
  if [[ ! "$old_pid" =~ ^[0-9]+$ ]]; then
    warn "Ignoring non-numeric CDP bridge PID file: $CDP_BRIDGE_PID_FILE"
    return 0
  fi
  if ! kill -0 "$old_pid" 2>/dev/null; then
    rm -f "$CDP_BRIDGE_PID_FILE"
    return 0
  fi
  command="$(ps -p "$old_pid" -o args= 2>/dev/null || true)"
  if [[ "$command" != *"$CDP_BRIDGE_SCRIPT"* || "$command" != *"--listen $CDP_BRIDGE_URL"* ]]; then
    warn "PID $old_pid is not this install's CDP bridge; leaving it untouched"
    return 0
  fi
  kill "$old_pid" 2>/dev/null || true
  for _ in $(seq 1 10); do
    kill -0 "$old_pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$old_pid" 2>/dev/null; then
    warn "Owned CDP bridge pid=$old_pid did not stop cleanly"
    return 1
  fi
  rm -f "$CDP_BRIDGE_PID_FILE"
}

disable_local_nginx_cdp_proxy() {
  local site="/etc/nginx/sites-enabled/cloak-cdp-proxy"
  if [[ -e "$site" || -L "$site" ]]; then
    rm -f "$site"
    if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1; then
      systemctl reload nginx >/dev/null 2>&1 || true
    fi
  fi
}

start_external_cdp_bridge() {
  local manager_url="$1" token="$2" code pid bridge_http_ready=0
  if [[ ! -f "$CDP_BRIDGE_SCRIPT" || ! -f "$CDP_BRIDGE_READINESS_SCRIPT" ]]; then
    warn "CDP bridge readiness scripts are missing"
    clear_cdp_proxy_base
    return 1
  fi
  stop_external_cdp_bridge || { clear_cdp_proxy_base; return 1; }
  CLOAK_AUTH_TOKEN="$token" CLOAK_MANAGER_URL="$manager_url" \
    "$PY" "$CDP_BRIDGE_SCRIPT" --listen "$CDP_BRIDGE_URL" --upstream "$manager_url" \
    >> "$CDP_BRIDGE_LOG_FILE" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$CDP_BRIDGE_PID_FILE"
  chmod 0600 "$CDP_BRIDGE_PID_FILE"
  for _ in $(seq 1 8); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$CDP_BRIDGE_URL/api/profiles" 2>/dev/null || echo 000)"
    if [[ "$code" == "200" ]]; then
      bridge_http_ready=1
      break
    fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if [[ $bridge_http_ready -eq 1 ]] && \
    CLOAK_AUTH_TOKEN="$token" "$PY" "$CDP_BRIDGE_READINESS_SCRIPT" \
      --manager-url "$manager_url" --bridge-url "$CDP_BRIDGE_URL" --timeout 5; then
    publish_cdp_proxy_base
    ok "External Manager CDP bridge passed HTTP and WebSocket readiness"
    return 0
  fi
  warn "External Manager CDP bridge failed protected readiness; see $CDP_BRIDGE_LOG_FILE"
  stop_external_cdp_bridge || true
  clear_cdp_proxy_base
  return 1
}

# ============================================================================
# 1. Self-hosted CloakBrowser-Manager (Docker) + env file + nginx proxy
# ============================================================================
provision_manager() {
  if [[ $WITH_MANAGER -eq 0 ]]; then
    log "Skipping CloakBrowser-Manager bring-up (--no-manager)"
    return 0
  fi
  if ! is_root; then
    warn "Not root — skipping Docker manager + nginx. Re-run with sudo to bring them up:"
    warn "  sudo bash $SCRIPT_DIR/install_cloak.sh"
    return 1
  fi

  # --- packages ---
  local needs_packages=0
  if ! command -v docker >/dev/null 2>&1; then needs_packages=1; fi
  if [[ $WITH_NGINX -eq 1 ]] && ! command -v nginx >/dev/null 2>&1; then needs_packages=1; fi
  if ! command -v curl >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then needs_packages=1; fi
  if [[ $needs_packages -eq 1 ]] && ! command -v apt-get >/dev/null 2>&1; then
    err "Automatic package provisioning needs Debian/Ubuntu with apt-get when Docker, nginx, curl, or openssl are missing."
    err "Install those dependencies first, or run this installer on a Debian/Ubuntu server."
    return 1
  fi

  log "Ensuring docker / nginx / openssl / curl..."
  if ! command -v docker >/dev/null 2>&1; then
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io || \
      warn "docker install failed — install Docker manually"
  fi
  if [[ $WITH_NGINX -eq 1 ]] && ! command -v nginx >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx-light || warn "nginx install failed"
  fi
  for pkg in curl openssl; do
    command -v "$pkg" >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg" || true
  done
  systemctl enable --now docker 2>/dev/null || warn "could not enable docker service"

  # --- env file + token ---
  # NOTE: do NOT publish CLOAK_CDP_PROXY_BASE here — only after nginx probe succeeds.
  install -d -m 0750 "$ETC_DIR"
  if [[ -f "$ENV_FILE" && $KEEP_TOKEN -eq 1 ]]; then
    ok "Keeping existing $ENV_FILE (use --regenerate-token to replace)"
  else
    local tok
    tok="$(openssl rand -hex 32)"
    install -m 0600 /dev/null "$ENV_FILE"
    cat > "$ENV_FILE" <<EOF
CLOAK_MANAGER_URL=http://127.0.0.1:${MANAGER_PORT}
CLOAK_AUTH_TOKEN=${tok}
CAPSOLVER_API_KEY=
TWOCAPTCHA_API_KEY=
TWO_CAPTCHA_API_KEY=
NOTLETTERS_API_KEY=
# Proxy pool: managed from the Hermes dashboard /cloak panel (saved to
# /etc/cloak/proxies.json). Set to 1 to auto-assign a pool proxy to every new
# Cloak profile (so "use a proxy from the pool" works with no skill).
CLOAK_USE_PROXY_POOL=0
EOF
    echo "$tok" > "$ETC_DIR/auth_token"; chmod 600 "$ETC_DIR/auth_token"
    ok "Created $ENV_FILE (token generated)"
  fi

  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
  local token="${CLOAK_AUTH_TOKEN:-}"
  [[ -n "$token" ]] || { err "CLOAK_AUTH_TOKEN missing in $ENV_FILE"; return 1; }
  local manager_url="${CLOAK_MANAGER_URL:-http://127.0.0.1:${MANAGER_PORT}}"
  manager_url="${manager_url%/}"
  if [[ ! "$manager_url" =~ ^https?:// ]]; then
    err "CLOAK_MANAGER_URL must use http:// or https://"
    return 1
  fi
  if ! "$PY" -c 'from urllib.parse import urlsplit; import sys; u = urlsplit(sys.argv[1]); sys.exit(0 if not (u.username or u.password or u.query or u.fragment) else 1)' "$manager_url"; then
    err "CLOAK_MANAGER_URL must not contain credentials, a query, or a fragment"
    return 1
  fi

  local local_manager_url="http://127.0.0.1:${MANAGER_PORT}"
  local local_manager_alias="http://localhost:${MANAGER_PORT}"
  if [[ "$manager_url" != "$local_manager_url" && "$manager_url" != "$local_manager_alias" ]]; then
    log "Using external CloakBrowser-Manager at $manager_url"
    ensure_manager_host_allowed "$manager_url" || { clear_cdp_proxy_base; return 1; }
    stop_external_cdp_bridge || { clear_cdp_proxy_base; return 1; }
    # Do not leave the local nginx site bound to the bridge port when switching
    # to an external Manager. The Python bridge below injects the remote token.
    disable_local_nginx_cdp_proxy
    if CLOAK_AUTH_TOKEN="$token" "$PY" "$HTTP_PROBE" \
      --url "$manager_url/api/profiles" --timeout 5 --bearer-env CLOAK_AUTH_TOKEN; then
      ok "External CloakBrowser-Manager passed protected readiness"
      if ! start_external_cdp_bridge "$manager_url" "$token"; then
        clear_cdp_proxy_base
        err "External Manager CDP bridge failed readiness; refusing a successful provision result"
        return 1
      fi
    else
      err "External Manager failed protected readiness"
      clear_cdp_proxy_base
      return 1
    fi
    return 0
  fi

  # --- docker token-env detection ---
  if [[ -z "$DOCKER_TOKEN_ENV" ]]; then
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$DOCKER_NAME"; then
      DOCKER_TOKEN_ENV="$(docker inspect "$DOCKER_NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | grep -oE '^(AUTH_TOKEN|CLOAK_AUTH_TOKEN|MANAGER_TOKEN|API_TOKEN)=' | head -1 | tr -d '=')"
    fi
    [[ -z "$DOCKER_TOKEN_ENV" ]] && DOCKER_TOKEN_ENV="AUTH_TOKEN"
  fi
  log "Docker token env = $DOCKER_TOKEN_ENV"

  # --- run / restart container ---
  # On --regenerate-token the Manager must be recreated so it picks up the new AUTH_TOKEN.
  if [[ $KEEP_TOKEN -eq 0 ]] && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$DOCKER_NAME"; then
    log "Token regenerated — recreating container $DOCKER_NAME with new AUTH_TOKEN..."
    docker rm -f "$DOCKER_NAME" >/dev/null 2>&1 || true
  fi
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$DOCKER_NAME"; then
    ok "Container $DOCKER_NAME already running"
  elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$DOCKER_NAME"; then
    docker start "$DOCKER_NAME" >/dev/null && ok "Started existing container $DOCKER_NAME"
  else
    log "Pulling $DOCKER_IMAGE..."
    docker pull "$DOCKER_IMAGE" 2>&1 | tail -2 || warn "pull failed (will try run anyway)"
    log "Starting $DOCKER_NAME on 127.0.0.1:${MANAGER_PORT}..."
    docker run -d --name "$DOCKER_NAME" --restart unless-stopped \
      -p "127.0.0.1:${MANAGER_PORT}:8080" \
      -v "${DOCKER_VOLUME}:/data" \
      -e "${DOCKER_TOKEN_ENV}=${token}" \
      "$DOCKER_IMAGE" >/dev/null && ok "Container started" || warn "docker run failed"
  fi

  # --- protected readiness ---
  MANAGER_READY=0
  for _ in $(seq 1 12); do
    if CLOAK_AUTH_TOKEN="$token" "$PY" "$HTTP_PROBE" \
      --url "$manager_url/api/profiles" --timeout 5 --bearer-env CLOAK_AUTH_TOKEN; then
      MANAGER_READY=1
      ok "CloakBrowser-Manager up on 127.0.0.1:${MANAGER_PORT}"
      break
    fi
    sleep 1
  done
  if [[ $MANAGER_READY -ne 1 ]]; then
    clear_cdp_proxy_base
    err "CloakBrowser-Manager did not pass protected readiness; refusing proxy publication"
    return 1
  fi

  # A prior external bridge uses the same localhost port as nginx. Stop only
  # the bridge recorded and verified as owned by this installer.
  stop_external_cdp_bridge || true

  # --- nginx CDP auth-proxy ---
  if [[ $WITH_NGINX -eq 1 ]] && command -v nginx >/dev/null 2>&1; then
    log "Configuring nginx CDP auth-proxy on 127.0.0.1:8081..."
    install -m 0644 "$SCRIPT_DIR/cloak/nginx/cloak-upgrade-map.conf" /etc/nginx/conf.d/cloak-upgrade-map.conf
    # Token lives in a root-only include so the site config can stay 0644 without leaking secrets.
    umask 077
    install -d -m 0750 /etc/nginx/cloak
    printf 'proxy_set_header Authorization "Bearer %s";\n' "$token" \
      > /etc/nginx/cloak/auth-header.conf
    chmod 0600 /etc/nginx/cloak/auth-header.conf
    umask 022
    # Template: inject auth include + manager upstream port.
    sed \
      -e 's|proxy_set_header Authorization "Bearer __CLOAK_AUTH_TOKEN__";|include /etc/nginx/cloak/auth-header.conf;|' \
      -e "s|__CLOAK_MANAGER_UPSTREAM__|127.0.0.1:${MANAGER_PORT}|g" \
      "$SCRIPT_DIR/cloak/nginx/cloak-cdp-proxy.conf.template" \
      > /etc/nginx/sites-available/cloak-cdp-proxy
    chmod 0644 /etc/nginx/sites-available/cloak-cdp-proxy
    ln -sf /etc/nginx/sites-available/cloak-cdp-proxy /etc/nginx/sites-enabled/cloak-cdp-proxy
    PROXY_READY=0
    if nginx -t >/dev/null 2>&1; then
      systemctl enable --now nginx >/dev/null 2>&1 || true
      systemctl reload nginx >/dev/null 2>&1 || systemctl restart nginx >/dev/null 2>&1 || true
      # Auth-proxy readiness: protected GET /api/profiles through nginx must return 200
      # (Bearer injected). Do NOT WS-probe ws://127.0.0.1:8081/ — Manager only
      # accepts CDP WebSockets at /api/profiles/{id}/cdp (needs a running profile).
      for _ in $(seq 1 8); do
        code="$(curl -s -o /dev/null -w '%{http_code}' \
          "http://127.0.0.1:8081/api/profiles" 2>/dev/null || echo 000)"
        if [[ "$code" == "200" ]]; then
          PROXY_READY=1
          break
        fi
        sleep 1
      done
    fi
    if [[ $PROXY_READY -eq 1 ]] && \
      CLOAK_AUTH_TOKEN="$token" "$PY" "$CDP_BRIDGE_READINESS_SCRIPT" \
        --manager-url "$manager_url" --bridge-url "http://127.0.0.1:8081" \
        --timeout 5; then
      if ! grep -q '^CLOAK_CDP_PROXY_BASE=' "$ENV_FILE"; then
        echo "CLOAK_CDP_PROXY_BASE=http://127.0.0.1:8081" >> "$ENV_FILE"
      else
        sed -i 's|^CLOAK_CDP_PROXY_BASE=.*|CLOAK_CDP_PROXY_BASE=http://127.0.0.1:8081|' "$ENV_FILE"
      fi
      ok "nginx CDP proxy ready on 127.0.0.1:8081 (CLOAK_CDP_PROXY_BASE published)"
    else
      # Do not leave a broken proxy URL in env — provider would rewrite WS to a dead endpoint.
      if grep -q '^CLOAK_CDP_PROXY_BASE=' "$ENV_FILE"; then
        sed -i '/^CLOAK_CDP_PROXY_BASE=/d' "$ENV_FILE"
      fi
      err "nginx CDP proxy failed protected readiness; CLOAK_CDP_PROXY_BASE cleared"
      return 1
    fi
  else
    clear_cdp_proxy_base
    if [[ $WITH_NGINX -eq 1 ]]; then
      err "nginx CDP proxy is unavailable; CLOAK_CDP_PROXY_BASE cleared"
      return 1
    fi
    warn "nginx CDP proxy disabled by --no-nginx - CLOAK_CDP_PROXY_BASE cleared"
  fi
}

# ============================================================================
# 2. Python client stack (cloakbrowser / playwright / httpx / pydoll)
# ============================================================================

# Pick a working installer backend for the Hermes venv. Hermes venvs are often
# created by `uv` and ship WITHOUT pip, so `python -m pip` fails with
# "No module named pip". Resolve once into PY_INSTALL_KIND:
#   uv       -> uv pip install --python "$PY"
#   pip      -> "$PY" -m pip install
#   (empty)  -> no usable backend
PY_INSTALL_KIND=""
PY_INSTALL_UV=""
resolve_py_installer() {
  if "$PY" -m pip --version >/dev/null 2>&1; then
    PY_INSTALL_KIND="pip"; return 0
  fi
  # Try to bootstrap pip into the venv (works on most CPython builds).
  if "$PY" -m ensurepip --upgrade >/dev/null 2>&1 && "$PY" -m pip --version >/dev/null 2>&1; then
    PY_INSTALL_KIND="pip"; ok "Bootstrapped pip via ensurepip"; return 0
  fi
  # uv can install straight into an existing interpreter without pip present.
  local managed_uv="$HERMES_DATA_DIR/bin/uv"
  if [[ -x "$managed_uv" ]]; then
    PY_INSTALL_KIND="uv"; PY_INSTALL_UV="$managed_uv"; ok "Using managed uv to install into the Hermes venv"; return 0
  fi
  if command -v uv >/dev/null 2>&1; then
    PY_INSTALL_KIND="uv"; PY_INSTALL_UV="$(command -v uv)"; ok "Using uv to install into the Hermes venv"; return 0
  fi
  return 1
}

py_install() {  # py_install <pkg> [pkg...]
  case "$PY_INSTALL_KIND" in
    pip) "$PY" -m pip install --upgrade --quiet "$@" ;;
    uv)  "$PY_INSTALL_UV" pip install --python "$PY" --quiet "$@" ;;
    *)   return 1 ;;
  esac
}

provision_python_stack() {
  if ! resolve_py_installer; then
    warn "No usable Python installer (no pip, ensurepip failed, no uv)."
    warn "Provider still works via 'requests'; to enable rich tools install uv or pip, then re-run."
    [[ $STRICT -eq 1 ]] && return 1
    return 0
  fi

  log "Installing core Cloak deps (cloakbrowser, playwright, httpx) via $PY_INSTALL_KIND..."
  if py_install "cloakbrowser>=0.3" "playwright>=1.53" "httpx>=0.27"; then
    ok "Core Cloak deps installed."
  else
    warn "Core Cloak deps install failed; provider still works with reduced features."
    [[ $STRICT -eq 1 ]] && return 1
  fi

  log "Installing optional hybrid deps (pydoll-python)..."
  py_install "pydoll-python>=2.20" >/dev/null 2>&1 && ok "Hybrid deps installed." || warn "Hybrid deps skipped (optional)."

  ok "Local Playwright Chromium skipped; Cloak attaches to the Manager-owned browser over CDP."
}
# ============================================================================
# 3. Node browser driver (agent-browser) — required for browser_* tools
# ============================================================================
agent_browser_healthy() {
  local binary="$1"
  [[ -x "$binary" ]] || return 1
  "$binary" --help >/dev/null 2>&1
}

provision_node_browser() {
  local ab="$INSTALL_DIR/node_modules/.bin/agent-browser"
  if agent_browser_healthy "$ab"; then
    ok "agent-browser already installed and runnable."
    return 0
  fi
  if ! command -v npm >/dev/null 2>&1; then
    warn "npm not found; browser_navigate will fail until you run: cd $INSTALL_DIR && npm install"
    [[ $STRICT -eq 1 ]] && return 1
    return 0
  fi
  log "Installing Node browser deps (agent-browser) for Cloak browser_* tools..."
  if (cd "$INSTALL_DIR" && npm install --silent --no-fund --no-audit); then
    if agent_browser_healthy "$ab"; then
      ok "agent-browser installed and runnable."
    else
      warn "npm install finished but agent-browser is not runnable; run: cd $INSTALL_DIR && npm install"
      [[ $STRICT -eq 1 ]] && return 1
    fi
  else
    warn "npm install failed; browser tools will not work until: cd $INSTALL_DIR && npm install"
    [[ $STRICT -eq 1 ]] && return 1
  fi
}

configure_cloak_provider() {
  local hermes_bin="" candidate
  for candidate in "$INSTALL_DIR/venv/bin/hermes" "$INSTALL_DIR/.venv/bin/hermes"; do
    if [[ -x "$candidate" ]]; then
      hermes_bin="$candidate"
      break
    fi
  done
  if [[ -z "$hermes_bin" ]]; then
    err "Hermes CLI was not found in the installed virtual environment."
    return 1
  fi
  if ! HERMES_HOME="$HERMES_DATA_DIR" "$hermes_bin" config set browser.cloud_provider cloak; then
    err "Hermes rejected browser.cloud_provider=cloak."
    return 1
  fi
  ok "Configured browser.cloud_provider=cloak."
}
# ============================================================================
# Run
# ============================================================================
CLOAK_MANAGER_PROVISION_OK=1
if ! provision_manager; then
  CLOAK_MANAGER_PROVISION_OK=0
fi

CLOAK_PYTHON_PROVISION_OK=1
if ! provision_python_stack; then
  CLOAK_PYTHON_PROVISION_OK=0
fi

CLOAK_NODE_PROVISION_OK=1
if ! provision_node_browser; then
  CLOAK_NODE_PROVISION_OK=0
fi

if [[ $CLOAK_MANAGER_PROVISION_OK -ne 1 || $CLOAK_PYTHON_PROVISION_OK -ne 1 || $CLOAK_NODE_PROVISION_OK -ne 1 ]]; then
  err "Cloak provisioning did not complete; no ready release state was published."
  exit 1
fi

if [[ $CONFIGURE_PROVIDER -eq 1 ]]; then
  configure_cloak_provider || { err "Could not configure browser.cloud_provider=cloak."; exit 1; }
fi
# ---- summary ----
# Keep the token out of the installer summary process state.
# Protected probes above read the token only through their environment.
PUBLIC_IP="$(curl -s --max-time 3 https://api.ipify.org 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "$PUBLIC_IP" ]] && PUBLIC_IP="YOUR_VPS"

cat <<EOF

────────────────────────────────────────────────────────────────────────
 Cloak Edition — ready
────────────────────────────────────────────────────────────────────────
 Manager:   http://127.0.0.1:${MANAGER_PORT}   (env: ${ENV_FILE})
 CDP proxy: http://127.0.0.1:8081

 Open the Cloak Manager UI from your laptop via an SSH tunnel:
   ssh -L ${MANAGER_PORT}:127.0.0.1:${MANAGER_PORT} root@${PUBLIC_IP}
   then browse http://localhost:${MANAGER_PORT}

 Bearer token: stored in ${ENV_FILE} (mode 0600) and /etc/nginx/cloak/auth-header.conf
   reprint (root only): sudo cat ${ETC_DIR}/auth_token
   (token is NOT printed here)

 In Hermes:
   - CLOAK_MANAGER_URL + token are read automatically from ${ENV_FILE}.
   - Run \`hermes tools\` -> Browser Automation -> "Cloak (CloakBrowser stealth)".
   - Dashboard panel: http://<dashboard-host>:<port>/cloak
   - Captcha keys: edit ${ENV_FILE} (CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY),
     then: docker restart ${DOCKER_NAME}
────────────────────────────────────────────────────────────────────────
EOF
ok "Cloak provisioning complete."
