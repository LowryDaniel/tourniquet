# Install Tourniquet

## macOS / Linux

```bash
pip install tourniquet-dev
tourniquet
```

## Windows (PowerShell)

```powershell
pip install tourniquet-dev
tourniquet
```

Windows note: the first time you run `tourniquet` you may see a Windows Defender Firewall prompt. Click **Allow access** for private networks. Python is binding to `127.0.0.1` (localhost only) so the dashboard works in your browser — nothing is exposed to your LAN or the internet.

## From source (any OS)

```bash
git clone https://github.com/LowryDaniel/tourniquet.git
cd tourniquet
python -m venv .venv
```

Activate the virtual environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows cmd.exe
.venv\Scripts\activate.bat
```

Then install and run:

```bash
pip install -e .
tourniquet
```

## What happens on first launch

1. Tourniquet creates `~/.tourniquet/` and writes a `.env` file with freshly generated encryption keys (`FERNET_KEY`, `SECRET_KEY`).
2. Your browser opens automatically at `http://127.0.0.1:8787/dashboard`.
3. The dashboard prompts you to add your first Anthropic API key (`sk-ant-...`) and set a daily spend cap. The key is encrypted at rest with AES-256 (Fernet) — only the ciphertext is stored in the local SQLite database.

From that point on, just run `tourniquet` to start the proxy. Everything stays on your machine.
