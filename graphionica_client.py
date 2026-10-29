#!/usr/bin/env python3
"""Graphionica 4.6.1 content-catalog client (reverse-engineered protocol).

Upstream (all unauthenticated, no signature, no apikey header):

    POST https://graphionica-app.firebaseapp.com/api/v2/home       -> HomeResponse
    POST https://graphionica-app.firebaseapp.com/api/v2/stickers   -> SResponse
    POST https://graphionica-app.firebaseapp.com/api/v2/templates  -> TResponse

Bodies are ``application/x-www-form-urlencoded``. Every endpoint answers the same
envelope: a CDN prefix (``u`` primary, ``u2`` mirror), a catalog version (``v``)
and an "is the version current" flag (``isV``); when ``isV`` is false the payload
lists are empty, which is how the client does incremental refresh.

Asset URL = ``<u or u2><item path>``: every item path is relative to the envelope
prefix of the endpoint that returned it. ``templates.tS[].u`` looks root-relative
(``s/1.webp``) but still resolves under the templates prefix
(``.../t/s/1.webp`` -> 200). Verified against the live CDN.

No AI / generation endpoint exists in this app; the whole remote surface is the
sticker + story-template catalog plus the asset CDN.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin

import requests

API_BASE = "https://graphionica-app.firebaseapp.com/api/v2/"
CDN_PRIMARY = "https://graphionica.fra1.cdn.digitaloceanspaces.com/"
CDN_MIRROR = "https://storage.yandexcloud.net/graphionica/"

# Client identity the app reports. versionCode 418 == Graphionica 4.6.1.
PLATFORM = "android"
VERSION_CODE = 418
USER_AGENT = "okhttp/4.12.0"

DEFAULT_TIMEOUT = float(os.environ.get("GRAPHIONICA_TIMEOUT", "90"))


class GraphionicaError(RuntimeError):
    """Upstream returned a non-2xx answer or an unparsable body."""


class AssetNotFound(GraphionicaError):
    """The asset is absent from both CDN prefixes (404 on every mirror)."""


@dataclass
class CatalogEnvelope:
    """Common shape of every /api/v2 response."""

    raw: dict[str, Any]
    version: int = 0
    is_current: bool = False
    cdn: str = CDN_PRIMARY
    cdn_mirror: str = CDN_MIRROR
    fetched_at: float = field(default_factory=time.time)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CatalogEnvelope":
        if not isinstance(payload, dict):
            raise GraphionicaError(f"unexpected payload type: {type(payload).__name__}")
        return cls(
            raw=payload,
            version=int(payload.get("v") or 0),
            is_current=bool(payload.get("isV")),
            cdn=payload.get("u") or CDN_PRIMARY,
            cdn_mirror=payload.get("u2") or CDN_MIRROR,
        )


class GraphionicaClient:
    """One HTTP session per client; safe to share between threads for GETs."""

    def __init__(
        self,
        language: str = "en",
        country: str = "US",
        proxies: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.language = language
        self.country = country
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        proxy = os.environ.get("GRAPHIONICA_PROXY")
        if proxies is None and proxy:
            proxies = {"http": proxy, "https": proxy}
        if proxies:
            self.session.proxies.update(proxies)

    # ---------------------------------------------------------------- internals
    def _post_form(self, endpoint: str, fields: dict[str, Any]) -> CatalogEnvelope:
        payload = {k: str(v) for k, v in fields.items() if v is not None}
        started = time.perf_counter()
        try:
            resp = self.session.post(
                urljoin(API_BASE, endpoint),
                data=payload,
                timeout=self.timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except requests.RequestException as exc:  # network-level failure
            raise GraphionicaError(f"{endpoint}: transport error: {exc}") from exc
        elapsed = (time.perf_counter() - started) * 1000
        if resp.status_code != 200:
            raise GraphionicaError(
                f"{endpoint}: HTTP {resp.status_code} after {elapsed:.0f}ms: {resp.text[:200]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:
            raise GraphionicaError(f"{endpoint}: body is not JSON: {resp.text[:200]}") from exc
        envelope = CatalogEnvelope.from_payload(body)
        envelope.raw.setdefault("_client_meta", {})["elapsed_ms"] = round(elapsed, 1)
        return envelope

    # ------------------------------------------------------------------ public
    def home(self, version: int = 0) -> CatalogEnvelope:
        """Banners + catalog version. ``version`` is the last known home version."""
        return self._post_form(
            "home",
            {
                "version": max(0, int(version)),
                "platform": PLATFORM,
                "versionCode": VERSION_CODE,
                "language": self.language,
                "country": self.country,
            },
        )

    def stickers(self, version: int = 0) -> CatalogEnvelope:
        """Sticker packs (``sP``) and stickers (``s``) for the whole catalog."""
        return self._post_form(
            "stickers",
            {
                "version": max(0, int(version)),
                "platform": PLATFORM,
                "versionCode": VERSION_CODE,
                "language": self.language,
                "country": self.country,
            },
        )

    def templates(self, version: int = 0, stickers_version: int = 0) -> CatalogEnvelope:
        """Story templates (``t``), template packs (``tP``) and template stickers (``tS``)."""
        return self._post_form(
            "templates",
            {
                "version": max(0, int(version)),
                "stickersVersion": max(0, int(stickers_version)),
                "platform": PLATFORM,
                "versionCode": VERSION_CODE,
                "language": self.language,
                "country": self.country,
            },
        )

    # ------------------------------------------------------------- asset layer
    @staticmethod
    def asset_paths(envelope: CatalogEnvelope) -> Iterator[tuple[str, str]]:
        """Yield ``(relative_path, kind)`` for every asset referenced by an envelope.

        ``kind`` is one of ``b`` (home banner), ``s`` (sticker), ``t`` (template).
        """
        body = envelope.raw
        for banner in body.get("b") or []:
            if banner.get("b"):
                yield banner["b"], "b"
        for pack in body.get("sP") or []:
            for key in ("p", "tV"):
                if pack.get(key):
                    yield pack[key], "s"
        for sticker in body.get("s") or []:
            for key in ("u", "p"):
                if sticker.get(key):
                    yield sticker[key], "s"
        for pack in body.get("tP") or []:
            if pack.get("p"):
                yield pack["p"], "t"
        for template in body.get("t") or []:
            if template.get("p"):
                yield template["p"], "t"
        for extra in body.get("tS") or []:
            path = extra.get("u")
            if path:
                yield path, "s"

    def asset_url(self, envelope: CatalogEnvelope, path: str, mirror: bool = False) -> str:
        """Absolute CDN URL for a catalog path (always relative to the envelope prefix)."""
        base = envelope.cdn_mirror if mirror else envelope.cdn
        if not base.endswith("/"):
            base += "/"
        return base + path.lstrip("/")

    @staticmethod
    def asset_key(path: str) -> str:
        """Filesystem-safe name for a catalog path.

        Some paths carry a cache-busting query (``10_home/preview.webp?v=4``);
        Windows rejects ``?`` in filenames, so the query is encoded into the stem.
        """
        if "?" not in path:
            return path
        base, _, query = path.partition("?")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", query)
        head, slash, name = base.rpartition("/")
        stem, dot, suffix = name.rpartition(".")
        if not dot:
            return f"{base}__{safe}"
        return f"{head}{slash}{stem}__{safe}{dot}{suffix}"

    def download_asset(
        self,
        envelope: CatalogEnvelope,
        path: str,
        timeout: float | None = None,
    ) -> tuple[bytes, str]:
        """Fetch one asset, falling back to the mirror. Returns ``(bytes, url_used)``.

        Raises :class:`AssetNotFound` when every prefix answers 404, so callers can
        map a missing asset to a 404 instead of blaming the upstream.
        """
        errors: list[str] = []
        statuses: list[int] = []
        effective_timeout = timeout or min(self.timeout, 45.0)
        for mirror in (False, True):
            url = self.asset_url(envelope, path, mirror=mirror)
            try:
                resp = self.session.get(url, timeout=effective_timeout)
            except requests.RequestException as exc:
                errors.append(f"{url}: {exc}")
                continue
            statuses.append(resp.status_code)
            if resp.status_code == 200 and resp.content:
                return resp.content, url
            errors.append(f"{url}: HTTP {resp.status_code}")
        if statuses and all(code == 404 for code in statuses):
            raise AssetNotFound(f"asset not found on any mirror: {path}")
        raise GraphionicaError("asset unavailable: " + " | ".join(errors))

    def iter_assets(self, envelope: CatalogEnvelope) -> Iterable[tuple[str, str]]:
        return self.asset_paths(envelope)


def summarize(home: CatalogEnvelope, stickers: CatalogEnvelope, templates: CatalogEnvelope) -> dict[str, Any]:
    """Aggregate counters used by the demo page and the benchmark."""
    sticker_items = stickers.raw.get("s") or []
    template_items = templates.raw.get("t") or []
    return {
        "home": {
            "version": home.version,
            "is_current": home.is_current,
            "banners": len(home.raw.get("b") or []),
            "cdn": home.cdn,
            "cdn_mirror": home.cdn_mirror,
        },
        "stickers": {
            "version": stickers.version,
            "is_current": stickers.is_current,
            "packs": len(stickers.raw.get("sP") or []),
            "items": len(sticker_items),
            "premium_items": sum(1 for s in sticker_items if s.get("isP")),
            "animated_items": sum(1 for s in sticker_items if s.get("a")),
            "cdn": stickers.cdn,
        },
        "templates": {
            "version": templates.version,
            "is_current": templates.is_current,
            "packs": len(templates.raw.get("tP") or []),
            "items": len(template_items),
            "template_stickers": len(templates.raw.get("tS") or []),
            "premium_items": sum(1 for t in template_items if t.get("isP")),
            "cdn": templates.cdn,
        },
        "assets": {
            "referenced_paths": len(
                set(p for env in (home, stickers, templates) for p, _ in GraphionicaClient.asset_paths(env))
            ),
        },
    }


if __name__ == "__main__":  # tiny smoke test: python graphionica_client.py
    client = GraphionicaClient()
    hm = client.home()
    st = client.stickers()
    tp = client.templates(stickers_version=st.version)
    print(f"home      v={hm.version} banners={len(hm.raw.get('b') or [])} cdn={hm.cdn}")
    print(f"stickers  v={st.version} packs={len(st.raw.get('sP') or [])} items={len(st.raw.get('s') or [])}")
    print(f"templates v={tp.version} packs={len(tp.raw.get('tP') or [])} items={len(tp.raw.get('t') or [])}")
    sample = next(iter(st.raw.get("s") or []), None)
    if sample:
        url = client.asset_url(st, sample["u"])
        body, used = client.download_asset(st, sample["u"])
        print(f"sample asset {sample['u']} -> {len(body)} bytes via {used}")
