"""Weekly archive of handoff documents.

Copies HANDOFF_ARCHITECT_latest.md and HANDOFF_CODER_latest.md
to dated snapshots (YYYYMMDD). Idempotent — skips if today's
archive already exists. Never raises.
"""

import shutil
from datetime import datetime
from pathlib import Path


HANDOFFS_DIR = Path(r"C:\AGT_Telegram_Bridge\reports\handoffs")

LATEST_FILES = [
    "HANDOFF_ARCHITECT_latest.md",
    "HANDOFF_CODER_latest.md",
]


def archive_handoffs() -> None:
    today = datetime.utcnow().strftime("%Y%m%d")

    for latest_name in LATEST_FILES:
        src = HANDOFFS_DIR / latest_name
        dated_name = latest_name.replace("_latest.md", f"_{today}.md")
        dst = HANDOFFS_DIR / dated_name

        if dst.exists():
            print(f"SKIP  {dated_name} — already exists")
            continue

        if not src.exists():
            print(f"SKIP  {latest_name} — source not found")
            continue

        shutil.copy2(src, dst)
        print(f"ARCHIVED  {latest_name} -> {dated_name}")


if __name__ == "__main__":
    try:
        archive_handoffs()
    except Exception as exc:
        print(f"archive_handoffs failed: {exc}")
