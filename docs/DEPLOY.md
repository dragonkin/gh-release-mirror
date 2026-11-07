# Deploying gh-release-mirror

A 1 vCPU / 1 GB RAM VPS is more than enough. The service is a single Python file with no third-party dependencies.

## 1. Install

```bash
sudo apt-get update && sudo apt-get install -y python3 git
sudo mkdir -p /opt/gh-release-mirror && cd /opt/gh-release-mirror
sudo git clone https://github.com/dragonkin/gh-release-mirror.git .
sudo cp config.example.json config.json
sudo nano config.json          # set repos, port, optional github_token
```

## 2. First sync

```bash
sudo python3 mirror.py sync --config config.json
```

## 3. systemd unit

`/etc/systemd/system/gh-release-mirror.service`:

```ini
[Unit]
Description=gh-release-mirror
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/gh-release-mirror
ExecStart=/usr/bin/python3 /opt/gh-release-mirror/mirror.py serve --config /opt/gh-release-mirror/config.json
Restart=always
RestartSec=5
DynamicUser=yes
StateDirectory=gh-release-mirror

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gh-release-mirror
curl -s http://127.0.0.1:8080/healthz
```

## 4. Periodic sync (optional)

`/etc/systemd/system/gh-release-mirror-sync.timer` runs a sync every 6 hours:

```ini
[Unit]
Description=Sync GitHub releases

[Timer]
OnCalendar=*-*-* 00/6:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`/etc/systemd/system/gh-release-mirror-sync.service`:

```ini
[Unit]
Description=gh-release-mirror sync run

[Service]
Type=oneshot
WorkingDirectory=/opt/gh-release-mirror
ExecStart=/usr/bin/python3 /opt/gh-release-mirror/mirror.py sync --config /opt/gh-release-mirror/config.json
```

```bash
sudo systemctl enable --now gh-release-mirror-sync.timer
```

## 5. nginx reverse proxy (optional)

```nginx
server {
    listen 80;
    server_name mirror.example.com;

    client_max_body_size 0;
    proxy_read_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
    }
}
```

Set `base_url` in `config.json` to `https://mirror.example.com` so index links point at the public host.
