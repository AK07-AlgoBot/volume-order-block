"""
Migrate archive folders under server/data/users.

  python scripts/migrate_archives.py central-to-users
    Moves server/data/users/<holder>/archive/<username>/<timestamp>/*
    -> server/data/users/<username>/archive/<timestamp>/
    Holder is checked as AK07 then legacy admin. Skips timestamp-named
    folders (normal per-user daily snapshots).

  python scripts/migrate_archives.py mirror-to-admin
    Copies each managed user's archive snapshots into
    server/data/users/AK07/archive/mirror/<username>/<timestamp>/
    (originals stay put; optional backup for the holder account.)
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USERS = ROOT / "server" / "data" / "users"
HOLDERS = ("AK07", "admin")
HOLDER_SKIP = frozenset(HOLDERS)
SKIP_TOP_LEVEL = frozenset({"mirror"})
TS_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}_")


def _holder_archive_roots() -> list[Path]:
    roots: list[Path] = []
    for name in HOLDERS:
        p = USERS / name / "archive"
        if p.is_dir():
            roots.append(p)
    return roots


def central_to_users() -> int:
    roots = _holder_archive_roots()
    if not roots:
        print("Nothing to do: no AK07/admin archive directory.")
        return 0
    moved = 0
    for ADMIN_ARCHIVE in roots:
        for bucket in sorted(ADMIN_ARCHIVE.iterdir()):
            if not bucket.is_dir() or bucket.name in SKIP_TOP_LEVEL:
                continue
            if TS_DIR.match(bucket.name):
                continue
            username = bucket.name
            dest_root = USERS / username
            if not dest_root.is_dir():
                print(f"Skip {bucket.name}: no users/{username}/ directory")
                continue
            dest_archive = dest_root / "archive"
            dest_archive.mkdir(parents=True, exist_ok=True)
            for snap in sorted(bucket.iterdir()):
                if not snap.is_dir():
                    continue
                target = dest_archive / snap.name
                if target.exists():
                    target = dest_archive / f"{snap.name}_from_holder_bucket"
                shutil.move(str(snap), str(target))
                print(f"Moved {snap} -> {target}")
                moved += 1
            try:
                bucket.rmdir()
            except OSError:
                print(f"Note: {bucket} not empty; remove leftovers manually.")
    print(f"Done. Moved {moved} snapshot folder(s).")
    return 0


def mirror_to_admin() -> int:
    holder = USERS / "AK07"
    holder.mkdir(parents=True, exist_ok=True)
    mirror_root = holder / "archive" / "mirror"
    copied = 0
    for user_dir in sorted(USERS.iterdir()):
        if not user_dir.is_dir() or user_dir.name in HOLDER_SKIP:
            continue
        src_ar = user_dir / "archive"
        if not src_ar.is_dir():
            continue
        for snap in sorted(src_ar.iterdir()):
            if not snap.is_dir():
                continue
            dst = mirror_root / user_dir.name / snap.name
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(snap, dst)
            print(f"Copied {snap} -> {dst}")
            copied += 1
    print(f"Done. Copied {copied} snapshot folder(s) under AK07/archive/mirror/.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive layout migrations")
    parser.add_argument(
        "command",
        choices=("central-to-users", "mirror-to-admin"),
        help="central-to-users: move holder/archive/<user>/… to <user>/archive/; "
        "mirror-to-admin: copy each user's archive into AK07/archive/mirror/",
    )
    args = parser.parse_args()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if args.command == "central-to-users":
        return central_to_users()
    return mirror_to_admin()


if __name__ == "__main__":
    raise SystemExit(main())
