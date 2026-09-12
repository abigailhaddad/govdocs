#!/usr/bin/env bash
# One-time setup for an Ubuntu 24.04 box that runs long crawls for this repo.
#
# Generic on purpose: a worker user, swap, a place for checkouts. The
# repo-specific half is install-collect.sh.
#
#   ssh root@YOUR_SERVER_IP 'bash -s' < deploy/hetzner-bootstrap.sh
#
# Idempotent. Safe to rerun.

set -euo pipefail

WORKER=worker
REPOS=/srv/repos

echo "==> packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-pip git tmux curl ca-certificates \
  xvfb ufw unattended-upgrades >/dev/null

echo "==> firewall (ssh only; nothing here listens)"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

echo "==> unattended security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

echo "==> swap"
# Hetzner ships these with none, and without swap the kernel's only way to
# satisfy a memory-hungry process under a cgroup ceiling is to evict page
# cache -- so a process re-reads the files it is itself reading, forever.
# Chromium is the memory-hungry process here.
if ! swapon --show --noheadings | grep -q .; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap -q /swapfile
  swapon /swapfile
  grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi
swapon --show

echo "==> worker user and $REPOS"
id -u "$WORKER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$WORKER"
mkdir -p "$REPOS"
chown -R "$WORKER:$WORKER" "$REPOS"

if [ -f /root/.ssh/authorized_keys ]; then
  install -d -m 700 -o "$WORKER" -g "$WORKER" "/home/$WORKER/.ssh"
  install -m 600 -o "$WORKER" -g "$WORKER" \
    /root/.ssh/authorized_keys "/home/$WORKER/.ssh/authorized_keys"
fi

echo
echo "Done. Next:"
echo "  1. printf 'HF_TOKEN=hf_xxx\\nDATAGOV_API_KEY=xxx\\n' > /etc/govdocs.env"
echo "     chmod 600 /etc/govdocs.env"
echo "  2. bash -s < deploy/install-collect.sh"
