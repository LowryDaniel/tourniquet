# Running Tourniquet 24/7

`caffeinate` keeps a laptop awake but only while the lid is open. For true always-on enforcement, run Tourniquet somewhere that never sleeps — a tiny machine on your network, a cloud VM, or a container. All paths use the same `pip install tourniquet-dev` package; only the host changes.

The dashboard, alerts, and API behaviour are identical. The only difference is what you put in your agents' `ANTHROPIC_BASE_URL`: instead of `http://localhost:8787`, point at the host's LAN IP or hostname (e.g. `http://192.168.1.42:8787` or `http://tourniquet.lan:8787`).

## Pick a target

| Host | RAM needed | Cost | Setup time | Good for |
|---|---|---|---|---|
| **Docker** on any Linux box | 256 MB | reuses existing host | 2 min | You already have a Docker host |
| **Proxmox LXC** container | 256 MB | reuses existing Proxmox | 5 min | You run a homelab |
| **Raspberry Pi** (Pi 3+ / 4 / 5) | 1 GB | ~£35 once | 10 min | No existing infra; want a tiny dedicated box |
| **Cloud VM** (DigitalOcean / Hetzner / Oracle free) | 512 MB | $0–5/mo | 5 min | No home network access; mobile-first |

All four are functionally equivalent. Pick whichever costs you the least friction to set up.

---

## Docker (single host)

Fastest path if Docker is already on the box.

```
docker run -d \
  --name tourniquet \
  --restart unless-stopped \
  -p 127.0.0.1:8787:8787 \
  -v tourniquet-data:/root/.tourniquet \
  -e DATABASE_URL=sqlite+aiosqlite:////root/.tourniquet/tourniquet.db \
  ghcr.io/lowrydaniel/tourniquet:latest
```

If you want it reachable from other devices on your LAN, change the bind to `-p 8787:8787` (drops the `127.0.0.1:` prefix). **Only do this on a trusted network** — the dashboard has no auth by design.

To check it's up:

```
curl -sI http://localhost:8787/dashboard | head -1
```

To follow logs:

```
docker logs -f tourniquet
```

To upgrade later:

```
docker pull ghcr.io/lowrydaniel/tourniquet:latest
docker rm -f tourniquet
```

Then re-run the original `docker run` command. The `tourniquet-data` volume preserves your keys, caps, and history across restarts.

### Docker Compose

If you prefer a `docker-compose.yml`:

```
services:
  tourniquet:
    image: ghcr.io/lowrydaniel/tourniquet:latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:8787:8787"
    volumes:
      - tourniquet-data:/root/.tourniquet
    environment:
      DATABASE_URL: sqlite+aiosqlite:////root/.tourniquet/tourniquet.db

volumes:
  tourniquet-data:
```

Then:

```
docker compose up -d
```

---

## Proxmox LXC container

Privileged or unprivileged container, both work. Debian 12 template recommended.

### Create the container

In the Proxmox web UI:

1. **Create CT** with these specs:
   - Template: `debian-12-standard`
   - Disk: 4 GB
   - CPU: 1 core
   - RAM: 512 MB
   - Network: DHCP, give it a static reservation in your router
2. Boot it, take note of its IP.

### Install Tourniquet inside the container

`pct enter <CTID>` from the Proxmox host, or SSH in:

```
apt update
apt install -y python3 python3-pip python3-venv
adduser --disabled-password --gecos "" tourniquet
su - tourniquet
python3 -m venv ~/venv
~/venv/bin/pip install tourniquet-dev
```

### Run as a systemd service

As root, create `/etc/systemd/system/tourniquet.service`:

```
[Unit]
Description=Tourniquet — local-first Anthropic API proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tourniquet
Group=tourniquet
WorkingDirectory=/home/tourniquet
ExecStart=/home/tourniquet/venv/bin/tourniquet start --no-browser --port 8787
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then:

```
systemctl daemon-reload
systemctl enable --now tourniquet
systemctl status tourniquet
journalctl -u tourniquet -f
```

The dashboard is at `http://<container-ip>:8787/dashboard`. Open it from your laptop's browser, add your `sk-ant-` key there, copy the resulting `tq_…` token.

### Make it reachable from your LAN

The default `tourniquet start` binds to `127.0.0.1` for safety. To accept connections from other devices on the LAN, edit the `ExecStart` line to add `--host 0.0.0.0`:

```
ExecStart=/home/tourniquet/venv/bin/tourniquet start --no-browser --host 0.0.0.0 --port 8787
```

