#!/usr/bin/env bash
# Manage the crawl box from the repo instead of the Hetzner console.
#
# The console is needed exactly once: make a project and an API token
# (Security > API tokens > Generate, read/write).
#
#   export HCLOUD_TOKEN=...            # or put it in deploy/.hcloud.env
#   ./deploy/server.sh create          # make the box and provision it
#   ./deploy/server.sh status          # what exists, and what it has collected
#   ./deploy/server.sh logs            # follow the crawl
#   ./deploy/server.sh ssh             # shell on it
#   ./deploy/server.sh update          # pull main and restart the crawl
#   ./deploy/server.sh destroy         # stop paying for it (prompts)
#   ./deploy/server.sh destroy --yes   # ... without the prompt
#
# Billed hourly. Destroy it when the rooms are exhausted.

set -euo pipefail

NAME="${WORKER_NAME:-govdocs-worker}"
# cax11: 2 vCPU / 4 GB / 40 GB, ARM. cx22 and cx33 no longer exist on this
# account -- the type line is versioned, check `hcloud server-type list` when
# create 404s. ARM because x86 here is 5x the price (cpx21 is EUR 0.060/hr
# against 0.011) and the crawl is politeness-limited anyway. The crawl is politeness-limited, not CPU-bound
# -- the point of a box is hours, not speed -- so the constraint is Chromium's
# memory rather than cores. Size up to cx33 if the unit starts hitting
# MemoryMax; `hcloud server-type list` if this 404s, the line is versioned.
TYPE="${WORKER_TYPE:-cpx21}"
IMAGE="${WORKER_IMAGE:-ubuntu-24.04}"
# Hetzner locations run out of stock; try the others if this refuses:
# fsn1 nbg1 hel1 (EU), ash hil (US), sin.
LOCATION="${WORKER_LOCATION:-ash}"
SSH_KEY_NAME="${WORKER_SSH_KEY:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# set -a so the token is exported: hcloud is a child process and a plain shell
# variable never reaches it.
if [ -f "$HERE/.hcloud.env" ]; then
  set -a; . "$HERE/.hcloud.env"; set +a
fi

need() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }

hc() {
  need hcloud
  : "${HCLOUD_TOKEN:?set HCLOUD_TOKEN, or put it in deploy/.hcloud.env}"
  hcloud "$@"
}

server_ip() { hc server ip "$NAME" 2>/dev/null; }

cmd_create() {
  if server_ip >/dev/null 2>&1; then
    echo "$NAME already exists at $(server_ip)"
  else
    if [ -z "$SSH_KEY_NAME" ]; then
      SSH_KEY_NAME=$(hc ssh-key list -o noheader -o columns=name | head -1)
      [ -n "$SSH_KEY_NAME" ] || {
        echo "No SSH key registered. Add one first:"
        echo "  hcloud ssh-key create --name laptop --public-key-from-file ~/.ssh/id_ed25519.pub"
        exit 1; }
      echo "using ssh key: $SSH_KEY_NAME"
    fi
    echo "==> creating $NAME ($TYPE, $IMAGE, $LOCATION)"
    hc server create --name "$NAME" --type "$TYPE" --image "$IMAGE" \
      --location "$LOCATION" --ssh-key "$SSH_KEY_NAME"
  fi
  cmd_provision
}

cmd_provision() {
  local ip; ip=$(server_ip)
  echo "==> waiting for ssh on $ip"
  for _ in $(seq 1 60); do
    ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
        "root@$ip" true 2>/dev/null && break
    sleep 5
  done
  echo "==> bootstrap"
  ssh "root@$ip" 'bash -s' < "$HERE/hetzner-bootstrap.sh"
  echo
  echo "Now put the tokens on the box, then install the crawl:"
  echo "  ssh root@$ip \"printf 'HF_TOKEN=%s\\nDATAGOV_API_KEY=%s\\n' HF DATAGOV > /etc/govdocs.env && chmod 600 /etc/govdocs.env\""
  echo "  ssh root@$ip 'bash -s' < $HERE/install-collect.sh"
}

cmd_status() {
  hc server list -o columns=name,status,ipv4,type,location,created
  echo
  echo "Type $TYPE is billed hourly; 'destroy' stops the meter."
  local ip; ip=$(server_ip 2>/dev/null) || return 0
  echo
  # Deliberately not `systemctl is-active`. Liveness is not progress: a unit
  # can sit in "activating" for hours while collecting nothing. Count what has
  # actually landed, and show the last thing the crawl said.
  ssh -o ConnectTimeout=10 "root@$ip" bash -s <<'REMOTE' 2>/dev/null || echo "(could not read status)"
cd /srv/repos/govdocs 2>/dev/null || exit 0
docs=$(grep -c '"path"' data/seen.jsonl 2>/dev/null || echo 0)
echo "recorded documents: $docs"
echo "disk: $(df -h / | awk 'NR==2{print $4" free"}')"
echo "memory pressure: $(cat /sys/fs/cgroup/system.slice/govdocs-collect.service/memory.pressure 2>/dev/null | head -1 || echo n/a)"
echo "last from the crawl:"
journalctl -u govdocs-collect -n 6 --no-pager -o cat 2>/dev/null | sed 's/^/  /'
REMOTE
}

cmd_logs()   { ssh "root@$(server_ip)" 'journalctl -u govdocs-collect -f -n 60'; }
cmd_ssh()    { ssh "root@$(server_ip)"; }
cmd_update() { ssh "root@$(server_ip)" 'bash -s' < "$HERE/install-collect.sh"; }

cmd_destroy() {
  local ip; ip=$(server_ip) || { echo "$NAME does not exist"; return 0; }
  if [ "${1:-}" = "--yes" ]; then
    hc server delete "$NAME"
    return
  fi
  # Without a terminal `read` gets EOF and takes the cancel branch, which is
  # the right default but looks exactly like a successful destroy while the
  # box carries on billing. Say so instead.
  [ -t 0 ] || {
    echo "destroy needs a terminal for the confirmation prompt."
    echo "Non-interactively, say so explicitly:  $0 destroy --yes"
    return 1; }
  read -r -p "Destroy $NAME ($ip)? Everything on it is lost. [y/N] " ok
  [ "$ok" = "y" ] || { echo "cancelled"; return 0; }
  hc server delete "$NAME"
}

case "${1:-}" in
  create)    cmd_create ;;
  provision) cmd_provision ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  ssh)       cmd_ssh ;;
  update)    cmd_update ;;
  destroy)   cmd_destroy "${2:-}" ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ; exit 1 ;;
esac
