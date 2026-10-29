#!/usr/bin/env python3
"""Local load test for the Graphionica catalog gateway (never hits the upstream).

Every request goes to 127.0.0.1:<port>. The gateway is expected to be warm
(/health reports ok=true), so this measures the local gateway + cache layer, not
the upstream. Run benchmark.py for the upstream comparison.

Usage
  python stress_test.py                     # default scenarios, port 8960
  python stress_test.py --port 8960 --json stress_results.json
  python stress_test.py --scenario health --concurrency 32 --requests 400
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

DEFAULT_PORT = 8960


@dataclass
class Result:
    name: str
    ok: int = 0
    failed: int = 0
    latencies: list[float] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    wall_s: float = 0.0

    def add(self, elapsed_ms: float, error: str | None) -> None:
        if error:
            self.failed += 1
            self.errors[error] = self.errors.get(error, 0) + 1
        else:
            self.ok += 1
            self.latencies.append(elapsed_ms)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[index]


def run_scenario(
    name: str,
    fn: Callable[[requests.Session, int], tuple[int, str | None]],
    concurrency: int,
    requests_total: int,
) -> Result:
    result = Result(name)
    counter = {"next": 0}
    lock = threading.Lock()
    local = threading.local()

    def session() -> requests.Session:
        if not hasattr(local, "s"):
            local.s = requests.Session()
        return local.s

    def worker() -> None:
        while True:
            with lock:
                if counter["next"] >= requests_total:
                    return
                index = counter["next"]
                counter["next"] += 1
            started = time.perf_counter()
            try:
                status, err = fn(session(), index)
                if err == "__expected__":
                    err = None
                elif err is None and status >= 400:
                    err = f"HTTP {status}"
            except Exception as exc:  # noqa: BLE001 - load test records any failure
                err = type(exc).__name__
            elapsed = (time.perf_counter() - started) * 1000
            with lock:
                result.add(elapsed, err)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    result.wall_s = time.perf_counter() - started
    return result


def build_scenarios(base: str) -> dict[str, Callable[[requests.Session, int], tuple[int, str | None]]]:
    def health(s: requests.Session, i: int):
        r = s.get(f"{base}/health", timeout=10)
        return r.status_code, None

    def summary(s: requests.Session, i: int):
        r = s.get(f"{base}/catalog/summary", timeout=20)
        return r.status_code, None

    def stickers_page(s: requests.Session, i: int):
        page = 1 + (i % 20)
        r = s.get(f"{base}/catalog/stickers", params={"page": page, "size": 60}, timeout=20)
        return r.status_code, None

    def templates_page(s: requests.Session, i: int):
        page = 1 + (i % 20)
        r = s.get(f"{base}/catalog/templates", params={"page": page, "size": 24}, timeout=20)
        return r.status_code, None

    def passthrough_home(s: requests.Session, i: int):
        # client_version=3 == current catalog -> gateway answers the incremental no-op
        r = s.post(f"{base}/api/v2/home", data={"version": 3, "platform": "android"}, timeout=20)
        return r.status_code, None

    def asset_hot(s: requests.Session, i: int):
        paths = [
            "s/1_flowers/1.webp",
            "s/1_flowers/preview/1.webp",
            "s/1_flowers/2.webp",
            "t/1_base/1.webp",
        ]
        r = s.get(f"{base}/asset/{paths[i % len(paths)]}", timeout=30)
        return r.status_code, None

    def error_pack(s: requests.Session, i: int):
        r = s.get(f"{base}/catalog/stickers", params={"pack": "NaN"}, timeout=20)
        if r.status_code == 400:
            return r.status_code, "__expected__"
        return r.status_code, "expected 400"

    def error_version(s: requests.Session, i: int):
        r = s.post(f"{base}/api/v2/stickers", data={"version": "abc"}, timeout=20)
        if r.status_code == 400:
            return r.status_code, "__expected__"
        return r.status_code, "expected 400"

    def error_asset(s: requests.Session, i: int):
        r = s.get(f"{base}/asset/s/__no_such_pack__/1.webp", timeout=60)
        if r.status_code == 404:
            return r.status_code, "__expected__"
        return r.status_code, f"expected 404 got {r.status_code}"

    return {
        "health": health,
        "catalog_summary": summary,
        "stickers_page": stickers_page,
        "templates_page": templates_page,
        "passthrough_home_incremental": passthrough_home,
        "asset_hot": asset_hot,
        "error_pack": error_pack,
        "error_version": error_version,
        "error_asset_missing": error_asset,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--scenario", action="append", help="limit to specific scenario(s)")
    parser.add_argument("--concurrency", type=int, default=0, help="override concurrency for all")
    parser.add_argument("--requests", type=int, default=0, help="override request count for all")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"
    try:
        warm = requests.get(f"{base}/health", timeout=10).json()
    except requests.RequestException as exc:
        print(f"gateway unreachable at {base}: {exc}", file=sys.stderr)
        return 2
    print(f"gateway ok={warm.get('ok')} cache={warm.get('cache_age_s')}\n")
    if not warm.get("ok"):
        print("warning: cache still warming; cold asset rows will be slow", file=sys.stderr)

    scenarios = build_scenarios(base)
    plan = [
        ("health", 16, 400),
        ("catalog_summary", 16, 300),
        ("stickers_page", 8, 200),
        ("templates_page", 8, 200),
        ("passthrough_home_incremental", 16, 300),
        ("asset_hot", 8, 120),
        ("error_pack", 4, 60),
        ("error_version", 4, 60),
        ("error_asset_missing", 2, 4),
    ]

    if args.scenario:
        wanted = set(args.scenario)
        missing = wanted - set(scenarios)
        if missing:
            print(f"unknown scenario(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
        plan = [row for row in plan if row[0] in wanted]

    results: list[Result] = []
    for name, concurrency, requests_total in plan:
        if args.concurrency:
            concurrency = args.concurrency
        if args.requests:
            requests_total = args.requests
        fn = scenarios[name]
        result = run_scenario(name, fn, concurrency, requests_total)
        results.append(result)
        rps = (result.ok + result.failed) / result.wall_s if result.wall_s else 0.0
        print(
            f"{name:32s} c={concurrency:<3d} n={requests_total:<4d} "
            f"ok={result.ok:<4d} fail={result.failed:<3d} rps={rps:7.1f} "
            f"p50={percentile(result.latencies, 50):7.1f}ms "
            f"p95={percentile(result.latencies, 95):7.1f}ms "
            f"p99={percentile(result.latencies, 99):7.1f}ms"
            + (f" errors={result.errors}" if result.errors else "")
        )

    total_ok = sum(r.ok for r in results)
    total_fail = sum(r.failed for r in results)
    print(f"\ntotal ok={total_ok} fail={total_fail}")

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "target": base,
        "gateway": warm,
        "scenarios": [
            {
                "name": r.name,
                "ok": r.ok,
                "failed": r.failed,
                "wall_s": round(r.wall_s, 3),
                "rps": round((r.ok + r.failed) / r.wall_s, 2) if r.wall_s else 0.0,
                "mean_ms": round(statistics.fmean(r.latencies), 2) if r.latencies else 0.0,
                "p50_ms": round(percentile(r.latencies, 50), 2),
                "p90_ms": round(percentile(r.latencies, 90), 2),
                "p95_ms": round(percentile(r.latencies, 95), 2),
                "p99_ms": round(percentile(r.latencies, 99), 2),
                "max_ms": round(max(r.latencies), 2) if r.latencies else 0.0,
                "errors": r.errors,
            }
            for r in results
        ],
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"written {args.json}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
