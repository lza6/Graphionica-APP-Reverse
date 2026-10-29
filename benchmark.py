#!/usr/bin/env python3
"""Benchmark: upstream直连 vs 本地网关（缓存前/后）+ 并发吞吐曲线.

Every scenario is executed ``--rounds`` times (default 3) and reported as
median/min/max. Direct-upstream rows are deliberately few because the upstream
/templates endpoint regularly needs 15-25s and there is no point hammering it.

Usage
  python benchmark.py --port 8960 --rounds 3
  python benchmark.py --skip-upstream        # gateway only, no upstream traffic
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Callable

import requests

import graphionica_client as gc

BASE_DIR = Path(__file__).parent / "benchmark_data"
BASE_DIR.mkdir(exist_ok=True)


def timeit(fn: Callable[[], object], rounds: int) -> dict[str, float]:
    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "median_ms": round(statistics.median(samples), 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        "samples_ms": [round(s, 1) for s in samples],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8960)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--skip-upstream", action="store_true")
    parser.add_argument("--json", default="benchmark_results.json")
    parser.add_argument("--md", default="benchmark_results.md")
    args = parser.parse_args()

    gw = f"http://127.0.0.1:{args.port}"
    client = gc.GraphionicaClient()
    results: list[dict[str, object]] = []

    def record(label: str, group: str, stats: dict[str, float], note: str = "") -> None:
        row = {"group": group, "label": label, **stats, "note": note}
        results.append(row)
        print(
            f"{group:12s} {label:42s} median={stats['median_ms']:8.1f}ms "
            f"min={stats['min_ms']:7.1f} max={stats['max_ms']:7.1f}"
            + (f"  {note}" if note else "")
        )

    # ---------------------------------------------------------------- gateway
    hits = {
        "gateway /health": lambda: requests.get(f"{gw}/health", timeout=10),
        "gateway /catalog/summary": lambda: requests.get(f"{gw}/catalog/summary", timeout=20),
        "gateway /catalog/stickers (p1,60)": lambda: requests.get(
            f"{gw}/catalog/stickers", params={"page": 1, "size": 60}, timeout=20
        ),
        "gateway /catalog/templates (p1,24)": lambda: requests.get(
            f"{gw}/catalog/templates", params={"page": 1, "size": 24}, timeout=20
        ),
        "gateway /api/v2/home (incremental)": lambda: requests.post(
            f"{gw}/api/v2/home", data={"version": 3}, timeout=20
        ),
        "gateway /export/manifest.json": lambda: requests.get(f"{gw}/export/manifest.json", timeout=30),
    }
    for label, fn in hits.items():
        record(label, "cache-hit", timeit(fn, args.rounds))

    # asset: force a cold copy by removing the cached file first
    asset_rel = "s/1_flowers/preview/1.webp"
    cached = BASE_DIR.parent / "assets" / asset_rel
    label = f"gateway /asset/{asset_rel}"
    if cached.exists():
        cached.unlink()
    stats = timeit(lambda: requests.get(f"{gw}/asset/{asset_rel}", timeout=60), 1)
    record(label, "asset-cold", stats, "本地无缓存 → 回源 CDN")
    stats = timeit(lambda: requests.get(f"{gw}/asset/{asset_rel}", timeout=30), args.rounds)
    record(label, "asset-warm", stats, "本地磁盘命中")

    # ----------------------------------------------------------------- upstream
    if not args.skip_upstream:
        record("upstream POST /home", "upstream", timeit(lambda: client.home(), args.rounds))
        record("upstream POST /stickers", "upstream", timeit(lambda: client.stickers(), args.rounds))
        record(
            "upstream POST /templates",
            "upstream",
            timeit(lambda: client.templates(stickers_version=211), args.rounds),
            "上游最慢端点",
        )
        envelope = client.stickers()
        record(
            "upstream CDN sticker (203KB)",
            "upstream",
            timeit(lambda: client.download_asset(envelope, "1_flowers/1.webp"), args.rounds),
        )

    # ------------------------------------------------------- concurrency ramp
    import stress_test as st  # local module

    scenarios = st.build_scenarios(gw)
    ramp = []
    for concurrency in (1, 4, 8, 16):
        result = st.run_scenario("stickers_page", scenarios["stickers_page"], concurrency, 120)
        rps = (result.ok + result.failed) / result.wall_s if result.wall_s else 0.0
        row = {
            "concurrency": concurrency,
            "requests": 120,
            "rps": round(rps, 1),
            "p50_ms": round(st.percentile(result.latencies, 50), 1),
            "p95_ms": round(st.percentile(result.latencies, 95), 1),
            "failed": result.failed,
        }
        ramp.append(row)
        print(f"ramp         /catalog/stickers c={concurrency:<2d} rps={row['rps']:7.1f} p95={row['p95_ms']:7.1f}ms")

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gateway": gw,
        "scenarios": results,
        "concurrency_ramp": ramp,
    }
    Path(args.json).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        "# Graphionica 4.6.1 gateway benchmark",
        "",
        f"- 生成时间: {payload['generated_at']}",
        f"- 网关: {gw}",
        f"- rounds: {args.rounds}",
        "",
        "## 单请求延迟",
        "",
        "| 分组 | 场景 | median | min | max | 备注 |",
        "|------|------|--------|-----|-----|------|",
    ]
    for row in results:
        lines.append(
            f"| {row['group']} | {row['label']} | {row['median_ms']}ms | {row['min_ms']}ms | "
            f"{row['max_ms']}ms | {row['note']} |"
        )
    lines += [
        "",
        "## 并发曲线 (`/catalog/stickers`, 120 请求)",
        "",
        "| 并发 | RPS | p50 | p95 | 失败 |",
        "|------|-----|-----|-----|------|",
    ]
    for row in ramp:
        lines.append(
            f"| {row['concurrency']} | {row['rps']} | {row['p50_ms']}ms | {row['p95_ms']}ms | {row['failed']} |"
        )
    Path(args.md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten {args.json} and {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
