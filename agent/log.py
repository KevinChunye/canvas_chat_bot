"""JSONL log, one file per cycle. Every string is scrubbed of secrets and
secret-shaped tokens before it is written or echoed."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import secret_values
from .text import scrub


def _scrub_value(value, secrets):
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, dict):
        return {str(k): _scrub_value(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_scrub_value(v, secrets) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub(str(value), secrets)


class CycleLog:
    def __init__(self, log_dir: Path | None, name: str, echo: bool = True):
        self.path = None
        if log_dir is not None:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            self.path = Path(log_dir) / f"cycle-{name}.jsonl"
        self.echo = echo
        self.cycle_id = name
        self.records = []

    def __call__(self, event: str, **fields) -> None:
        secrets = secret_values()
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "cycle_id": self.cycle_id,
                  "event": event}
        record.update(_scrub_value(fields, secrets))
        self.records.append(record)
        line = json.dumps(record, ensure_ascii=False)
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.echo:
            print(line, file=sys.stderr)
