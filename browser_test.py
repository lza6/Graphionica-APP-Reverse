#!/usr/bin/env python3
"""Real-browser verification of the demo page (Playwright + Chromium).

Checks, in a real browser:
  * the summary cards render catalog counters
  * sticker tiles load actual pixels through /asset/... (naturalWidth > 0)
  * the templates tab renders tiles and layer/text metadata
  * the export view builds the manifest and enables the download link
  * a missing asset answers 404 through the proxy

Usage
  python browser_test.py --port 8960
Outputs demo_stickers.png / demo_templates.png / demo_export.png + browser_report.json
"""

from __future__ import annotations

import argparse
import json
import time

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8960)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    report: dict[str, object] = {"base": base, "checks": [], "console_errors": []}

    def check(name: str, ok: bool, detail: object = "") -> None:
        report["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.on("console", lambda msg: report["console_errors"].append(msg.text) if msg.type == "error" else None)
        page.goto(base, wait_until="domcontentloaded")

        page.wait_for_selector(".card", timeout=30_000)
        page.wait_for_function("document.querySelectorAll('.card').length >= 5", timeout=30_000)
        cards = page.eval_on_selector_all(".card", "els => els.map(e => e.innerText.replace(/\\n/g,' '))")
        check("summary cards rendered", len(cards) >= 5, cards[:3])

        page.wait_for_selector(".tile img", timeout=60_000)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('.tile img')).some(i => i.naturalWidth > 0)",
            timeout=60_000,
        )
        loaded = page.eval_on_selector_all(
            ".tile img", "els => els.filter(e => e.naturalWidth > 0).length"
        )
        total_stickers = len(page.query_selector_all(".tile img"))
        check("sticker tiles serve real pixels", loaded > 0, f"{loaded}/{total_stickers} loaded")
        page.screenshot(path="demo_stickers.png", full_page=False)

        first_src = page.eval_on_selector(".tile img", "e => e.currentSrc || e.src")
        check("sticker tile uses local proxy", "/asset/" in first_src, first_src)

        page.click('.tab[data-view="templates"]')
        page.wait_for_selector("#templateGrid .tile", timeout=60_000)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('#templateGrid img')).some(i => i.naturalWidth > 0)",
            timeout=60_000,
        )
        tpl_loaded = page.eval_on_selector_all(
            "#templateGrid img", "els => els.filter(e => e.naturalWidth > 0).length"
        )
        tpl_total = len(page.query_selector_all("#templateGrid .tile"))
        meta_text = page.eval_on_selector("#templateGrid .tile .texts", "e => e.innerText")
        check("template tiles serve real pixels", tpl_loaded > 0, f"{tpl_loaded}/{tpl_total} loaded")
        check("template layer metadata visible", "层" in meta_text, meta_text[:60])
        page.screenshot(path="demo_templates.png", full_page=False)

        page.click('.tab[data-view="export"]')
        page.click("#loadManifest")
        page.wait_for_function(
            "!document.getElementById('manifestOut').innerText.includes('生成中')",
            timeout=90_000,
        )
        manifest_text = page.inner_text("#manifestOut")
        link_visible = page.is_visible("#manifestLink")
        check("manifest generated", "total" in manifest_text, manifest_text.splitlines()[:2])
        check("manifest download link enabled", link_visible, page.inner_text("#manifestLink"))

        status_404 = page.evaluate(
            "async () => (await fetch('/asset/s/__no_such_pack__/1.webp')).status"
        )
        check("missing asset answers 404", status_404 == 404, status_404)

        page.screenshot(path="demo_export.png", full_page=False)
        browser.close()

    failures = [c for c in report["checks"] if not c["ok"]]
    report["ok"] = not failures
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open("browser_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"\n{len(report['checks']) - len(failures)}/{len(report['checks'])} checks passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
