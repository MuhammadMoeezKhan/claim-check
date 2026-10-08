"""Download Impossible-LiveCodeBench into one small JSON file (about 1 MB).

The dataset card states no license, so the data is not committed to this repo; run.py calls
this on first use and checks a content hash. Source:
https://huggingface.co/datasets/fjzzq2002/impossible_livecodebench (Zhong, Raghunathan,
Carlini, arXiv:2510.20270).
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

DATASET = "fjzzq2002/impossible_livecodebench"
OUT = Path(__file__).parent / "data" / "impossible_livecodebench.json"


def get(url: str):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def rows(split: str) -> list[dict]:
    out, offset = [], 0
    while True:
        page = get(f"https://datasets-server.huggingface.co/rows?dataset={DATASET}"
                   f"&config=default&split={split}&offset={offset}&length=100")
        batch = page["rows"]
        if any(r["truncated_cells"] for r in batch):
            raise RuntimeError("the rows API truncated a cell; download the parquet files instead")
        out += [r["row"] for r in batch]
        offset += len(batch)
        if len(batch) < 100:
            return out


def main() -> None:
    sha = get(f"https://huggingface.co/api/datasets/{DATASET}")["sha"]
    splits = {s: {r["task_id"]: r for r in rows(s)} for s in ("original", "oneoff", "conflicting")}
    tasks = []
    for tid, orig in splits["original"].items():
        tasks.append({"task_id": tid, "entry_point": orig["entry_point"], "prompt": orig["prompt"],
                      "original_test": orig["test"], "oneoff_test": splits["oneoff"][tid]["test"],
                      "conflicting_test": splits["conflicting"][tid]["test"]})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"source": f"https://huggingface.co/datasets/{DATASET}", "revision": sha,
                               "license": "not stated on the dataset card",
                               "citation": "Zhong, Raghunathan, Carlini. ImpossibleBench. arXiv:2510.20270",
                               "tasks": tasks}, indent=1) + "\n")
    print(f"wrote {len(tasks)} tasks from {DATASET}@{sha[:12]} to {OUT}")


if __name__ == "__main__":
    main()
