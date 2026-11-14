# Music Bot — notes for Claude

Self-hosted Discord music bot (discord.py 2.4+, yt-dlp, FFmpeg). Streams YouTube
audio into voice channels; controlled by slash commands plus a live
"Now Playing" panel with buttons. Production runs on Linux under systemd;
development happens on Windows.

## Commands

```bash
uv venv && uv pip install -r requirements.txt   # setup (or sync after requirement bumps)
.venv/Scripts/python.exe bot.py                 # run on Windows (.venv/bin/python on Linux)
.venv/Scripts/python.exe -m py_compile *.py cogs/*.py   # quick syntax check
```

- There is **no test suite**. Verify with `py_compile`, then offline smoke
  scripts (in the scratchpad) that fake the guild/voice client — see "Testing".
- Running `bot.py` logs in as the real bot and re-syncs its global commands.
  Ask before doing it. Set `DEV_GUILD_ID` in `.env` for instant per-guild sync.
- yt-dlp must stay current: stale versions get **403 Forbidden** from YouTube
  streams. If FFmpeg reports 403, upgrade yt-dlp before suspecting the code.
- `remote_components: ["ejs:github"]` needs a JS runtime (deno) on PATH for
  YouTube challenge solving; the systemd unit adds `~/.deno/bin` to PATH.

## Architecture

| File | Role |
|------|------|
| `bot.py` | `MusicBot`: loads stores + cogs, registers persistent views, syncs commands, auto-resumes sessions in `on_ready`, global app-command error handler |
| `player.py` | `GuildPlayer` (one per guild, via `player.get(guild)` / `player.find(id)`): queue (deque), history, loop mode, volume, voice connection, player loop, panel message, persistence |
| `sources.py` | `Track` dataclass + yt-dlp: `search` (flat, cached), `find` (query/URL/playlist → tracks), `resolve` (stream URL). `SourceError` = user-facing message |
| `views.py` | All UI: formatting (`fmt_time`, `parse_time`, `clean`, `link`), embeds, `PlayerControls`, `SavedQueueView`, `QueueView`, `AddToPlaylistView`, `PlaylistBrowser`, `ConfirmView`, shared helpers `reply`, `voice_target`, `ensure_listener`, `enqueue`, `play_playlist` |
| `autocomplete.py` | Autocomplete providers (live YouTube search, playlist names, queue positions) |
| `session.py` | `sessions.json`: per guild `live` (auto-resumed after crash/restart) and `saved` (parked for `/continue`) slots |
| `storage.py` | `playlists.json`: shared per-guild playlists (case-insensitive names) |
| `cogs/music.py` | `/play /nowplaying /continue` + voice-state and message listeners |
| `cogs/playlists.py` | `/playlist view/add` |

`player` and `views` import each other (module-level `import x`, attribute
access only at call time, `from __future__ import annotations`). Keep it that
way; never `from player import …` inside `views` or the reverse.

## Invariants — read before editing the player

- **Reconnect logic is battle-tested** (`ensure_voice`, the drop detection in
  `GuildPlayer._play`). Several commits went into it. A track that ends while
  disconnected or >10 s before its duration counts as a drop: re-fetch the URL,
  resume at the cut-off position (`-ss`). Reconnect only while a human is in the
  channel, for up to 10 min, then park.
- `GuildPlayer._interrupt` records *why* the source stopped: `skip`,
  `previous`, `replace`, `stop`, `failed`, `seek`, or `None` (natural end).
  `_finish` uses it for history and loop handling. Any new control that stops
  the source must set a reason via `_stop_source`. `seek` is special: `_play`
  consumes it and restarts the same stream at `_seek_to` (FFmpeg `-ss`) without
  ending the track. Drops resume the same way.
- `Track` has `eq=False` on purpose: queue entries are matched by identity
  (`is`), so duplicates of one song stay distinct. Use `track.copy()` before
  putting a track somewhere else (playlists, a second queue).
- Stream URLs are transient (`stream_url`, `resolved_at`); never persist them.
  `Track.from_dict` must keep reading the legacy shapes (`{url,title,duration}`
  playlists, `{webpage_url,title}` old sessions).
- Persistence: `gp.persist()` runs on every queue change (fire-and-forget,
  snapshot taken at call time). `park()` disables persistence until the next
  `add/start`, so the loop can't overwrite a parked queue.
- Persistent view `custom_id`s (`mb:*`) are a public contract with messages
  already posted in servers. Don't rename them.
- Never name a method or attribute on a `View`/`Modal` subclass after one of
  discord.py's private members (`_refresh`, `_children`, `_cache_key`, …).
  discord.py calls those from the gateway loop: a `PlayerControls._refresh`
  once made every panel edit crash the whole process. Check with
  `hasattr(discord.ui.View, name)` before adding a `_helper`.

## UX conventions

- Keep channels quiet: control commands reply **ephemerally**, and the panel
  footer (`gp._note(...)`) records who did what. Queueing replies are public
  (small green embed with the requester's avatar).
- **Buttons first, no duplicate commands.** A slash command exists only when
  there's no button for it (`/play`, `/continue`, `/nowplaying`,
  `/playlist view/add`). New controls go on the panel, `QueueView` or
  `PlaylistBrowser`, not into new commands.
- There is one live panel per guild. `gp.update_panel(move_to_bottom=True)` on
  track change edits it in place if it's still the latest message, otherwise
  reposts it. Other changes call `gp.schedule_panel_update()` (debounced 1 s).
- The panel is sticky: `on_message` → `gp.on_channel_message` reposts a buried
  panel once the channel has been quiet for `STICKY_QUIET` s (at most
  `STICKY_MAX_WAIT` s during busy chat). Every panel edit, repost and close goes
  through `gp._panel_lock`, so two of them can't double-post.
- Errors: plain `⚠️ …` text; public errors after a deferred `/play` auto-delete
  (`reply(..., delete_after=20)`).
- Escape user and YouTube text with `views.clean()` / `views.link()` before it
  goes into markdown.
- Discord limits: autocomplete must answer within 3 s (search is capped at
  2.4 s and cached); max 25 select options and 25 autocomplete choices; 5
  buttons per row and 5 rows; embed description 4096 chars and field 1024 (use
  `views._fit`); choice values ≤ 100 chars.

## Testing approach

Write throwaway scripts in the scratchpad that copy the modules and:
- build a `GuildPlayer` with a `types.SimpleNamespace` guild, then drive
  `add/skip/previous/_finish` and check queue and history;
- monkeypatch `sources.resolve` and `GuildPlayer._stream`, give it a fake
  `VoiceClient` and text channel, and run the real `_run` loop;
- render `views.render_panel`, `QueueView`, `PlaylistBrowser`, and check
  `tree.get_commands()` plus `cmd.to_dict(tree)` to validate command schemas.
Network checks (`sources.search/find/resolve`, FFmpeg `-ss`) hit YouTube for
real and are fine to run.
