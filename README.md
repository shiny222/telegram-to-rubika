# Telegram → Rubika Bridge

Forwards messages, photos and videos from a Telegram channel to a Rubika chat/channel in real time.

## Requirements

- Python 3.10 or newer
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A Rubika account token

## Setup

### 1. Create your config

```bash
cp config.example.json config.json
```

`config.json` is **gitignored** — never commit it. It holds all your secrets.

### 2. Fill in the values

| Key | What it is |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather (e.g. `123456:ABC...`) |
| `RUBIKA_TOKEN` | Your Rubika account token (from the Rubika app) |
| `RUBIKA_DEFAULT_CHAT_ID` | The Rubika chat/channel you want messages sent to |
| `MY_CHANNEL_LINK` | Your Telegram channel link, e.g. `@MyChannel` |
| `ALBUM_FLUSH_DELAY_PHOTO_SECONDS` | Seconds to wait before forwarding a photo album (default `5`) |
| `ALBUM_FLUSH_DELAY_VIDEO_SECONDS` | Seconds to wait before forwarding a video album (default `10`) |

#### `LOCAL_API` (optional, recommended for big files)

Uses a [Telegram Local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server)
instead of the public API so large files (up to 2 GB) can be downloaded:

- `enabled` — `true`/`false`
- `api_id` / `api_hash` — from [my.telegram.org](https://my.telegram.org)
- `executable_path` — path to the `telegram-bot-api` binary
- `host` / `port` — where the local API server listens (default `127.0.0.1:8081`)
- `start_server` — set `true` to let the bridge launch the server itself
- `max_download_mb` — max download size in MB (default `2000`)

#### `SHARED_CACHE`

A download cache **shared with the Bale bridge**, so files are only downloaded once:

- `dir` — cache folder path
- `keep_hours` — how long files are kept (default `1`)
- `max_gb` — maximum cache size (default `20`)

#### `UPLOAD`

Upload retry behaviour:

- `max_attempts` — retries per file (default `3`)
- `retry_delay_sec` — delay between retries (default `30`)
- `base_timeout_sec` / `timeout_per_mb` — upload timeout calculation
- `pending_queue` — file where failed uploads are queued (default `pending_uploads.jsonl`)

### 3. Install dependencies & run

**Linux:**

```bash
./run_bot.sh
```

**Windows:**

```
run_bot.bat
```

The launcher creates a virtual environment and installs `requirements.txt`
automatically on first run.

### Update

`./update.sh` (Linux) or `update.bat` (Windows) pulls the latest code from git.

## Useful tools

- `rubika_test.py` — sends a test message straight to Rubika (reads your token from `config.json`)

## Security

- Never commit `config.json` — it is listed in `.gitignore`.
- If a token ever leaks to a public repo, **revoke it immediately** and regenerate it.
