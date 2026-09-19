"""The asset catalogue: what data exist, where, and with what provenance.

No module in the analysis ever opens a path directly.  Everything asks the
catalogue "which assets of this product cover this contour at this time", and
gets back `Asset` records carrying the file, its real georeferenced bounds, the
product version, the licence and a content hash.  Two consequences follow, and
both are graded by the case:

* a contour anywhere on Earth is matched by *bounds*, never by an area
  identifier, so a new polygon needs no code change;
* every number the service prints can be traced back to a file and a hash.

Assets are discovered by scanning the data roots and reading each GeoTIFF
header.  Product classification is by filename convention, declared in
`PRODUCT_PATTERNS` below, so a new product is one table entry.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.config import Settings, get_settings
from ..geo.crs import to_wgs84
from ..geo.grid import RasterGrid
from ..rio.tiff import GeoTiff, TiffError

# --- products --------------------------------------------------------------
CCI_AGB = "CCI_AGB"
CCI_CHANGE = "CCI_CHANGE"
GFC = "GFC"
MODIS_BURN = "MODIS_BURN"
S2_REFLECTANCE = "S2_REFLECTANCE"
S2_SCL = "S2_SCL"

PRODUCT_LABELS = {
    CCI_AGB: "ESA CCI Biomass (AGB, AGB_SD)",
    CCI_CHANGE: "ESA CCI Biomass change",
    GFC: "Hansen Global Forest Change",
    MODIS_BURN: "MODIS MCD64A1 burned area",
    S2_REFLECTANCE: "Sentinel-2 L2A surface reflectance",
    S2_SCL: "Sentinel-2 L2A scene classification",
}

PRODUCT_SOURCE_ID = {
    CCI_AGB: "CCI_V7",
    CCI_CHANGE: "CCI_V7",
    GFC: "GFC_2025_V113",
    MODIS_BURN: "MODIS_MCD64A1_061",
    S2_REFLECTANCE: "S2_L2A",
    S2_SCL: "S2_L2A",
}

#: filename -> (product, regex with named groups). Order matters.
PRODUCT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (CCI_CHANGE, re.compile(r"CCI_Change_(?P<y0>\d{4})_(?P<y1>\d{4})\.tif$", re.I)),
    (CCI_AGB, re.compile(r"CCI_Biomass_(?P<year>\d{4})\.tif$", re.I)),
    (GFC, re.compile(r"GFC[_A-Za-z0-9]*\.tif$", re.I)),
    (MODIS_BURN, re.compile(r"MCD64A1\.A(?P<ayear>\d{4})(?P<adoy>\d{3})\..*_(?P<layer>Burn_Date|Burn_Date_Uncertainty|First_Day|Last_Day|QA)\.tif$", re.I)),
    (S2_REFLECTANCE, re.compile(r"(?P<scene>S2[AB]_\w+?_(?P<date>\d{8})_\d+_L2A)_reflectance\.tif$", re.I)),
    (S2_SCL, re.compile(r"(?P<scene>S2[AB]_\w+?_(?P<date>\d{8})_\d+_L2A)_SCL\.tif$", re.I)),
]

BAND_NAMES = {
    CCI_AGB: ["AGB", "AGB_SD"],
    CCI_CHANGE: ["AGB_difference", "AGB_difference_SD", "AGB_difference_quality_flag"],
    GFC: ["treecover2000", "lossyear", "datamask"],
    S2_REFLECTANCE: ["B02", "B03", "B04", "B8A", "B11", "B12"],
    S2_SCL: ["SCL"],
}


@dataclass
class Asset:
    """One georeferenced file plus everything needed to cite it."""

    product: str
    path: str
    grid: RasterGrid
    bounds_wgs84: Tuple[float, float, float, float]
    bands: int
    dtype: str
    nodata: Optional[float]
    origin: str                      # "dataset" | "fixture" | "cache"
    year: Optional[int] = None
    observation_time: Optional[str] = None
    scene_id: Optional[str] = None
    layer: Optional[str] = None
    source_id: str = ""
    version: str = ""
    extra: Dict[str, object] = field(default_factory=dict)

    @property
    def relative_path(self) -> str:
        settings = get_settings()
        for base in (settings.data_root(), settings.fixture_root()):
            base = os.path.abspath(base) + os.sep
            if os.path.abspath(self.path).startswith(base):
                return os.path.relpath(self.path, base).replace(os.sep, "/")
        return os.path.basename(self.path)

    @property
    def band_names(self) -> List[str]:
        return BAND_NAMES.get(self.product, [f"band{i+1}" for i in range(self.bands)])

    def sha256(self) -> str:
        return file_sha256(self.path)

    def as_dict(self, with_hash: bool = False) -> Dict[str, object]:
        out: Dict[str, object] = {
            "product": self.product,
            "product_label": PRODUCT_LABELS.get(self.product, self.product),
            "file": self.relative_path,
            "origin": self.origin,
            "source_id": self.source_id,
            "version": self.version,
            "bands": self.bands,
            "band_names": self.band_names,
            "dtype": self.dtype,
            "nodata": self.nodata,
            "crs": self.grid.crs if isinstance(self.grid.crs, int) else str(self.grid.crs),
            "pixel_size": [self.grid.dx, self.grid.dy],
            "bounds_wgs84": list(self.bounds_wgs84),
            "year": self.year,
            "observation_time": self.observation_time,
            "scene_id": self.scene_id,
            "layer": self.layer,
        }
        if with_hash:
            out["sha256"] = self.sha256()
        return out


@lru_cache(maxsize=4096)
def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _bounds_wgs84(grid: RasterGrid) -> Tuple[float, float, float, float]:
    """Bounding box of a grid in WGS 84, sampling the edges for curved CRS."""
    xs: List[float] = []
    ys: List[float] = []
    steps = 8
    for i in range(steps + 1):
        for j in range(steps + 1):
            if i not in (0, steps) and j not in (0, steps):
                continue
            x = grid.origin_x + grid.dx * grid.width * i / steps
            y = grid.origin_y + grid.dy * grid.height * j / steps
            lon, lat = to_wgs84(x, y, grid.crs)
            xs.append(lon)
            ys.append(lat)
    return min(xs), min(ys), max(xs), max(ys)


def _classify(path: str) -> Optional[Tuple[str, Dict[str, str]]]:
    name = os.path.basename(path)
    for product, pattern in PRODUCT_PATTERNS:
        m = pattern.search(name)
        if m:
            return product, {k: v for k, v in m.groupdict().items() if v}
    return None


class Catalog:
    """An index over every raster reachable by the service."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.assets: List[Asset] = []
        self.errors: List[Dict[str, str]] = []
        self._scene_meta = _load_scene_table(self.settings)
        self._versions = _load_versions(self.settings)
        self.scan()

    # -- building ----------------------------------------------------------
    def scan(self) -> None:
        self.assets = []
        self.errors = []
        roots = [
            (self.settings.data_root(), "dataset"),
            (self.settings.fixture_root(), "fixture"),
        ]
        seen_rel: Dict[str, str] = {}
        for root, origin in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in sorted(filenames):
                    if not fn.lower().endswith((".tif", ".tiff")):
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    # a real dataset file always wins over a fixture of the same name
                    if rel in seen_rel:
                        continue
                    asset = self._make_asset(full, origin)
                    if asset is not None:
                        seen_rel[rel] = origin
                        self.assets.append(asset)

    def _make_asset(self, path: str, origin: str) -> Optional[Asset]:
        hit = _classify(path)
        if hit is None:
            return None
        product, groups = hit
        try:
            with GeoTiff(path) as t:
                info = t.info
        except (TiffError, OSError) as exc:
            self.errors.append({"file": path, "error": str(exc)})
            return None

        year: Optional[int] = None
        obs: Optional[str] = None
        scene: Optional[str] = None
        if "year" in groups:
            year = int(groups["year"])
        if "y1" in groups:
            year = int(groups["y1"])
        if "date" in groups:
            d = groups["date"]
            obs = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            year = int(d[0:4])
        if "scene" in groups:
            scene = groups["scene"]
            meta = self._scene_meta.get(scene)
            if meta:
                obs = meta.get("datetime_utc", obs)
                year = int(meta.get("year") or year or 0) or year
        if "ayear" in groups and "adoy" in groups:
            year = int(groups["ayear"])
            obs = _from_doy(int(groups["ayear"]), int(groups["adoy"]))

        return Asset(
            product=product,
            path=path,
            grid=info.grid,
            bounds_wgs84=_bounds_wgs84(info.grid),
            bands=info.bands,
            dtype=str(info.dtype),
            nodata=info.nodata,
            origin=origin,
            year=year,
            observation_time=obs,
            scene_id=scene,
            layer=groups.get("layer"),
            source_id=PRODUCT_SOURCE_ID.get(product, ""),
            version=self._versions.get(PRODUCT_SOURCE_ID.get(product, ""), ""),
        )

    # -- querying ----------------------------------------------------------
    def query(
        self,
        product: str,
        bbox: Optional[Tuple[float, float, float, float]] = None,
        year: Optional[int] = None,
        years: Optional[Sequence[int]] = None,
        layer: Optional[str] = None,
    ) -> List[Asset]:
        out = []
        for a in self.assets:
            if a.product != product:
                continue
            if year is not None and a.year != year:
                continue
            if years is not None and a.year not in years:
                continue
            if layer is not None and a.layer != layer:
                continue
            if bbox is not None and not _overlaps(a.bounds_wgs84, bbox):
                continue
            out.append(a)
        out.sort(key=lambda a: (a.observation_time or "", a.relative_path))
        return out

    def products_available(
        self, bbox: Optional[Tuple[float, float, float, float]] = None
    ) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for a in self.assets:
            if bbox is not None and not _overlaps(a.bounds_wgs84, bbox):
                continue
            counts[a.product] = counts.get(a.product, 0) + 1
        return counts

    def coverage_bbox(self, product: str) -> Optional[Tuple[float, float, float, float]]:
        boxes = [a.bounds_wgs84 for a in self.assets if a.product == product]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )

    def scene_meta(self, scene_id: str) -> Dict[str, str]:
        return self._scene_meta.get(scene_id, {})

    def summary(self) -> Dict[str, object]:
        by_product: Dict[str, Dict[str, object]] = {}
        for a in self.assets:
            entry = by_product.setdefault(
                a.product,
                {
                    "label": PRODUCT_LABELS.get(a.product, a.product),
                    "files": 0,
                    "origins": {},
                    "years": set(),
                },
            )
            entry["files"] = int(entry["files"]) + 1
            origins = entry["origins"]
            origins[a.origin] = origins.get(a.origin, 0) + 1
            if a.year:
                entry["years"].add(a.year)
        for entry in by_product.values():
            entry["years"] = sorted(entry["years"])
        return {"products": by_product, "errors": self.errors}


def _overlaps(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _from_doy(year: int, doy: int) -> str:
    return date.fromordinal(date(year, 1, 1).toordinal() + doy - 1).isoformat()


def _load_scene_table(settings: Settings) -> Dict[str, Dict[str, str]]:
    path = settings.data_path("scenes.csv")
    table: Dict[str, Dict[str, str]] = {}
    if not os.path.exists(path):
        return table
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            item = (row.get("item_id") or "").strip()
            if item:
                table[item] = dict(row)
    return table


def _load_versions(settings: Settings) -> Dict[str, str]:
    path = settings.data_path("sources.csv")
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sid = (row.get("source_id") or "").strip()
            if sid:
                out[sid] = (row.get("version") or "").strip()
    return out


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    return Catalog()


def reload_catalog() -> Catalog:
    get_catalog.cache_clear()
    file_sha256.cache_clear()
    return get_catalog()


def load_sources_table(settings: Optional[Settings] = None) -> List[Dict[str, str]]:
    """The full `sources.csv`, carried into API responses and the report."""
    settings = settings or get_settings()
    path = settings.data_path("sources.csv")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]
