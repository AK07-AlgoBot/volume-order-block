from datetime import datetime
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT = ROOT / "archive"

FILES_TO_ARCHIVE = [
    "trading_bot.log",
    "orders.log",
    "market_status.log",
    "bot_output.txt",
    "trading_state.json",
]

DIRS_TO_ARCHIVE = [
    "__pycache__",
]


def move_item(src: Path, dst_dir: Path):
    dst = dst_dir / src.name
    if dst.exists():
        stamp = datetime.now().strftime("%H%M%S")
        dst = dst_dir / f"{src.stem}_{stamp}{src.suffix}"

    shutil.move(str(src), str(dst))
    return dst


def main():
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_archive_dir = ARCHIVE_ROOT / timestamp
    logs_dir = run_archive_dir / "logs"
    state_dir = run_archive_dir / "state"
    cache_dir = run_archive_dir / "cache"

    moved = []

    logs_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    for name in FILES_TO_ARCHIVE:
        src = ROOT / name
        if not src.exists():
            continue

        target_dir = state_dir if src.name == "trading_state.json" else logs_dir
        dst = move_item(src, target_dir)
        moved.append((src.name, dst))

    for name in DIRS_TO_ARCHIVE:
        src = ROOT / name
        if not src.exists():
            continue
        dst = move_item(src, cache_dir)
        moved.append((src.name, dst))

    print("=" * 80)
    print("DAY ARCHIVE SUMMARY")
    print("=" * 80)
    print(f"Archive folder: {run_archive_dir}")

    if moved:
        print("Moved items:")
        for original, destination in moved:
            rel = destination.relative_to(ROOT)
            print(f"- {original} -> {rel}")
    else:
        print("No matching runtime artifacts found to archive.")

    print("\nWorkspace is clean for next run.")
    print("=" * 80)


if __name__ == "__main__":
    main()
