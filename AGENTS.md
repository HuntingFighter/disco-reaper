# AGENTS.md — DiscoReaper

Guidance for AI coding agents (Claude Code and others) working in this repository.
Read this file before making changes. If it disagrees with the code, trust the code
and point out the mismatch.

---

## 1. What this project is

DiscoReaper migrates a whole **Discord** server to **Fluxer**: categories and channels,
roles and permissions, emojis and stickers, server name/icon/banner, and the full
message history (attachments, replies, webhook embeds, threads).

Three tool modes (`tool_mode` in config):

- `direct_transfer` — read live from Discord, write to the target.
- `backup_transfer` — read from a local backup, write to the target.
- `backup_only` — back up Discord to disk, no target.

It is a **terminal UI app** (Textual), distributed as PyInstaller binaries for
Linux, Windows and macOS. It is not a long-running bot.

The codebase also contains a Stoat target (`src/stoat/`). It is out of scope for
this file — don't modify it unless asked, but don't break it either (see §9).

---

## 2. Tech stack

- **Python 3.11** (the version CI builds with)
- **Textual** + **Rich** — terminal UI
- **discord.py** — Discord access (read side)
- **fluxer.py** (`git+https://github.com/akarealemil/fluxer.py.git`) — Fluxer access (write side)
- **pydantic** — config model; **PyYAML** — config file
- **SQLite** (stdlib `sqlite3`) — backups and migration state
- **Pillow**, **lottie**, **cairosvg** — sticker/Lottie conversion
- **pytest**, **pytest-asyncio**, **pytest-mock** — tests
- **PyInstaller** — release builds (`disco-reaper.spec`)

Everything is `async`/`await` on asyncio.

---

## 3. Commands

```bash
./launch.sh              # create venv if missing, install requirements, run the app
./runtests.sh            # activate venv, PYTHONPATH=., run pytest -v on tests/
./runtests.sh tests/test_database.py -k mapping   # run a subset
./build.sh               # PyInstaller build -> dist/DiscoReaper + zip
```

Windows equivalents: `launch.bat`, `build.bat`.
Entry point: `disco-reaper.py` → `src.ui.main_app.run_disco_reaper_tui()`.

After a change, run `./runtests.sh` and fix failures before reporting the task done.
The app is a TUI: don't launch it in a non-interactive shell to "test" a change.

Note: the README mentions `launch-app.sh` / `launch-app-MAC.sh` / `launch-app-WIN.bat`;
the repo actually ships `launch.sh` / `launch.bat`.

---

## 4. Repository layout

```
disco-reaper.py           # entry point: logging, SSL setup for frozen builds, terminal relaunch
src/
  core/
    configuration.py      # AppConfig (pydantic), load/save reaper_config.yaml, ReaperFiles-* workspaces
    base.py               # MigrationContext: picks source reader + target writer, validation
    discord_reader.py     # DiscordReader — the ONLY module that imports discord.py
    backup_reader.py      # BackupReader — same interface as DiscordReader, reads a local backup
    backup_database.py    # SQLite backup schema + media pool (CAS, SHA-256 dedup)
    exporter.py           # Discord -> local backup
    database.py           # MigrationDatabase: source->target ID mappings, progress, user aliases
    state.py              # MigrationState: facade over MigrationDatabase
    audit.py              # #reaper-logs audit channel on the target
    updater.py, utils.py
  fluxer/
    writer.py             # FluxerWriter — all Fluxer API calls
    clone_server.py       # categories/channels
    roles_permissions.py  # roles + permission overwrites
    emoji_stickers.py
    server_metadata.py    # name/icon/banner
    migrate_message.py    # message history migration (incl. threads, replies, mentions)
    danger_zone.py        # destructive target operations
  stoat/                  # parallel Stoat target (out of scope, see §1)
  ui/                     # Textual screens, modals, widgets (shuttle_ops.py drives operations)
tests/                    # pytest; conftest.py provides fixtures and mocks
docs/                     # features.md, guide.md, faq.md, backup-specs.md
BOT-SETUP.md              # how users create the Discord and Fluxer bots
```

