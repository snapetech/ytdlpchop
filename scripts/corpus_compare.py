#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

import acoustid


def read_fp(path: Path) -> tuple[float, bytes]:
    duration = None
    fingerprint = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DURATION="):
            duration = float(line.split("=", 1)[1])
        if line.startswith("FINGERPRINT="):
            fingerprint = line.split("=", 1)[1].encode("ascii")
    if duration is None or fingerprint is None:
        raise ValueError(f"missing duration or fingerprint in {path}")
    return duration, fingerprint


def load_meta(fp_path: Path) -> tuple[str, str]:
    meta_path = fp_path.with_suffix("").with_suffix(".meta.json")
    label = fp_path.stem.replace(".fp", "")
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            label = data.get("label", label)
        except json.JSONDecodeError:
            pass
    return label, str(meta_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    query_path = Path(args.query)
    corpus_dir = Path(args.corpus_dir)
    query_fp = read_fp(query_path)

    rows = []
    for fp_path in sorted(corpus_dir.glob("*.fp.txt")):
        try:
            candidate_fp = read_fp(fp_path)
            score = acoustid.compare_fingerprints(query_fp, candidate_fp)
        except Exception:
            continue
        label, meta_path = load_meta(fp_path)
        rows.append((score, label, str(fp_path), meta_path))

    rows.sort(key=lambda row: row[0], reverse=True)
    for score, label, fp_path, meta_path in rows[: args.top]:
      print(f"{score:.6f}\t{label}\t{fp_path}\t{meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
