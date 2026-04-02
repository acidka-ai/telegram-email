#!/usr/bin/env bash
set -euo pipefail

sudo mkdir -p /opt/mail_tg_bot
sudo cp -f bot.py requirements.txt .env.example mail-tg-bot.service /opt/mail_tg_bot/
cd /opt/mail_tg_bot

sudo python3 -m venv .venv
sudo ./.venv/bin/pip install --upgrade pip
sudo ./.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  sudo cp .env.example .env
  echo "Fill /opt/mail_tg_bot/.env then run: sudo systemctl restart mail-tg-bot"
fi

sudo cp -f mail-tg-bot.service /etc/systemd/system/mail-tg-bot.service
sudo systemctl daemon-reload
sudo systemctl enable mail-tg-bot
sudo systemctl restart mail-tg-bot || true
sudo systemctl status --no-pager mail-tg-bot || true
