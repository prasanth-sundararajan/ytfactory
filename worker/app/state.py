import datetime
import json
import os

from app.config import CFG
from app.db import board


def write_state() -> dict:
    rows = board()
    now = datetime.datetime.now().isoformat(timespec="seconds")
    st = {"updated": now, "agents": {}}
    for r in rows:
        fin = r["finished_at"]
        dur = r["duration_s"]
        st["agents"][r["id"]] = {
            "status": r["status"],
            "last_run": fin.isoformat(timespec="seconds") if fin else None,
            "duration_s": float(dur) if dur is not None else None,
            "note": "",
        }
    tmp = CFG.state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, CFG.state_path)
    return st
