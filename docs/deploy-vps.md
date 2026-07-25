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

## Optional Docker Compose Run

The bot can also run through Docker Compose. Do not run the `systemd` Python service and Docker container at the same time, because Telegram polling must have only one active bot process.

Install Docker on Ubuntu:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
```

Stop the old service:

```bash
sudo systemctl stop algobet-bot
sudo systemctl disable algobet-bot
```

Start the bot with Docker Compose:

```bash
cd /opt/algobet
docker compose up -d --build
docker compose logs -f bot
```

Useful commands:

```bash
docker compose ps
docker compose logs -n 100 bot
docker compose restart bot
docker compose down
```

## Web Admin Dashboard

The Docker Compose setup includes a web admin service on port `8000`. It uses HTTP Basic Auth. Normal web admins are managed in the database from the `Админы` page; `WEB_ADMIN_USERS` in `.env` remains an emergency fallback login. The web admin can view data and perform operational actions: update request statuses, manage user access, edit payment/contact settings, fix signal results, manage web admins, view monitoring, and export CSV reports.

Add at least one emergency admin credential to `/opt/algobet/.env`:

```env
WEB_ADMIN_USERS=admin:PASTE_LONG_RANDOM_PASSWORD_HERE,manager:PASTE_SECOND_LONG_PASSWORD_HERE
```

Start both services:

```bash
cd /opt/algobet
docker compose up -d --build
```

The Compose file publishes the web dashboard on a non-standard external port from `WEB_ADMIN_PORT`. This is convenient but it is still plain HTTP, so use long unique passwords and do not reuse them elsewhere.

Add the external port to `/opt/algobet/.env`:

```env
WEB_ADMIN_PORT=48291
```

Open the web admin:

```text
http://217.114.5.208:48291/
```

Use a database web-admin login/password, or one of the emergency `WEB_ADMIN_USERS` pairs from `/opt/algobet/.env`, when the browser asks for credentials. Payment remains manual: users create requests, admins confirm payment and activate access in the admin panel.

Useful checks:

```bash
docker compose ps
docker compose logs -n 100 web
docker compose restart web
```

## External Web Admin With HTTPS

For stronger external access later, use a domain and expose the Python web service through nginx with HTTPS.

1. Point a domain or subdomain to the VPS IP, for example:

```text
admin.example.com -> 217.114.5.208
```

2. Configure an emergency web admin in `/opt/algobet/.env`, then manage regular admins from the `Админы` page:

```env
WEB_ADMIN_USERS=evgeniy:LONG_PASSWORD_1,manager:LONG_PASSWORD_2
```

3. Install nginx and certbot:

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

4. Copy the nginx template and edit `server_name`:

```bash
sudo cp /opt/algobet/deploy/algobet-web-nginx.conf /etc/nginx/sites-available/algobet-web
sudo nano /etc/nginx/sites-available/algobet-web
sudo ln -sf /etc/nginx/sites-available/algobet-web /etc/nginx/sites-enabled/algobet-web
sudo nginx -t
sudo systemctl reload nginx
```

5. Issue HTTPS certificate:

```bash
sudo certbot --nginx -d admin.example.com
```

6. Start the Docker services:

```bash
cd /opt/algobet
docker compose up -d --build
```

Then open:

```text
https://admin.example.com/
```

The browser will ask for a web-admin login and password. Regular admins are stored in the database; `WEB_ADMIN_USERS` remains the emergency fallback.
