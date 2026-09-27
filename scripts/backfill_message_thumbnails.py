#!/usr/bin/env python3
import argparse
import logging
from pathlib import Path
import sys

from PIL import Image, ImageSequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from igngbot_v3.config import Config


logger = logging.getLogger("backfill_message_thumbnails")
THUMB_MAX_DIMENSION = 360
THUMB_QUALITY = 30


def make_thumbnail(source, target):
    with Image.open(source) as image:
        frame = next(ImageSequence.Iterator(image)).convert("RGBA")
        ratio = min(1.0, THUMB_MAX_DIMENSION / max(frame.width, frame.height))
        size = (max(1, int(frame.width * ratio)), max(1, int(frame.height * ratio)))
        if size != frame.size:
            frame = frame.resize(size, Image.LANCZOS)
        frame.save(target, "WEBP", quality=THUMB_QUALITY, optimize=True)


def backfill(root, limit, dry_run):
    scanned = created = failed = 0
    for source in sorted(root.rglob("*.webp")):
        if source.stem.endswith("_thumb"):
            continue
        scanned += 1
        target = source.with_name(f"{source.stem}_thumb.webp")
        if target.exists():
            continue
        if limit and created >= limit:
            break
        if dry_run:
            logger.info("would create %s", target)
            created += 1
            continue
        try:
            make_thumbnail(source, target)
            created += 1
        except Exception:
            failed += 1
            logger.exception("thumbnail generation failed for %s", source)
    return {"scanned": scanned, "created": created, "failed": failed}


def main():
    parser = argparse.ArgumentParser(description="Generate missing QQ message thumbnails")
    parser.add_argument("--root", default=Config.NAS_MOUNT_PATH)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"media root does not exist: {root}")
    print(backfill(root, max(0, args.limit), args.dry_run))


if __name__ == "__main__":
    main()
