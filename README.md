# Music Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A self-hosted Discord music bot that streams audio from YouTube using slash
commands. Built with [discord.py](https://github.com/Rapptz/discord.py),
[yt-dlp](https://github.com/yt-dlp/yt-dlp), and FFmpeg.

## Features

- 🎵 **Stream from YouTube** — play a direct link or search by keywords and pick from a dropdown
- 📋 **Per-guild queues** — append, view, skip, pause/resume, and clear the queue
- 💾 **Shared playlists** — save tracks into named playlists per server, then play them (with optional shuffle)
- 🔁 **Resilient playback** — survives internet drops by reconnecting for up to 10 minutes (whenever someone is still in the channel) and re-fetches expired stream URLs
- 💿 **Resumable queue** — the live queue is saved to disk on every change, so a crash or restart picks up where it left off; `/stop` and `/leave` offer to save the queue for a later `/continue`
- 👋 **Auto-disconnect** — leaves when the channel empties (saving the queue for `/continue` if one was playing) or after 5 minutes of inactivity

## Prerequisites

| Dependency | Notes |
|------------|-------|
| Python 3.10+ | Uses `asyncio.get_running_loop()` and modern type hints |
| [uv](https://docs.astral.sh/uv/) | Package and venv manager (or use plain `pip`) |
| FFmpeg | `ffmpeg` and `ffprobe` must be on your `PATH` |
| A Discord bot token | Create one at [discord.com/developers](https://discord.com/developers/applications) |

Installing FFmpeg:

```bash
# Debian / Ubuntu
sudo apt install ffmpeg

# Arch / Manjaro
sudo pacman -S ffmpeg

# macOS (Homebrew)
brew install ffmpeg

# Windows (winget) — or download from https://ffmpeg.org/download.html
winget install Gyan.FFmpeg
```

Verify it works with `ffmpeg -version`.

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/CaptainShepard01/Music_Bot.git
cd Music_Bot

# 2. Create a virtual environment and install dependencies
uv venv
uv pip install -r requirements.txt
# (or with pip: python -m venv .venv && .venv/bin/pip install -r requirements.txt)

# 3. Create your .env file and add your token
cp .env.example .env
```

Then edit `.env`:

```
DISCORD_TOKEN=your_discord_bot_token_here
```

> **Never commit `.env`** — it holds your secret token. It is already listed in `.gitignore`.

### Discord developer portal

When you generate the bot's invite URL, enable:

- **Scopes:** `bot` and `applications.commands`
- **Bot permissions:** Connect, Speak, Send Messages, Embed Links

No privileged gateway intents are required.

## Running

```bash
uv run python bot.py
```

Slash commands are registered globally on startup. Global commands can take up
to an hour to appear in every server the first time.

## Running as a systemd service (Linux)

The bot ships with a systemd unit and an install script that auto-detects your
user, working directory, and venv Python path. Run it from inside the project
directory:

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

> **Note:** systemd's `EnvironmentFile` reads `KEY=VALUE` pairs literally — do
> **not** wrap the value in quotes in `.env` (a plain `DISCORD_TOKEN=abc123` is correct).

## Commands

### Playback

| Command | Description |
|---------|-------------|
| `/join` | Join your current voice channel |
| `/play <query>` | Play a YouTube URL, or search and pick from a dropdown |
| `/pause` | Pause playback |
| `/resume` | Resume paused playback |
| `/skip` | Skip the current song |
| `/stop` | Stop playback and clear the queue (offers to save it for `/continue`) |
| `/queue` | Show the current queue |
| `/continue` | Resume a previously saved queue |
| `/leave` | Disconnect the bot from voice (offers to save the queue for `/continue`) |

### Playlists

Playlists are **shared per server** — anyone in the guild can view, edit, play, or delete them.

| Command | Description |
|---------|-------------|
| `/playlist create <name>` | Create a new empty playlist |
| `/playlist add <name> <query>` | Add a track (URL or search) to a playlist |
| `/playlist addcurrent <name>` | Add the currently playing track to a playlist |
| `/playlist remove <name> <track>` | Remove a track from a playlist |
| `/playlist show <name>` | Show the tracks in a playlist |
| `/playlist list` | List all playlists |
| `/playlist play <name> [mode] [shuffle]` | Play a playlist (append or replace the queue, optionally shuffled) |
| `/playlist delete <name>` | Delete a playlist (with confirmation) |

Playlists are stored in `playlists.json` in the project directory. The live
playback queue is saved separately in `sessions.json` (also in the project
directory) so it can be resumed after an outage or restart.

## Project structure

```
Music_Bot/
├── bot.py              # Entry point: loads cogs, syncs slash commands, runs the bot
├── player.py           # Playback engine: queue state, yt-dlp helpers, player loop
├── storage.py          # Persistent per-guild playlist storage (JSON)
├── session.py          # Persistent live-queue state for crash/drop resume (JSON)
├── cogs/
│   ├── music.py        # Playback and queue commands
│   └── playlists.py    # /playlist command group
├── requirements.txt    # Python dependencies
├── .env.example        # Template for .env
├── install-service.sh  # Installs the systemd service on Linux
├── music-bot.service   # systemd unit file
├── LICENSE
└── README.md
```

## Contributing

Issues and pull requests are welcome. If you find a bug or have a feature idea,
please open an issue first to discuss it.

## Disclaimer

This project is intended for personal, self-hosted use. You are responsible for
ensuring your usage complies with [YouTube's Terms of Service](https://www.youtube.com/t/terms)
and the laws in your jurisdiction.

## Author

Created and maintained by **Anton Balykov** ([@CaptainShepard01](https://github.com/CaptainShepard01)).

## License

MIT — see [LICENSE](LICENSE).
