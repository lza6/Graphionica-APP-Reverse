#!/usr/bin/env python3
"""Harvest the whole Graphionica creative library (stickers + story templates).

Resumable by design: an existing non-empty file is skipped unless --force is given,
so an interrupted run can simply be restarted.

Usage
  python harvest.py --limit 40                  # smoke run: 40 assets
  python harvest.py                             # everything (~4.5k files, several hundred MB)
  python harvest.py --kind s --concurrency 12   # stickers only, 12 workers
  python harvest.py --out D:\\graphionica-assets --force
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import Counter
from pathlib import Path

import graphionica_client as gc

DONE = threading.Lock()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="assets")
    parser.add_argument("--kind", action="append", choices=["s", "t", "b"], help="default: all")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    parser.add_argument("--json", default="harvest_report.json")
    args = parser.parse_args()

    out_dir = Path(args.out)
    client = gc.GraphionicaClient()

    print("[1/3] pulling catalogs…", flush=True)
    home = client.home()
    stickers = client.stickers()
    templates = client.templates(stickers_version=stickers.version)
    print(
        f"      home v{home.version} / stickers v{stickers.version} / templates v{templates.version}",
        flush=True,
    )

    wanted: dict[str, str] = {}
    for envelope in (home, stickers, templates):
        for path, kind in gc.GraphionicaClient.asset_paths(envelope):
            if args.kind and kind not in args.kind:
                continue
            wanted.setdefault(f"{kind}/{path}", kind)
    entries = sorted(wanted.items())
    if args.limit:
        entries = entries[: args.limit]
    print(f"[2/3] {len(entries)} assets queued ({args.concurrency} workers)", flush=True)

    # envelopes per kind so mirror fallback uses the right prefix
    envelopes = {"s": stickers, "t": templates, "b": home}

    counters = Counter()
    bytes_total = {"value": 0}
    failures: list[dict[str, str]] = []
    started = time.perf_counter()
    index = {"next": 0}
    stop = threading.Event()

    def worker() -> None:
        local_session = __import__("requests").Session()
        while not stop.is_set():
            with DONE:
                if index["next"] >= len(entries):
                    return
                key, kind = entries[index["next"]]
                index["next"] += 1
            rel = key.split("/", 1)[1]
            target = out_dir / kind / gc.GraphionicaClient.asset_key(rel)
            if target.exists() and target.stat().st_size and not args.force:
                counters["skipped"] += 1
                continue
            try:
                body, used = client.download_asset(envelopes[kind], rel, timeout=60)
            except gc.AssetNotFound:
                counters["missing"] += 1
                failures.append({"asset": key, "reason": "404 on both mirrors"})
                continue
            except gc.GraphionicaError as exc:
                counters["failed"] += 1
                failures.append({"asset": key, "reason": str(exc)[:200]})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(body)
            except OSError as exc:
                counters["failed"] += 1
                failures.append({"asset": key, "reason": f"write failed: {exc}"})
                continue
            with DONE:
                counters["downloaded"] += 1
                bytes_total["value"] += len(body)
                done = counters["downloaded"] + counters["skipped"] + counters["missing"] + counters["failed"]
                if done % 25 == 0:
                    mb = bytes_total["value"] / 1048576
                    rate = mb / max(0.001, time.perf_counter() - started)
                    print(
                        f"      {done}/{len(entries)}  {mb:.1f}MB  {rate:.2f}MB/s  "
                        f"fail={counters['failed']} missing={counters['missing']}",
                        flush=True,
                    )

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    wall = time.perf_counter() - started
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "catalogs": {"home": home.version, "stickers": stickers.version, "templates": templates.version},
        "queued": len(entries),
        "downloaded": counters["downloaded"],
        "skipped": counters["skipped"],
        "missing_404": counters["missing"],
        "failed": counters["failed"],
        "bytes": bytes_total["value"],
        "megabytes": round(bytes_total["value"] / 1048576, 1),
        "wall_s": round(wall, 1),
        "throughput_mb_s": round(bytes_total["value"] / 1048576 / max(0.001, wall), 2),
        "failures": failures[:50],
    }
    Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"[3/3] downloaded={counters['downloaded']} skipped={counters['skipped']} "
        f"missing={counters['missing']} failed={counters['failed']} "
        f"{report['megabytes']}MB in {report['wall_s']}s ({report['throughput_mb_s']}MB/s)"
    )
    print(f"      report -> {args.json}")
    return 0 if counters["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