Then `systemctl restart tourniquet`. **Only do this if your LAN is trusted** — anyone on the same network can read keys and change caps. Dashboard auth is on the v0.2 roadmap.

---

## Raspberry Pi

Pi 4 with 1 GB RAM works fine; Pi 3+ also works. Tested on Raspberry Pi OS Bookworm (Debian 12 base).

```
sudo apt update
sudo apt install -y python3-pip python3-venv
python3 -m venv ~/tourniquet-venv
~/tourniquet-venv/bin/pip install tourniquet-dev
```

Use the same systemd unit as the Proxmox section above, but adjust paths:

```
ExecStart=/home/pi/tourniquet-venv/bin/tourniquet start --no-browser --port 8787
WorkingDirectory=/home/pi
User=pi
```

Save as `/etc/systemd/system/tourniquet.service`, then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now tourniquet
```

Find your Pi's IP with `hostname -I`. Dashboard at `http://<pi-ip>:8787/dashboard`.

---

## Cloud VM (DigitalOcean / Hetzner / Oracle free tier)

Cheapest path: Oracle Cloud's "Always Free" ARM VM (4 vCPU, 24 GB — overkill but free) or a $4/mo Hetzner CX11.

After SSH'ing into a fresh Ubuntu 22.04 / 24.04 box:

```
sudo apt update
sudo apt install -y python3-pip python3-venv
python3 -m venv ~/tourniquet-venv
~/tourniquet-venv/bin/pip install tourniquet-dev
```

Same systemd unit as above. Then **before** opening the firewall, decide the access model:

- **Tailscale / WireGuard** (recommended): expose Tourniquet only on the VPN interface; never publicly reachable. Set `--host 100.64.0.x` to the Tailscale IP.
- **SSH tunnel**: keep `--host 127.0.0.1` and forward `ssh -L 8787:localhost:8787 user@vm` from your laptop.
- **Public + reverse-proxy** (advanced): nginx/caddy in front with HTTP basic auth. Until v0.2 ships dashboard auth, this is the only way to put Tourniquet on the public internet without leaking access to anyone who finds the IP.

---

## Pointing your agents at the remote host

Once the proxy is reachable from where your agents run, swap the env vars:

```
export ANTHROPIC_BASE_URL=http://<host-ip>:8787
export ANTHROPIC_API_KEY=tq_…
```

Replace `<host-ip>` with the Pi's IP / Proxmox container's IP / Tailscale IP / wherever Tourniquet listens. Replace `tq_…` with the token you got from the dashboard.

Everything else — Claude Code, Cursor, Python SDK, Node SDK, curl — works exactly as if Tourniquet were on `localhost`.

---

## Choosing between SQLite and Postgres

The default `pip install` deployment uses **SQLite** — single file at `~/.tourniquet/tourniquet.db`. Plenty for a solo dev with even 50 keys; no separate database to operate.

You only need **Postgres** if you're running Tourniquet for multiple humans (an extension of the v0.1 single-user-multi-key model that's on the v0.2 roadmap). For a homelab / solo deployment, stay on SQLite. The `docker-compose.yml` in the repo root is configured for the Postgres path; ignore it unless you specifically need Postgres.

---

## Upgrading

```
pip install --upgrade tourniquet-dev
sudo systemctl restart tourniquet
```

For Docker, pull the latest image and recreate the container (your data volume preserves state):

```
docker pull ghcr.io/lowrydaniel/tourniquet:latest
docker compose up -d --force-recreate tourniquet
```

---

## Troubleshooting

**Dashboard shows 502 / connection refused from another machine:**
- Tourniquet is bound to 127.0.0.1 only. Change `ExecStart` to add `--host 0.0.0.0` (LXC/Pi/VM) or remove the `127.0.0.1:` prefix from your `-p` flag (Docker).

**`tourniquet start` fails on first run with "could not import":**
- Pip dependencies didn't install cleanly. Re-run `~/tourniquet-venv/bin/pip install --force-reinstall tourniquet-dev`.

**Service starts but immediately exits:**
- `journalctl -u tourniquet -n 50` — read the actual error. Most common is a permission problem on `~/.tourniquet/` if you ran `tourniquet start` once as root by accident, then again as a non-root user.

**Slack/Telegram alerts don't fire from the remote host:**
- Tourniquet's outbound long-poll/Socket Mode connections need internet. Check the host can reach `api.telegram.org` and `slack.com`: `curl -sI https://api.anthropic.com | head -1` for a sanity check.

If something else breaks, paste the relevant journal lines into a GitHub issue at <https://github.com/LowryDaniel/tourniquet/issues>.
