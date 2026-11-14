# Music Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A self-hosted Discord music bot that streams audio from YouTube using slash
commands. Built with [discord.py](https://github.com/Rapptz/discord.py),
[yt-dlp](https://github.com/yt-dlp/yt-dlp), and FFmpeg.

## Features

- 🎛️ **Live player panel** — one "Now Playing" message with buttons for previous, pause/resume, skip, stop, queue, shuffle, loop, volume, seek, save-to-playlist and playlists. It updates in place instead of flooding the channel, and floats back to the bottom of the chat a few seconds after other messages push it up.
- 🔎 **Search as you type** — `/play` suggests YouTube results (and your saved playlists) while you type. Pick one, or just press Enter to play the top hit.
- 🔗 **Links and playlists** — paste a video link or a whole YouTube playlist link (up to 200 tracks).
- 📜 **Interactive queue** — browse pages, then play now, move up next, or remove any track; shuffle, clear, or save the whole queue as a playlist.
- 💾 **Shared playlists** — save songs to named playlists per server, browse them, and play them (optionally shuffled).
- 🔁 **Loop, history and seek** — loop the track or the whole queue; ⏮ goes back a track (or restarts the current one); ⏩ **Seek** jumps to any time in the song (`1:30`, `+30`, `-10`).
- 🛟 **Resilient playback** — survives internet drops by reconnecting for up to 10 minutes while someone is still listening, then resumes **from where the song cut out**. The next track is preloaded so songs start without a gap.
- ♻️ **Nothing gets lost** — the live queue is saved on every change, so a crash or restart picks up where it left off. Stopping, everyone leaving, or a failed reconnect saves the queue in a separate slot that new playback won't overwrite; bring it back with the **Resume** button or `/continue`.
- 👋 **Auto-disconnect** — leaves 30 s after the channel empties (saving the queue) or after 5 minutes of silence.
- 🤫 **Quiet by design** — control commands answer privately, and the panel footer shows who did what.

## Prerequisites

| Dependency | Notes |
|------------|-------|
| Python 3.10+ | Uses modern type hints |
| deno | JS runtime yt-dlp uses to solve YouTube's challenges (`curl -fsSL https://deno.land/install.sh \| sh`) |
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
to an hour to appear in every server the first time. While developing, set
`DEV_GUILD_ID` in `.env` to sync instantly to a single server.

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

There are only five commands — everything else is a button on the player panel.

| Command | Description |
|---------|-------------|
| `/play <query> [next] [shuffle]` | Play a song, YouTube link, YouTube playlist link, or saved playlist. Suggestions appear as you type. Joins your voice channel automatically. `next` puts it at the front of the queue. |
| `/continue` | Bring back the queue saved by the last stop (same as the **Resume** button on the "Stopped" card) |
| `/nowplaying` | Bring the player panel into the current channel |
| `/playlist view [name]` | Browse playlists, or open one (same as 📚 **Playlists** on the panel) |
| `/playlist add <name> [query]` | Add a song (or a whole YouTube playlist) to a playlist without playing it. Leave `query` empty to add what's playing now. |

### Player panel

| Button | Does |
|--------|------|
| ⏮️ ⏸️ ⏭️ ⏹️ | Previous (or restart the song), pause/resume, skip, stop (leaves and saves the queue for later) |
| 📜 **Queue** | Browse the queue: play now, move up next, remove, shuffle, clear, save as playlist |
| 🔀 🔁 🔉 🔊 | Shuffle, loop (off → queue → track), volume down/up |
| ⏩ **Seek** | Jump to a time: `1:30`, `90`, or `+30` / `-10` relative to now |
| ⭐ **Save** | Add the current song to a playlist (or a new one) |
| 📚 **Playlists** | Browse playlists: play / shuffle-play, remove songs, delete a playlist |

Only people in the bot's voice channel can control playback. Button replies are
private, so the channel only shows the panel and what got queued.

The panel stays at the bottom of the channel: it's reposted when a new song
starts, and a few seconds after chat (or a queued-song message) pushes it up.

Playlists are **shared per server**: anyone in the server can view, edit, play, or delete them. They're created on the fly, with no separate "create" step.

Playlists are stored in `playlists.json` in the project directory. The live and
saved playback queues are stored separately in `sessions.json`.

## Project structure

```
Music_Bot/
├── bot.py              # Entry point: loads cogs, registers persistent buttons, syncs commands
├── player.py           # Playback engine: per-guild GuildPlayer (queue, loop, reconnects, panel)
├── sources.py          # yt-dlp: search, links/playlists → tracks, stream resolution
├── views.py            # Embeds, the player panel, queue/playlist browsers, buttons and menus
├── autocomplete.py     # Slash-command suggestions (live search, playlists, queue)
├── storage.py          # Persistent per-guild playlist storage (JSON)
├── session.py          # Persistent live/saved queue state for resume (JSON)
├── cogs/
│   ├── music.py        # Playback commands
│   └── playlists.py    # /playlist command group
├── CLAUDE.md           # Architecture notes and invariants for AI-assisted development
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
