#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"
if [ ! -f "$REPO/.env" ]; then
  echo ".env missing in $REPO; copy .env.example and fill it in first" >&2
  exit 1
fi
for unit in polymarket-runner polymarket-bot polymarket-dashboard; do
  sed -e "s#__REPO__#$REPO#g" -e "s#__USER__#$USER_NAME#g" "$REPO/deploy/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\nIdleAction=ignore\n' | sudo tee /etc/systemd/logind.conf.d/polymarket.conf >/dev/null
sudo systemctl restart systemd-logind || true
sudo timedatectl set-ntp true || true
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-runner polymarket-bot polymarket-dashboard
systemctl --no-pager --lines=5 status polymarket-runner polymarket-bot polymarket-dashboard || true
echo "logs: journalctl -u polymarket-runner -f"
