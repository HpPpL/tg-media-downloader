# tg-media-downloader

Download videos, photos, documents, audio, voice messages and round videos from Telegram chats,
groups and channels. It runs as your own account over MTProto ([Telethon](https://github.com/LonamiWebs/Telethon)),
so it sees everything you can see and isn't limited to the Bot API's 20 MB — files up to 2–4 GB work fine.

```
✓ 12_video_1.mp4 (512.4 MB)
✓ 13_voice_1.ogg (1.2 MB)
✓2 ↷0 ✗0 │ 513.6 MB │ 6.0 MB/s │ 14_video_2.mp4 51% │ 15_document_1.pdf 12%
```

## Features

- Pick a chat by `@username`, a `t.me/…` link (including `t.me/c/123/456`), a numeric id or `me` (Saved Messages).
- Filters: file type, size (global or per type), date range, message id range, forum topic, count.
- Parallel downloads with a live progress line and speed.
- Resumable: each file is written to `.part` and renamed only when complete, so a rerun skips what's already there.
- Retries on network errors, waits out FloodWait, refreshes expired file references.
- `--dry-run` shows what would be downloaded and the total size without downloading anything.

## Setup

1. Get an `api_id` and `api_hash` at [my.telegram.org](https://my.telegram.org) → API development tools
   (the short name must be alphanumeric; if the form just says `ERROR`, turn off your VPN).
2. Install dependencies and fill in `.env`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set TG_API_ID and TG_API_HASH
```

3. The first run asks for your phone number, the login code from Telegram and your 2FA password if you have one.
   After that the login is stored in `tg_downloader.session`.

```bash
python tg_download.py --list-chats
```

## Usage

```bash
# what's in the channel and how much it weighs
python tg_download.py https://t.me/somechannel --dry-run

# only videos and documents, the latest 50
python tg_download.py @somechannel -t video,document -n 50

# videos up to 200 MB, voice messages up to 5 MB, sorted into subfolders, 4 parallel downloads
python tg_download.py -1001234567890 -t video,voice --max-size video=200 --max-size voice=5 --by-type -j 4

# everything since the start of the year into a custom folder
python tg_download.py @somechannel --since 2026-01-01 -o ./downloads/somechannel
```

| Flag | What it does |
|---|---|
| `--list-chats` | list your chats with their ids |
| `-o DIR` | output folder (default: `downloads/<chat title>`) |
| `-t TYPES` | `photo,video,document,audio,voice,round,gif,sticker` or `all`; default is everything except stickers |
| `--max-size [TYPE=]SIZE` | skip files larger than SIZE: `100` (MB), `500K`, `1.5G`; repeat for different types |
| `--min-size [TYPE=]SIZE` | skip files smaller than SIZE |
| `-n N` | at most N files, newest first |
| `--since` / `--until` | date range `YYYY-MM-DD`, both ends inclusive |
| `--min-id` / `--max-id` | message id range |
| `--topic ID` | topic in a forum group |
| `--by-type` | sort into subfolders `video/`, `photo/`, … |
| `-j N` | parallel downloads (default 3, max 10) |
| `--dry-run` | only list the files |

Files are named `<message id>_<original name>`, so they sort in posting order.
If a file has no name, it becomes `<message id>_<type>_<date>.<ext>`.
Each file's modification time is set to the message date.

## How it works

1. Telethon connects to Telegram over MTProto like a regular client and authorizes with the key from `.session`.
2. `iter_messages` pages through the chat history 100 messages at a time, newest first. The file type comes
   from document attributes: a video with `round_message` is a round video, one with `Animated` is a GIF, and so on.
3. Matching messages go into a short queue that N async workers pull from.
4. Each file is fetched in chunks via `upload.getFile` from the Telegram data center that stores it;
   `cryptg` speeds up decrypting the chunks.

## Security

`tg_downloader.session` gives full access to your account without a code or password — never share it.
It is excluded in `.gitignore`, along with `.env`. You can end the session in Telegram: Settings → Devices.

## License

[MIT](LICENSE)