### Architecture rules

- **Reader / writer split.** Operations get a `MigrationContext` and use
  `context.discord_reader` (source) and `context.writer` (target). Don't call
  discord.py or fluxer.py directly from operation or UI code.
- **`import discord` stays in `discord_reader.py`.** Other modules use the constants
  and helpers it exposes (`MESSAGE_TYPE_*`, `CHANNEL_TYPE_*`, `Forbidden`,
  `find_item`, `create_permission_overwrite`). Add a new constant there rather than
  importing discord elsewhere.
- **`DiscordReader` and `BackupReader` share an interface.** Any method you add to or
  change on one must be mirrored on the other, or backup-mode migrations break.
- **`fluxer.py` calls stay in `src/fluxer/writer.py`.** Operation modules call writer
  methods, not the library.

---

## 5. Hard rules

1. **Discord is read-only.** Never add code that creates, edits or deletes anything
   on the source Discord server. The bot only has *View Channels* and *Read Message
   History*; the README promises users their original data is never touched.
2. **Destructive actions only on the target, only with confirmation.** Everything in
   `danger_zone.py` (delete channels/roles/emojis/stickers, reset permissions, remove
   logo/banner) must keep its preview + dedicated confirmation step in the UI. When a
   destructive action deletes target objects, it must also clear the matching
   mappings in `MigrationState` (as the existing functions do).
3. **Never run the app against real servers** unless the user explicitly asks in this
   session.
4. **Secrets and user data stay local.** Never read out, print, log, commit or paste:
   - `reaper_config.yaml` (holds bot tokens)
   - `ReaperFiles-*/`, `DISCORD_BACKUP-*/`, `*.db`, `.reaper.log`, `.reaper_tests.log`
   - `ReaperFiles-AutoTest/` (real test data and tokens, if present)
   These are gitignored — keep it that way. Never log a token, even at DEBUG.
5. **Respect `anonymize_users`.** Any new code path that outputs a user's name or
   avatar to the target must use the alias from `state.get_user_alias()` when
   anonymization is on.
6. **Don't commit `src/core/_baked_version.py`** — it's generated during builds.

---

## 6. Discord (source) rules

Reference: https://docs.discord.com/developers/reference

- Access goes through **discord.py**, which handles API versioning, auth headers and
  rate limits. Don't hand-roll `aiohttp` calls to `discord.com/api`, and don't add
  another Discord library.
- `DiscordReader` uses REST `fetch_*` calls after `client.login()`; it doesn't
  rely on the Gateway cache. Keep it that way.
- Required intents: `guilds`, `members`, `message_content`. Required bot permissions:
  View Channels, Read Message History. If a feature needs more, update
  `validate()` and `BOT-SETUP.md`, and say so — but never add write permissions.
- **Snowflakes:** 64-bit; discord.py gives `int`, mappings store them as `str`.
  Never truncate or float-convert. Resume logic depends on snowflake order:
  messages are skipped when `id <= last migrated id` for that channel/thread.
  Timestamp from an ID: `(id >> 22) + 1420070400000` (ms).
- **Pagination:** history is fetched with `after_id`; never assume one page is the
  whole channel.
- **Attachment URLs are signed and expire** (`ex`/`is`/`hm` params). Download bytes
  during export/migration; never store a signed CDN URL as the long-term reference.
  Unsigned CDN assets (icons, emojis, avatars) are fine to reference.
- Handle `discord.Forbidden` per channel: log and skip the channel, don't abort the
  whole run.

---

## 7. Fluxer (target) rules

- Access goes through **fluxer.py** (`from fluxer import Bot, Webhook, HTTPClient, ...`).
  There's no separate API docs link for this project — when you need a method, read
  the fluxer.py source rather than guessing a signature.
- `FluxerWriter` runs a `fluxer.Bot` in a background task, waits for `on_ready`
  (10 s timeout), and uses the bot's HTTP client (`self.client`) for REST calls.
- **Custom instances:** honour `fluxer_api_url` (`"default"` means the public
  instance). Never hardcode an API URL.
