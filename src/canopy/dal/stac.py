"""Live Sentinel-2 discovery against the Earth Search STAC API.

This is the open source queried by the parameters of the request, as the case
requires: a POST to `/search` with the bounding box, the date range and a cloud
filter, no key and no subscription.  Only the standard library is used, so the
service still installs with nothing but numpy.

Behaviour is deliberately strict about honesty:

* a successful query returns real STAC items, and every item keeps its own
  `scale`, `offset` and processing baseline -- the dataset contains scenes with
  offset -0.1 and scenes with offset 0.0, and a hardcoded constant would corrupt
  exactly the scene taken right after the fire;
* a failure is reported as `SOURCE_UNREACHABLE` with the real error text and the
  analysis continues on the saved catalogue, which is what the case means by
  "the saved source data must allow the analysis to be repeated".

The service never silently pretends a live query succeeded.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..core.config import get_settings
from ..core.status import OK, SOURCE_UNREACHABLE, Status


@dataclass
class StacItem:
    item_id: str
    datetime_utc: str
    cloud_cover: Optional[float]
    processing_baseline: str
    boa_offset_applied: Optional[bool]
    assets: Dict[str, Dict[str, object]] = field(default_factory=dict)
    raw: Dict[str, object] = field(default_factory=dict)

    def band_scale_offset(self, asset_key: str) -> Tuple[float, float]:
        """Per-scene radiometry, read from the item and never assumed."""
        asset = self.assets.get(asset_key, {})
        raster = asset.get("raster:bands") or []
        if raster and isinstance(raster, list):
            b = raster[0]
            return float(b.get("scale", 1.0)), float(b.get("offset", 0.0))
        if self.boa_offset_applied:
            return 0.0001, -0.1
        return 0.0001, 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "item_id": self.item_id,
            "datetime_utc": self.datetime_utc,
            "date": self.datetime_utc[:10],
            "cloud_cover": self.cloud_cover,
            "processing_baseline": self.processing_baseline,
            "boa_offset_applied": self.boa_offset_applied,
            "asset_keys": sorted(self.assets.keys()),
        }


@dataclass
class StacResult:
    status: Status = field(default_factory=Status)
    items: List[StacItem] = field(default_factory=list)
    endpoint: str = ""
    collection: str = ""
    query: Dict[str, object] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "status": self.status.as_dict(),
            "endpoint": self.endpoint,
            "collection": self.collection,
            "query": self.query,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "item_count": len(self.items),
            "items": [i.as_dict() for i in self.items[:40]],
        }


def search_sentinel2(
    bbox: Tuple[float, float, float, float],
    date_from: str,
    date_to: str,
    max_cloud: Optional[float] = None,
    limit: Optional[int] = None,
    timeout_s: Optional[float] = None,
) -> StacResult:
    """Query Earth Search for Sentinel-2 L2A items covering a bbox and period."""
    import time

    cfg = get_settings().sources.get("stac", {}).get("sentinel2", {})
    endpoint = cfg.get("endpoint", "https://earth-search.aws.element84.com/v1")
    collection = cfg.get("collection", "sentinel-2-l2a")
    timeout = float(timeout_s if timeout_s is not None else cfg.get("timeout_s", 20))
    limit = int(limit or cfg.get("max_items", 60))

    body: Dict[str, object] = {
        "collections": [collection],
        "bbox": list(bbox),
        "datetime": f"{date_from}T00:00:00Z/{date_to}T23:59:59Z",
        "limit": limit,
    }
    if max_cloud is not None:
        body["query"] = {"eo:cloud_cover": {"lte": max_cloud}}

    res = StacResult(endpoint=endpoint, collection=collection, query=body)
    if not cfg.get("enabled", True):
        res.status = Status(SOURCE_UNREACHABLE, "Живой источник отключён в config/sources.json")
        return res

    url = endpoint.rstrip("/") + "/search"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/geo+json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError, ValueError) as exc:
        res.elapsed_ms = int((time.time() - started) * 1000)
        res.error = f"{type(exc).__name__}: {exc}"
        res.status = Status(
            SOURCE_UNREACHABLE,
            f"Earth Search недоступен из этой среды ({res.error}). "
            "Анализ продолжен по сохранённому набору.",
            context={"endpoint": endpoint},
        )
        return res

    res.elapsed_ms = int((time.time() - started) * 1000)
    for feat in payload.get("features", []):
        props = feat.get("properties", {})
        res.items.append(
            StacItem(
                item_id=feat.get("id", ""),
                datetime_utc=props.get("datetime", ""),
                cloud_cover=props.get("eo:cloud_cover"),
                processing_baseline=str(props.get("s2:processing_baseline", "")),
                boa_offset_applied=props.get("earthsearch:boa_offset_applied"),
                assets=feat.get("assets", {}),
                raw=feat,
            )
        )
    res.items.sort(key=lambda i: i.datetime_utc)
    res.status = Status(OK)
    return res


def probe() -> Dict[str, object]:
    """A cheap reachability check used by the readiness screen."""
    cfg = get_settings().sources.get("stac", {}).get("sentinel2", {})
    endpoint = cfg.get("endpoint", "")
    try:
        req = urllib.request.Request(endpoint.rstrip("/") + "/collections", method="GET")
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout_s", 20))) as resp:
            return {"reachable": True, "http_status": resp.status, "endpoint": endpoint}
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return {
            "reachable": False,
            "endpoint": endpoint,
            "error": f"{type(exc).__name__}: {exc}",
        }
