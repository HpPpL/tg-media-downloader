#!/usr/bin/env python3
"""
Download files (photos, videos, documents, audio, voice messages…) from a Telegram
chat, group or channel via the MTProto API, acting as your own account (Telethon).

Examples:
  python tg_download.py --list-chats
  python tg_download.py @somechannel
  python tg_download.py https://t.me/somechannel -t video,document -n 50
  python tg_download.py -1001234567890 --since 2026-01-01 --by-type -j 4
  python tg_download.py me --dry-run            # Saved Messages
  python tg_download.py @chat -t video,voice --max-size 100 --max-size voice=5
                                                # videos up to 100 MB, voice up to 5 MB
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, errors, utils
from telethon.tl.types import MessageMediaWebPage

SCRIPT_DIR = Path(__file__).resolve().parent
KINDS = ("photo", "video", "document", "audio", "voice", "round", "gif", "sticker")
DEFAULT_KINDS = tuple(k for k in KINDS if k != "sticker")
MAX_RETRIES = 5


# ---------- helpers ----------

def human(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"


_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def safe_name(name: str, limit: int = 150) -> str:
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if len(name) > limit:
        stem, ext = os.path.splitext(name)
        name = stem[: max(1, limit - len(ext))] + ext
    return name or "file"


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def get_credentials() -> tuple[int, str]:
    for p in (Path.cwd() / ".env", SCRIPT_DIR / ".env"):
        load_env(p)
    api_id = os.environ.get("TG_API_ID") or input("api_id (my.telegram.org): ").strip()
    api_hash = os.environ.get("TG_API_HASH") or input("api_hash: ").strip()
    if not api_id.isdigit() or not api_hash:
        sys.exit("TG_API_ID (a number) and TG_API_HASH are required — see .env.example")
    return int(api_id), api_hash


def normalize_chat(raw: str) -> str | int:
    """@name, t.me/name, t.me/name/123, t.me/c/123/456, t.me/s/name, -100…, me."""
    raw = raw.strip()
    m = re.match(r"(?:https?://)?(?:www\.)?t(?:elegram)?\.me/(.+)", raw)
    if m:
        parts = m.group(1).split("?")[0].strip("/").split("/")
        if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
            raw = f"-100{parts[1]}"
        elif parts[0] == "s" and len(parts) > 1:
            raw = parts[1]
        elif not parts[0].startswith(("+", "joinchat")):
            raw = parts[0]
        # invite links (t.me/+…) are passed to Telethon as is
    return int(raw) if re.fullmatch(r"-?\d+", raw) else raw


def media_kind(msg) -> str | None:
    if getattr(msg, "action", None) or msg.media is None:
        return None
    if isinstance(msg.media, MessageMediaWebPage):
        return None
    # order matters: stickers, round videos and GIFs also carry video attributes
    if msg.photo:
        return "photo"
    if msg.sticker:
        return "sticker"
    if msg.video_note:
        return "round"
    if msg.gif:
        return "gif"
    if msg.video:
        return "video"
    if msg.voice:
        return "voice"
    if msg.audio:
        return "audio"
    if msg.document:
        return "document"
    return None


def size_ok(size: int, kind: str, args) -> bool:
    lo = args.min_size.get(kind, args.min_size.get("*"))
    hi = args.max_size.get(kind, args.max_size.get("*"))
    return (lo is None or size >= lo) and (hi is None or size <= hi)


def target_name(msg, kind: str) -> str:
    f = msg.file
    if f and f.name:
        return f"{msg.id}_{safe_name(f.name)}"
    ext = (f.ext if f else "") or ""
    return f"{msg.id}_{kind}_{msg.date:%Y%m%d_%H%M%S}{ext}"


# ---------- progress ----------

class Progress:
    def __init__(self, live: bool):
        self.live = live
        self.done = self.skipped = 0
        self.failed: list[str] = []
        self.bytes = 0
        self.active: dict[str, tuple[int, int]] = {}
        self.started = time.monotonic()
        self._speed = 0.0

    def log(self, text: str) -> None:
        if self.live:
            sys.stdout.write("\r\033[K")
        print(text, flush=True)

    def render(self) -> None:
        width = shutil.get_terminal_size((100, 20)).columns
        line = (f"✓{self.done} ↷{self.skipped} ✗{len(self.failed)} │ "
                f"{human(self.bytes)} │ {human(self._speed)}/s")
        for name, (cur, tot) in list(self.active.items())[:3]:
            pct = f"{cur * 100 // tot}%" if tot else human(cur)
            line += f" │ {name[:24]} {pct}"
        sys.stdout.write("\r\033[K" + line[: width - 1])
        sys.stdout.flush()

    async def ticker(self) -> None:
        prev_b, prev_t = self.bytes, time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            inst = (self.bytes - prev_b) / (now - prev_t)
            self._speed = 0.7 * self._speed + 0.3 * inst
            prev_b, prev_t = self.bytes, now
            self.render()

    def summary(self, out_dir: Path) -> None:
        elapsed = int(time.monotonic() - self.started)
        self.log(f"\nDone in {elapsed // 60}m{elapsed % 60:02d}s: downloaded {self.done} "
                 f"({human(self.bytes)}), skipped {self.skipped}, "
                 f"failed {len(self.failed)} → {out_dir}")
        for line in self.failed:
            print(f"  ✗ {line}")


# ---------- Telegram ----------

async def resolve_chat(client: TelegramClient, raw: str):
    target = normalize_chat(raw)
    try:
        return await client.get_entity(target)
    except ValueError:
        # a numeric id resolves only once the entity is in the session cache
        await client.get_dialogs()
        return await client.get_entity(target)


async def list_chats(client: TelegramClient) -> None:
    async for d in client.iter_dialogs():
        kind = "group" if d.is_group else "channel" if d.is_channel else "user"
        uname = getattr(d.entity, "username", None)
        print(f"{d.id:>16}  {kind:<7}  {d.name}" + (f"  @{uname}" if uname else ""))


async def iter_media(client: TelegramClient, entity, args):
    kw = {}
    if args.min_id:
        kw["min_id"] = args.min_id
    if args.max_id:
        kw["max_id"] = args.max_id
    if args.until:
        kw["offset_date"] = args.until
    if args.topic:
        kw["reply_to"] = args.topic

    found = 0
    async for msg in client.iter_messages(entity, **kw):
        if args.since and msg.date < args.since:
            break
        kind = media_kind(msg)
        if kind is None or kind not in args.types:
            continue
        if not size_ok((msg.file.size if msg.file else 0) or 0, kind, args):
            continue
        yield msg, kind
        found += 1
        if args.limit and found >= args.limit:
            break


async def download_one(client, entity, msg, kind, out_dir: Path, by_type: bool, prog: Progress):
    folder = out_dir / kind if by_type else out_dir
    name = target_name(msg, kind)
    final = folder / name
    if final.exists():
        prog.skipped += 1
        return

    folder.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    expected = (msg.file.size if msg.file else 0) or 0
    got = 0

    def on_progress(cur, tot):
        nonlocal got
        prog.bytes += cur - got
        got = cur
        prog.active[name] = (cur, tot or expected)

    try:
        for attempt in range(1, MAX_RETRIES + 1):
            prog.bytes -= got  # don't count bytes from a failed attempt
            got = 0
            prog.active[name] = (0, expected)
            try:
                with open(part, "wb") as fh:
                    result = await client.download_media(msg, file=fh, progress_callback=on_progress)
                if result is None:
                    raise RuntimeError("message has no downloadable media")
                break
            except errors.FloodWaitError as e:
                prog.log(f"  ⏳ FloodWait {e.seconds}s ({name})")
                await asyncio.sleep(e.seconds + 1)
            except errors.FileReferenceExpiredError:
                fresh = await client.get_messages(entity, ids=msg.id)
                if fresh is None:
                    raise RuntimeError("message was deleted")
                msg = fresh
            except Exception as e:
                if attempt == MAX_RETRIES:
                    raise
                prog.log(f"  ! {name}: {e!r} — retry {attempt}/{MAX_RETRIES - 1}")
                await asyncio.sleep(2 ** attempt)
        else:
            raise RuntimeError("out of retries")

        os.replace(part, final)
        ts = msg.date.timestamp()
        os.utime(final, (ts, ts))
        prog.done += 1
        prog.log(f"✓ {final.relative_to(out_dir)} ({human(final.stat().st_size)})")
    except Exception as e:
        prog.bytes -= got
        prog.failed.append(f"{name}: {e}")
        prog.log(f"✗ {name}: {e}")
    finally:
        prog.active.pop(name, None)
        part.unlink(missing_ok=True)


async def worker(client, entity, queue: asyncio.Queue, out_dir: Path, by_type: bool, prog: Progress):
    while (item := await queue.get()) is not None:
        msg, kind = item
        await download_one(client, entity, msg, kind, out_dir, by_type, prog)


async def run(args) -> None:
    api_id, api_hash = get_credentials()
    session = str(SCRIPT_DIR / args.session)

    async with TelegramClient(session, api_id, api_hash) as client:
        if args.list_chats:
            await list_chats(client)
            return

        entity = await resolve_chat(client, args.chat)
        title = utils.get_display_name(entity) or str(entity.id)
        out_dir = Path(args.out) if args.out else Path("downloads") / safe_name(title)
        print(f"Chat: {title}  →  {out_dir}\nTypes: {', '.join(sorted(args.types))}")

        if args.dry_run:
            count = total = 0
            async for msg, kind in iter_media(client, entity, args):
                size = (msg.file.size if msg.file else 0) or 0
                count, total = count + 1, total + size
                print(f"{msg.date.astimezone():%Y-%m-%d %H:%M}  {kind:<8} {human(size):>10}  "
                      f"{target_name(msg, kind)}")
            print(f"\nTotal: {count} files, {human(total)}")
            return

        prog = Progress(live=sys.stdout.isatty())
        queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 2)
        workers = [asyncio.create_task(worker(client, entity, queue, out_dir, args.by_type, prog))
                   for _ in range(args.concurrency)]
        ticker = asyncio.create_task(prog.ticker()) if prog.live else None

        async for item in iter_media(client, entity, args):
            await queue.put(item)
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)

        if ticker:
            ticker.cancel()
        prog.summary(out_dir)


# ---------- CLI ----------

def parse_types(value: str) -> set[str]:
    if value == "all":
        return set(KINDS)
    types = {t.strip() for t in value.split(",") if t.strip()}
    bad = types - set(KINDS)
    if bad:
        raise argparse.ArgumentTypeError(f"unknown types: {', '.join(sorted(bad))}")
    return types


_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([KMG]?B?)", re.I)
_SIZE_UNITS = {"": 1024 ** 2, "B": 1, "K": 1024, "KB": 1024,
               "M": 1024 ** 2, "MB": 1024 ** 2, "G": 1024 ** 3, "GB": 1024 ** 3}


def parse_size_limit(value: str) -> tuple[str, int]:
    """'100' → ('*', 100 MB), 'voice=5' → ('voice', 5 MB), '1.5G', '500K'."""
    kind, _, size = value.rpartition("=")
    kind = kind.strip() or "*"
    if kind != "*" and kind not in KINDS:
        raise argparse.ArgumentTypeError(f"unknown type: {kind}")
    m = _SIZE_RE.fullmatch(size.strip())
    if not m:
        raise argparse.ArgumentTypeError("size: 50 (MB), 500K, 1.5G or type=size")
    num, unit = m.groups()
    return kind, int(float(num.replace(",", ".")) * _SIZE_UNITS[unit.upper()])


def parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").astimezone()  # local timezone
    except ValueError:
        raise argparse.ArgumentTypeError("date format: YYYY-MM-DD")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download files from a Telegram chat or channel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:" + __doc__.split("Examples:", 1)[1],
    )
    p.add_argument("chat", nargs="?", help="@username, t.me/… link, numeric id or me")
    p.add_argument("--list-chats", action="store_true", help="list your chats with their ids")
    p.add_argument("-o", "--out", help="output folder (default: downloads/<chat title>)")
    p.add_argument("-t", "--types", type=parse_types, default=set(DEFAULT_KINDS),
                   help=f"comma-separated from: {', '.join(KINDS)} or all "
                        f"(default: everything except sticker)")
    p.add_argument("--max-size", action="append", type=parse_size_limit, default=[],
                   metavar="[TYPE=]SIZE",
                   help="skip files larger than SIZE: 100 (MB), 500K, 1.5G. "
                        "Can be set per type and repeated: --max-size 200 --max-size voice=5")
    p.add_argument("--min-size", action="append", type=parse_size_limit, default=[],
                   metavar="[TYPE=]SIZE", help="skip files smaller than SIZE, same format")
    p.add_argument("-n", "--limit", type=int, help="max number of files (newest first)")
    p.add_argument("--since", type=parse_date, help="not older than YYYY-MM-DD")
    p.add_argument("--until", type=parse_date, help="not newer than YYYY-MM-DD (inclusive)")
    p.add_argument("--min-id", type=int, help="only messages with id > MIN_ID")
    p.add_argument("--max-id", type=int, help="only messages with id < MAX_ID")
    p.add_argument("--topic", type=int, help="topic id in a forum group")
    p.add_argument("--by-type", action="store_true", help="sort into subfolders photo/, video/, …")
    p.add_argument("-j", "--concurrency", type=int, default=3, help="parallel downloads (default: 3)")
    p.add_argument("--dry-run", action="store_true", help="only list what would be downloaded")
    p.add_argument("--session", default="tg_downloader", help="session file name next to the script")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.list_chats and not args.chat:
        parser.error("specify a chat or --list-chats")
    args.concurrency = max(1, min(args.concurrency, 10))
    args.max_size, args.min_size = dict(args.max_size), dict(args.min_size)
    if args.until:
        args.until += timedelta(days=1)  # offset_date is exclusive — make the date inclusive

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Files already downloaded will be skipped on the next run.")
    except (errors.RPCError, ValueError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
