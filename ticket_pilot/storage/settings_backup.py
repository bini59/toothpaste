"""Write the settings export used by legacy imports and normal settings saves."""

import json
from datetime import datetime, timezone
from pathlib import Path

BACKUP_KEEP = 50


def backup_dir(db_path: Path) -> Path:
    return db_path.parent / "backups"


def write_backup(doc: dict, directory: Path, keep: int = BACKUP_KEEP) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"config-{stamp}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    for old in sorted(directory.glob("config-*.json"))[:-keep]:
        old.unlink(missing_ok=True)
    return path
