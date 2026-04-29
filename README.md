# Music Bot

A Discord slash-command music bot that streams audio from YouTube via yt-dlp and FFmpeg. Supports per-guild queues, pause/resume, skip, and auto-disconnect on inactivity.

## Prerequisites

| Dependency | Notes |
|------------|-------|
| Python 3.10+ | Uses `asyncio.get_running_loop()` and modern type hints |
| FFmpeg | Must be on `PATH` (`ffmpeg`, `ffprobe`) |
| A Discord bot token | [discord.com/developers](https://discord.com/developers/applications) |

On Manjaro / Arch Linux:
```bash
sudo pacman -S python ffmpeg
```

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/Music_Bot.git
cd Music_Bot

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env file
cp .env.example .env
# Then edit .env and paste your Discord bot token
```

## Configuration

Edit `.env` (never commit this file):

```
DISCORD_TOKEN=your_discord_bot_token_here
```

**Required bot permissions** (set in the Discord developer portal):
- `bot` scope + `applications.commands` scope
- Voice permissions: Connect, Speak
- Text permissions: Send Messages, Embed Links

## Running locally

```bash
source .venv/bin/activate
python bot.py
```

## Running as a systemd service on Manjaro Linux

Run the install script from inside the project directory — it auto-detects your user, working directory, and venv Python path:

```bash
chmod +x install-service.sh
./install-service.sh
```

Then start and (optionally) enable the service:

```bash
sudo systemctl start music-bot
sudo systemctl enable music-bot  # auto-start on boot
journalctl -u music-bot -f       # follow logs
```

> **Note:** systemd's `EnvironmentFile` reads `KEY=VALUE` pairs literally — do **not** wrap values in quotes in `.env` when using systemd (a plain `DISCORD_TOKEN=abc123` is fine).

## Commands

| Command | Description |
|---------|-------------|
| `/join` | Join your current voice channel |
| `/play <query>` | Play a YouTube URL or search for a song |
| `/pause` | Pause playback |
| `/resume` | Resume paused playback |
| `/skip` | Skip the current song |
| `/stop` | Stop playback and clear the queue |
| `/queue` | Show the current queue |
| `/leave` | Disconnect the bot from voice |

## Project structure

```
Music_Bot/
├── bot.py              # Main bot entry point
├── requirements.txt    # Python dependencies
├── .env                # Secret tokens (not committed)
├── .env.example        # Template for .env
├── music-bot.service   # systemd unit file for Linux deployment
├── LICENSE
└── README.md
```

## License

MIT — see [LICENSE](LICENSE).
