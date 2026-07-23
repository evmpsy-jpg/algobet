# Deploy to VPS

This project is deployed as a long-running Telegram bot process managed by `systemd`.

The recommended first VPS setup is:

- Ubuntu 22.04/24.04 LTS.
- Python 3.11+.
- SQLite database in `/opt/algobet/data/algobet.db`.
- Project files in `/opt/algobet`.
- Service user `algobet`.

## 1. Create User And Packages

```bash
sudo adduser --system --group --home /opt/algobet algobet
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

## 2. Upload Code

Use either `git clone` or copy the project archive.

```bash
sudo git clone https://github.com/evmpsy-jpg/algobet.git /opt/algobet
sudo chown -R algobet:algobet /opt/algobet
cd /opt/algobet
```

If deploying a feature branch:

```bash
sudo -u algobet git checkout feature/stage-5-1-stabilization
```

## 3. Create Virtual Environment

```bash
cd /opt/algobet
sudo -u algobet python3 -m venv .venv
sudo -u algobet .venv/bin/pip install --upgrade pip
sudo -u algobet .venv/bin/pip install -r requirements.txt
```

## 4. Configure Environment

Create `/opt/algobet/.env`:

```bash
sudo -u algobet nano /opt/algobet/.env
```

Example:

```env
BOT_TOKEN=PASTE_TOKEN_FROM_BOTFATHER
ADMIN_IDS=315715137
DATABASE_URL=sqlite+aiosqlite:///./data/algobet.db
TIMEZONE=Europe/Moscow
SIGNAL_LEAD_MINUTES=20
SCHEDULER_INTERVAL_SECONDS=30
MAX_UPLOAD_MB=25
ANALYSIS_PAYMENT_DETAILS=Реквизиты для оплаты анализа уточните у специалиста.
ANALYSIS_SPECIALIST_CONTACT=@your_specialist
```

Subscription payment details and contacts can be changed later in the bot admin settings.

## 5. Initialize Database

```bash
cd /opt/algobet
sudo -u algobet .venv/bin/python -c "import asyncio; from app.database.session import init_db; asyncio.run(init_db()); print('db ok')"
```

## 6. Install Systemd Service

```bash
sudo cp /opt/algobet/deploy/algobet-bot.service /etc/systemd/system/algobet-bot.service
sudo systemctl daemon-reload
sudo systemctl enable algobet-bot
sudo systemctl start algobet-bot
```

Check status and logs:

```bash
sudo systemctl status algobet-bot
sudo journalctl -u algobet-bot -f
```

## 7. Update Deployment

```bash
cd /opt/algobet
sudo -u algobet git pull
sudo -u algobet .venv/bin/pip install -r requirements.txt
sudo -u algobet .venv/bin/python -c "import asyncio; from app.database.session import init_db; asyncio.run(init_db()); print('db ok')"
sudo systemctl restart algobet-bot
sudo journalctl -u algobet-bot -n 100 --no-pager
```

## 8. Backup SQLite Database

```bash
sudo mkdir -p /opt/algobet/backups
sudo cp /opt/algobet/data/algobet.db /opt/algobet/backups/algobet-$(date +%F-%H%M).db
sudo chown -R algobet:algobet /opt/algobet/backups
```

## Web Admin Later

The web admin can be added as a separate FastAPI app behind nginx:

- bot stays as `algobet-bot.service`;
- web admin runs as a second service, for example `algobet-web.service`;
- both use the same database;
- nginx terminates HTTPS and proxies to localhost.

Do not expose the SQLite file or `.env` through nginx.
