#!/usr/bin/env python3
"""Graphionica 4.6.1 catalog gateway + demo page.

The app's entire remote surface is three unauthenticated catalog endpoints plus a
CDN. This gateway re-publishes them with the things the upstream lacks:

  * TTL cache + single-flight (upstream /templates regularly takes 15-25s)
  * asset proxy with on-disk cache and DigitalOcean -> Yandex mirror fallback
  * a browsable catalog (packs, stickers, story templates with layer JSON)
  * a full asset manifest so the whole creative library can be harvested offline

Endpoints
  GET  /                          demo page
  GET  /health                    gateway + upstream status
  GET  /stats                     cache / proxy counters
  POST /api/v2/home               raw passthrough (form body, honouring version)
  POST /api/v2/stickers           raw passthrough
  POST /api/v2/templates          raw passthrough
  GET  /catalog/summary           counters for the whole catalog
  GET  /catalog/stickers          paged sticker list (?page&size&pack&include_premium)
  GET  /catalog/templates         paged template list (?page&size&pack)
  GET  /export/manifest.json      every referenced asset with absolute URLs + sizes
  GET  /asset/<kind>/<path>       asset proxy/cache (kind = s | t | b)

Env
  GRAPHIONICA_PORT      default 8940
  GRAPHIONICA_HOST      default 0.0.0.0
  GRAPHIONICA_TTL       catalog cache TTL seconds, default 600
  GRAPHIONICA_LANG      default en
  GRAPHIONICA_COUNTRY   default US
  GRAPHIONICA_PROXY     optional http(s) proxy for upstream calls
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
from graphionica_client import (  # noqa: E402
    AssetNotFound,
    GraphionicaClient,
    GraphionicaError,
    summarize,
)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
CACHE_DIR = BASE_DIR / "cache"
ASSET_DIR = BASE_DIR / "assets"
for directory in (STATIC_DIR, CACHE_DIR, ASSET_DIR):
    directory.mkdir(exist_ok=True)

PORT = int(os.environ.get("GRAPHIONICA_PORT", "8960"))
HOST = os.environ.get("GRAPHIONICA_HOST", "0.0.0.0")
TTL = float(os.environ.get("GRAPHIONICA_TTL", "600"))

app = Flask(__name__, static_folder=None)

_client = GraphionicaClient()
_lock = threading.Lock()
_flight: dict[str, threading.Lock] = {}
_state: dict[str, dict[str, Any]] = {
    "home": {"envelope": None, "fetched_at": 0.0},
    "stickers": {"envelope": None, "fetched_at": 0.0},
    "templates": {"envelope": None, "fetched_at": 0.0},
}
_counters = {
    "upstream_calls": 0,
    "cache_hits": 0,
    "cache_stale_served": 0,
    "upstream_failures": 0,
    "asset_hits": 0,
    "asset_misses": 0,
    "asset_bytes": 0,
    "asset_mirror_fallbacks": 0,
}


# --------------------------------------------------------------------- caching
def _cache_file(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def _load_disk_cache() -> None:
    for name, slot in _state.items():
        path = _cache_file(name)
        if not path.exists():
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            from graphionica_client import CatalogEnvelope

            slot["envelope"] = CatalogEnvelope.from_payload(blob["payload"])
            slot["fetched_at"] = float(blob.get("fetched_at") or 0.0)
        except (OSError, ValueError, KeyError):
            continue


def _store_disk_cache(name: str, envelope, fetched_at: float) -> None:
    try:
        _cache_file(name).write_text(
            json.dumps({"payload": envelope.raw, "fetched_at": fetched_at}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _fetch(name: str, force: bool = False):
    """Return a cached envelope, refreshing through a single-flight lock."""
    slot = _state[name]
    age = time.time() - slot["fetched_at"]
    if slot["envelope"] is not None and age < TTL and not force:
        _counters["cache_hits"] += 1
        return slot["envelope"]

    flight = _flight.setdefault(name, threading.Lock())
    with flight:
        # re-check: another thread may have refreshed while we waited
        slot = _state[name]
        age = time.time() - slot["fetched_at"]
        if slot["envelope"] is not None and age < TTL and not force:
            _counters["cache_hits"] += 1
            return slot["envelope"]
        try:
            if name == "home":
                envelope = _client.home()
            elif name == "stickers":
                envelope = _client.stickers()
            else:
                stickers_version = _state["stickers"]["envelope"].version if _state["stickers"]["envelope"] else 0
                envelope = _client.templates(stickers_version=stickers_version)
        except GraphionicaError:
            _counters["upstream_failures"] += 1
            if slot["envelope"] is not None:
                _counters["cache_stale_served"] += 1
                return slot["envelope"]
            raise
        _counters["upstream_calls"] += 1
        _state[name] = {"envelope": envelope, "fetched_at": time.time()}
        _store_disk_cache(name, envelope, _state[name]["fetched_at"])
        return envelope


def _warm() -> None:
    """Populate the cache in dependency order (templates need the sticker version)."""
    try:
        _fetch("home")
        _fetch("stickers")
        _fetch("templates")
    except GraphionicaError as exc:
        print(f"[warm] upstream unavailable: {exc}", flush=True)


# ------------------------------------------------------------------- passthrough
def _passthrough(name: str):
    form = request.form or request.get_json(silent=True) or {}
    try:
        client_version = int(form.get("version") or 0)
    except (TypeError, ValueError):
        abort(400, "version must be an integer")
    try:
        envelope = _fetch(name)
    except GraphionicaError as exc:
        return jsonify({"error": "upstream_unavailable", "detail": str(exc)}), 502

    # Honest incremental semantics, matching the upstream: isV is false only when the
    # caller already holds exactly the current catalog version.
    if client_version and client_version == envelope.version:
        body = {
            "u": envelope.cdn,
            "u2": envelope.cdn_mirror,
            "v": envelope.version,
            "isV": False,
        }
        if name == "home":
            body["b"] = []
        elif name == "stickers":
            body["sP"], body["s"] = [], []
        else:
            body["tP"], body["t"], body["tS"] = [], [], []
        return jsonify(body)

    body = dict(envelope.raw)
    body.pop("_client_meta", None)
    return jsonify(body)


@app.post("/api/v2/home")
def api_home():
    return _passthrough("home")


@app.post("/api/v2/stickers")
def api_stickers():
    return _passthrough("stickers")


@app.post("/api/v2/templates")
def api_templates():
    return _passthrough("templates")


# ------------------------------------------------------------------- catalog UI
def _pack_names(envelope) -> dict[int, str]:
    names = {}
    for pack in envelope.raw.get("sP") or []:
        names[int(pack["id"])] = pack.get("n") or f"pack-{pack['id']}"
    for pack in envelope.raw.get("tP") or []:
        names[int(pack["id"])] = pack.get("n") or f"pack-{pack['id']}"
    return names


def _page(items: list[Any], default_size: int = 60) -> tuple[list[Any], dict[str, int]]:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        size = min(500, max(1, int(request.args.get("size", default_size))))
    except ValueError:
        size = default_size
    start = (page - 1) * size
    return items[start : start + size], {"page": page, "size": size, "total": len(items)}


@app.get("/catalog/summary")
def catalog_summary():
    try:
        home, stickers, templates = _fetch("home"), _fetch("stickers"), _fetch("templates")
    except GraphionicaError as exc:
        return jsonify({"error": "upstream_unavailable", "detail": str(exc)}), 502
    data = summarize(home, stickers, templates)
    data["upstream_latency_ms"] = {
        name: (slot["envelope"].raw.get("_client_meta") or {}).get("elapsed_ms")
        for name, slot in _state.items()
        if slot["envelope"] is not None
    }
    data["cache"] = {name: round(time.time() - slot["fetched_at"], 1) for name, slot in _state.items()}
    return jsonify(data)


@app.get("/catalog/stickers")
def catalog_stickers():
    try:
        envelope = _fetch("stickers")
    except GraphionicaError as exc:
        return jsonify({"error": "upstream_unavailable", "detail": str(exc)}), 502
    items = list(envelope.raw.get("s") or [])
    if request.args.get("pack"):
        try:
            pack_id = int(request.args["pack"])
        except ValueError:
            abort(400, "pack must be an integer")
        items = [s for s in items if int(s.get("pId") or 0) == pack_id]
    if request.args.get("include_premium", "1") == "0":
        items = [s for s in items if not s.get("isP")]
    query = (request.args.get("q") or "").strip().lower()
    if query:
        packs = _pack_names(envelope)
        items = [s for s in items if query in packs.get(int(s.get("pId") or 0), "").lower()]
    items.sort(key=lambda s: (int(s.get("pId") or 0), int(s.get("o") or 0)))
    page_items, meta = _page(items)
    packs = _pack_names(envelope)
    out = []
    for sticker in page_items:
        path = sticker.get("u")
        out.append(
            {
                "id": sticker.get("id"),
                "pack_id": sticker.get("pId"),
                "pack": packs.get(int(sticker.get("pId") or 0)),
                "premium": bool(sticker.get("isP")),
                "animated": bool(sticker.get("a")),
                "width": sticker.get("w"),
                "height": sticker.get("h"),
                "url": _client.asset_url(envelope, path),
                "proxy": f"/asset/s/{path}",
                "preview_url": _client.asset_url(envelope, sticker.get("p")),
                "preview_proxy": f"/asset/s/{sticker.get('p')}",
            }
        )
    return jsonify({"meta": meta, "packs": packs, "items": out})


@app.get("/catalog/templates")
def catalog_templates():
    try:
        envelope = _fetch("templates")
    except GraphionicaError as exc:
        return jsonify({"error": "upstream_unavailable", "detail": str(exc)}), 502
    items = list(envelope.raw.get("t") or [])
    if request.args.get("pack"):
        try:
            pack_id = int(request.args["pack"])
        except ValueError:
            abort(400, "pack must be an integer")
        items = [t for t in items if int(t.get("pId") or 0) == pack_id]
    if request.args.get("include_premium", "1") == "0":
        items = [t for t in items if not t.get("isP")]
    items.sort(key=lambda t: (int(t.get("pId") or 0), int(t.get("o") or 0)))
    page_items, meta = _page(items, default_size=24)
    packs = _pack_names(envelope)
    out = []
    for template in page_items:
        path = template.get("p")
        layers = template.get("i") or []
        out.append(
            {
                "id": template.get("id"),
                "pack_id": template.get("pId"),
                "pack": packs.get(int(template.get("pId") or 0)),
                "premium": bool(template.get("isP")),
                "aspect": template.get("d"),
                "background": template.get("c"),
                "layer_count": len(layers),
                "popular_order": template.get("pO"),
                "texts": [layer.get("t") for layer in layers if layer.get("tit") == "txt"],
                "layers": layers,
                "url": _client.asset_url(envelope, path),
                "proxy": f"/asset/t/{path}",
            }
        )
    return jsonify({"meta": meta, "packs": packs, "items": out})


@app.get("/export/manifest.json")
def export_manifest():
    try:
        envelopes = {name: _fetch(name) for name in ("home", "stickers", "templates")}
    except GraphionicaError as exc:
        return jsonify({"error": "upstream_unavailable", "detail": str(exc)}), 502
    seen: dict[str, dict[str, Any]] = {}
    for name, envelope in envelopes.items():
        for path, kind in GraphionicaClient.asset_paths(envelope):
            key = f"{kind}/{path}"
            if key in seen:
                continue
            cache_path = ASSET_DIR / kind / path
            seen[key] = {
                "kind": kind,
                "path": path,
                "source": name,
                "url": _client.asset_url(envelope, path),
                "mirror_url": _client.asset_url(envelope, path, mirror=True),
                "cached_bytes": cache_path.stat().st_size if cache_path.exists() else None,
            }
    return jsonify(
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "counts": {
                "total": len(seen),
                "by_kind": {
                    kind: sum(1 for v in seen.values() if v["kind"] == kind)
                    for kind in {v["kind"] for v in seen.values()}
                },
                "cached": sum(1 for v in seen.values() if v["cached_bytes"]),
            },
            "assets": list(seen.values()),
        }
    )


@app.get("/asset/<kind>/<path:path>")
def asset_proxy(kind: str, path: str):
    if kind not in {"s", "t", "b"}:
        abort(404)
    # keep the cache-busting query in the on-disk key, exactly like harvest.py does
    full = path + (("?" + request.query_string.decode()) if request.query_string else "")
    target = (ASSET_DIR / kind / GraphionicaClient.asset_key(full)).resolve()
    if not str(target).startswith(str(ASSET_DIR.resolve())):
        abort(400, "invalid asset path")
    if target.exists() and target.stat().st_size:
        _counters["asset_hits"] += 1
        return send_from_directory(target.parent, target.name, max_age=86400)

    try:
        envelope = _fetch("stickers" if kind == "s" else "templates" if kind == "t" else "home")
    except GraphionicaError as exc:
        return jsonify({"error": "upstream_unavailable", "detail": str(exc)}), 502

    try:
        body, used = _client.download_asset(envelope, full)
    except AssetNotFound:
        return jsonify({"error": "asset_not_found", "path": full}), 404
    except GraphionicaError as exc:
        return jsonify({"error": "asset_unavailable", "detail": str(exc)}), 502

    if "/graphionica/" in used:
        _counters["asset_mirror_fallbacks"] += 1
    _counters["asset_misses"] += 1
    _counters["asset_bytes"] += len(body)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    response = Response(body, mimetype="image/webp" if target.suffix == ".webp" else "application/octet-stream")
    response.headers["Cache-Control"] = "public, max-age=86400"
    response.headers["X-Graphionica-Source"] = used
    return response


# ----------------------------------------------------------------------- status
@app.get("/health")
def health():
    ready = all(slot["envelope"] is not None for slot in _state.values())
    return jsonify(
        {
            "ok": ready,
            "cached": {name: slot["envelope"] is not None for name, slot in _state.items()},
            "cache_age_s": {
                name: (round(time.time() - slot["fetched_at"], 1) if slot["envelope"] else None)
                for name, slot in _state.items()
            },
            "ttl_s": TTL,
            "port": PORT,
        }
    )


@app.get("/stats")
def stats():
    return jsonify(_counters)


@app.get("/")
def index():
    page = STATIC_DIR / "demo.html"
    if not page.exists():
        return jsonify({"error": "demo.html missing", "hint": "expected at static/demo.html"}), 500
    return Response(page.read_text(encoding="utf-8"), mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    print(f"[boot] Graphionica gateway on http://{HOST}:{PORT} (ttl={TTL}s)", flush=True)
    _load_disk_cache()
    threading.Thread(target=_warm, daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True)
