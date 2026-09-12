#!/usr/bin/env bash
# Check out the repo, build its venv, install the systemd unit, start it.
#
# The per-repo half; hetzner-bootstrap.sh is the generic half.
#
#   ssh root@YOUR_SERVER_IP 'bash -s' < deploy/install-collect.sh
#
# Idempotent. Rerunning updates the checkout and restarts the crawl.

set -euo pipefail

WORKER=worker
REPOS=/srv/repos
NAME=govdocs
REPO_URL=https://github.com/abigailhaddad/govdocs
DIR="$REPOS/$NAME"

[ -s /etc/govdocs.env ] || {
  echo "Missing /etc/govdocs.env with HF_TOKEN=... and DATAGOV_API_KEY=..."
  echo "  printf 'HF_TOKEN=hf_xxx\nDATAGOV_API_KEY=xxx\n' > /etc/govdocs.env"
  echo "  chmod 600 /etc/govdocs.env"
  exit 1; }
chmod 600 /etc/govdocs.env

echo "==> stopping any running pass before touching the checkout"
# Bash reads a script incrementally by byte offset, so rewriting run-collect.sh
# under a running copy can drop it into the middle of a different line.
systemctl stop govdocs-collect.service 2>/dev/null || true

echo "==> checkout"
if [ -d "$DIR/.git" ]; then
  sudo -u "$WORKER" git -C "$DIR" fetch --quiet origin
  sudo -u "$WORKER" git -C "$DIR" reset --hard --quiet origin/main
else
  sudo -u "$WORKER" git clone --quiet "$REPO_URL" "$DIR"
fi
# A pass killed mid-flight leaves the lock behind; it is a directory, not a
# process, so nothing clears it on reboot.
rm -rf "$DIR/data/.collect.lock"

echo "==> venv"
sudo -u "$WORKER" python3 -m venv "$DIR/.venv"
sudo -u "$WORKER" "$DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$WORKER" "$DIR/.venv/bin/pip" install --quiet \
  boto3 requests huggingface_hub pymupdf pyarrow playwright python-dotenv

echo "==> chromium"
# Browser binaries as the worker (they land in its home); the shared libraries
# they need are apt packages and want root.
"$DIR/.venv/bin/playwright" install-deps chromium
sudo -u "$WORKER" "$DIR/.venv/bin/playwright" install chromium

echo "==> systemd unit"
cat > /etc/systemd/system/govdocs-collect.service <<UNIT
[Unit]
Description=govdocs FOIA reading-room crawl
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$WORKER
WorkingDirectory=$DIR
EnvironmentFile=/etc/govdocs.env
ExecStart=$DIR/deploy/run-collect.sh
# Chromium is what uses memory here, and it grows across a long pass. Let the
# cgroup push back and, at worst, kill this service rather than letting the
# kernel choose a victim. Re-measure before trusting these: the usajobs box
# taught that every number taken from a laptop was wrong on a shared vCPU.
MemoryHigh=2500M
MemoryMax=3G
MemorySwapMax=2G
# Every step is resumable. A document is recorded only after its file is
# pushed, so a kill costs a re-fetch rather than a permanently skipped
# document, and seen.jsonl on disk survives the restart.
Restart=always
RestartSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable govdocs-collect.service
systemctl start --no-block govdocs-collect.service

echo
echo "Started. Watch it with:"
echo "  journalctl -u govdocs-collect -f"
echo "or from your laptop:  ./deploy/server.sh logs"