- **Messages:** sent through a per-channel webhook named `ReapersWebhook` (cached in
  `_webhooks`) so the original author name and avatar are shown. fluxer.py's webhook
  execute doesn't support `message_reference`, so **replies are sent via the bot**
  instead. Migrated messages can't be edited or deleted by their original authors.
- **Model:** Fluxer is Discord-like (snowflake IDs, permission bitfields, overwrites —
  e.g. `deny=1024` is View Channel) but not identical. Check before assuming a
  Discord field, channel/message type or permission maps 1:1.
- **Threads and forums:** Fluxer supports them (types 10/11/12, forum 15, media 16), but
  fluxer.py has no thread routes yet, so `FluxerWriter` calls the REST API directly via
  `self.client.request(Route(...))` (see https://docs.fluxer.app/http-api/threads/).
  Cloning creates threads under their mapped parent and stores them with
  `set_channel_mapping(thread_id, ...)`; forum posts are created during message migration
  together with their first message (webhook `thread_name`). Webhooks can't live on a
  thread, so thread messages go through the parent's webhook with `?thread_id=`. If a
  thread can't be created, messages fall back to inline markers in the parent channel.
- **Stability:** Fluxer has stability issues under high traffic. Writes must be
  resumable — record each mapping (`set_*_mapping`, `set_message_mapping`,
  `update_last_message_id`) right after the target confirms, so a crash or restart
  continues where it stopped without duplicates. Don't batch progress updates to the
  end of a run.
- **Audit channel:** `audit.py` creates and locks a `#reaper-logs` channel on the
  target and posts progress via `writer.send_marker`. Keep audit messages free of
  tokens.

---

## 8. State, backups and idempotency

- **Backups** (`BackupDatabase`): SQLite with `guild_profile`, `roles`, `permissions`,
  `channels`, `threads`, `messages`, `attachments`, `embeds`, `reactions`, `users`,
  `user_alias`, plus a `media_pool` table and a `cas/` folder (SHA-256 content-addressed,
  deduplicated media). Incremental backups fetch only messages after the newest
  stored ID per channel. See `docs/backup-specs.md`.
- **Migration state** (`MigrationState` → `MigrationDatabase`): source→target
  mappings for channels, categories, roles, emojis, stickers and messages, plus
  per-channel/thread progress and completion flags.
- **Schema changes must be backward compatible** — users resume migrations on
  existing databases. Add columns/tables with `IF NOT EXISTS`/migrations; never drop
  or rename existing ones without a migration path.
- Every operation must be **safe to re-run**: check the mapping before creating a
  target object, and skip already-migrated messages.

---

## 9. Testing

- Run with `./runtests.sh` (sets `PYTHONPATH=.`). Logs go to `.reaper_tests.log`.
- `conftest.py` uses real data from `ReaperFiles-AutoTest/` if it exists and falls
  back to mocks otherwise. Tests must pass in **mock mode** (no real data, no network).
- Use the fixtures: `mock_discord_reader`, `mock_fluxer_writer`, `mock_stoat_writer`,
  `backup_db`, `backup_reader`, `temp_db`, `reaper_config`.
- No real network calls in tests; mock writer/reader methods with `AsyncMock`.
- Some tests are parametrized over both targets (`get_platforms()`). If you change
  shared code in `src/core/` or `src/ui/`, those Stoat cases must still pass.
- Add tests for: mention/emoji/channel remapping in `clean_mentions`, resume/skip
  logic, mapping cleanup after Danger Zone actions, and DB schema changes.

---

## 10. Working agreements

- Smallest change that solves the task; don't refactor unrelated code.
- Use `logging.getLogger(__name__)`, not `print()`, in new code.
- Don't add dependencies to `requirements.txt` without explaining why; check that
  they bundle with PyInstaller (`disco-reaper.spec`).
- Keep user-facing changes reflected in `docs/` and `BOT-SETUP.md`.
- Don't edit `.github/workflows/release.yml`, `disco-reaper.spec` or this file unless
  asked.
- Finish every task with: what changed, how it was verified (test output), and any
  open issues.