"""A self-contained GeoTIFF reader and writer.

No GDAL, no rasterio, no external wheels: the service must start on a clean
Python with numpy and nothing else, which also means a jury can run it without
an hour of geospatial installation.

Supported on read
-----------------
* classic TIFF and BigTIFF, little and big endian
* strip and tile layouts, windowed reads (only the needed blocks are decoded)
* compression: none, LZW, Deflate/zlib, PackBits
* horizontal differencing predictor
* planar configuration 1 (contiguous) and 2 (separate)
* sample formats uint8/16/32, int8/16/32, float32/64
* GeoTIFF georeferencing: ModelPixelScale + ModelTiepoint, or ModelTransformation
* CRS from the GeoKey directory: EPSG code, or MODIS sinusoidal by projection key
* nodata from GDAL_NODATA

Supported on write
------------------
Classic TIFF, contiguous, strip layout, uncompressed or Deflate, with the
GeoTIFF keys needed to describe a north-up grid in EPSG:4326, a UTM zone or the
MODIS sinusoidal grid.  That is exactly what the fixture generator needs.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..geo.crs import SINUSOIDAL
from ..geo.grid import RasterGrid

# --- TIFF tag numbers -------------------------------------------------------
T_WIDTH = 256
T_LENGTH = 257
T_BITS = 258
T_COMPRESSION = 259
T_PHOTOMETRIC = 262
T_STRIP_OFFSETS = 273
T_SAMPLES = 277
T_ROWS_PER_STRIP = 278
T_STRIP_BYTES = 279
T_PLANAR = 284
T_PREDICTOR = 317
T_SAMPLE_FORMAT = 339
T_TILE_WIDTH = 322
T_TILE_LENGTH = 323
T_TILE_OFFSETS = 324
T_TILE_BYTES = 325
T_MODEL_PIXEL_SCALE = 33550
T_MODEL_TIEPOINT = 33922
T_MODEL_TRANSFORM = 34264
T_GEO_KEYS = 34735
T_GEO_DOUBLES = 34736
T_GEO_ASCII = 34737
T_GDAL_NODATA = 42113

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
              11: 4, 12: 8, 16: 8, 17: 8, 18: 8}


class TiffError(Exception):
    pass


@dataclass
class TiffInfo:
    width: int
    height: int
    bands: int
    dtype: np.dtype
    grid: RasterGrid
    nodata: Optional[float]
    compression: int
    tiled: bool
    block_size: Tuple[int, int]
    epsg: Optional[int]
    tags: Dict[int, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "bands": self.bands,
            "dtype": str(self.dtype),
            "crs": self.epsg if self.epsg is not None else str(self.grid.crs),
            "pixel_size": [self.grid.dx, self.grid.dy],
            "origin": [self.grid.origin_x, self.grid.origin_y],
            "nodata": self.nodata,
            "compression": self.compression,
            "tiled": self.tiled,
        }


# ---------------------------------------------------------------------------
# decompression helpers
# ---------------------------------------------------------------------------
def _lzw_decode(data: bytes) -> bytes:
    """TIFF flavour of LZW: MSB-first, codes grow at 511/1023/2047."""
    out = bytearray()
    dictionary: List[bytes] = [bytes([i]) for i in range(256)] + [b"", b""]
    dict_len = 258
    code_len = 9
    prev: Optional[bytes] = None
    bitbuf = 0
    bitcnt = 0
    for byte in data:
        bitbuf = (bitbuf << 8) | byte
        bitcnt += 8
        while bitcnt >= code_len:
            bitcnt -= code_len
            code = (bitbuf >> bitcnt) & ((1 << code_len) - 1)
            if code == 256:                       # ClearCode
                dictionary = [bytes([i]) for i in range(256)] + [b"", b""]
                dict_len = 258
                code_len = 9
                prev = None
                continue
            if code == 257:                       # EndOfInformation
                return bytes(out)
            if prev is None:
                entry = dictionary[code]
            elif code < dict_len:
                entry = dictionary[code]
                dictionary.append(prev + entry[:1])
                dict_len += 1
            else:
                entry = prev + prev[:1]
                dictionary.append(entry)
                dict_len += 1
            out += entry
            prev = entry
            if dict_len + 1 >= (1 << code_len) and code_len < 12:
                code_len += 1
    return bytes(out)


def _packbits_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        h = data[i]
        i += 1
        if h < 128:
            out += data[i : i + h + 1]
            i += h + 1
        elif h > 128:
            if i < n:
                out += bytes([data[i]]) * (257 - h)
                i += 1
    return bytes(out)


def _decompress(raw: bytes, compression: int) -> bytes:
    if compression == 1:
        return raw
    if compression == 5:
        return _lzw_decode(raw)
    if compression in (8, 32946):
        return zlib.decompress(raw)
    if compression == 32773:
        return _packbits_decode(raw)
    raise TiffError(f"unsupported TIFF compression {compression}")


def _unpredict(arr: np.ndarray, predictor: int) -> np.ndarray:
    if predictor == 2:
        np.cumsum(arr, axis=-2 if arr.ndim == 3 else -1, dtype=arr.dtype, out=arr)
    return arr


# ---------------------------------------------------------------------------
# reader
# ---------------------------------------------------------------------------
class GeoTiff:
    """Read-only access to a (Geo)TIFF file."""

    def __init__(self, path: str):
        self.path = str(path)
        self._fh = open(self.path, "rb")
        try:
            self._read_header()
        except Exception:
            self._fh.close()
            raise

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "GeoTiff":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    # -- header ------------------------------------------------------------
    def _read_header(self) -> None:
        head = self._fh.read(16)
        if len(head) < 8:
            raise TiffError("file too short to be a TIFF")
        bo = head[:2]
        if bo == b"II":
            self._e = "<"
        elif bo == b"MM":
            self._e = ">"
        else:
            raise TiffError("not a TIFF file (bad byte order mark)")
        magic = struct.unpack(self._e + "H", head[2:4])[0]
        if magic == 42:
            self.big = False
            ifd_off = struct.unpack(self._e + "I", head[4:8])[0]
        elif magic == 43:
            self.big = True
            offsize = struct.unpack(self._e + "H", head[4:6])[0]
            if offsize != 8:
                raise TiffError("unsupported BigTIFF offset size")
            ifd_off = struct.unpack(self._e + "Q", head[8:16])[0]
        else:
            raise TiffError(f"not a TIFF file (magic {magic})")

        self.tags: Dict[int, object] = {}
        self._read_ifd(ifd_off)
        self._build_info()

    def _read_ifd(self, offset: int) -> None:
        f = self._fh
        f.seek(offset)
        if self.big:
            count = struct.unpack(self._e + "Q", f.read(8))[0]
            entry_size, cntfmt, offfmt = 20, "Q", "Q"
        else:
            count = struct.unpack(self._e + "H", f.read(2))[0]
            entry_size, cntfmt, offfmt = 12, "I", "I"
        raw = f.read(entry_size * count)
        for i in range(count):
            e = raw[i * entry_size : (i + 1) * entry_size]
            tag, typ = struct.unpack(self._e + "HH", e[:4])
            n = struct.unpack(self._e + cntfmt, e[4 : 4 + struct.calcsize(cntfmt)])[0]
            vfield = e[4 + struct.calcsize(cntfmt) :]
            size = _TYPE_SIZE.get(typ, 0) * n
            if size == 0:
                continue
            if size <= len(vfield):
                data = vfield[:size]
            else:
                off = struct.unpack(self._e + offfmt, vfield[: struct.calcsize(offfmt)])[0]
                cur = f.tell()
                f.seek(off)
                data = f.read(size)
                f.seek(cur)
            self.tags[tag] = self._decode_tag(typ, n, data)

    def _decode_tag(self, typ: int, n: int, data: bytes):
        e = self._e
        if typ == 2:
            return data.split(b"\x00")[0].decode("ascii", "replace")
        fmt = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i",
               11: "f", 12: "d", 16: "Q", 17: "q", 18: "Q"}.get(typ)
        if fmt is None:
            return data
        vals = struct.unpack(e + fmt * n, data[: struct.calcsize(fmt) * n])
        return list(vals)

    def _tag1(self, tag: int, default=None):
        v = self.tags.get(tag, default)
        if isinstance(v, list):
            return v[0] if v else default
        return v

    def _build_info(self) -> None:
        width = int(self._tag1(T_WIDTH, 0))
        height = int(self._tag1(T_LENGTH, 0))
        bands = int(self._tag1(T_SAMPLES, 1))
        bits = self.tags.get(T_BITS, [8])
        if not isinstance(bits, list):
            bits = [bits]
        fmts = self.tags.get(T_SAMPLE_FORMAT, [1] * bands)
        if not isinstance(fmts, list):
            fmts = [fmts]
        bps = int(bits[0])
        sfmt = int(fmts[0]) if fmts else 1
        if any(int(b) != bps for b in bits):
            raise TiffError("mixed bit depths per band are not supported")
        dtype = _numpy_dtype(bps, sfmt, self._e)

        compression = int(self._tag1(T_COMPRESSION, 1))
        tiled = T_TILE_OFFSETS in self.tags
        if tiled:
            block = (int(self._tag1(T_TILE_LENGTH, 0)), int(self._tag1(T_TILE_WIDTH, 0)))
        else:
            rps = int(self._tag1(T_ROWS_PER_STRIP, height or 1))
            block = (max(1, rps), width)

        grid, epsg = self._georeference(width, height)

        nodata = None
        nd = self.tags.get(T_GDAL_NODATA)
        if isinstance(nd, str):
            try:
                nodata = float(nd.strip())
            except ValueError:
                nodata = None

        self.info = TiffInfo(
            width=width,
            height=height,
            bands=bands,
            dtype=dtype,
            grid=grid,
            nodata=nodata,
            compression=compression,
            tiled=tiled,
            block_size=block,
            epsg=epsg,
            tags=self.tags,
        )

    def _georeference(self, width: int, height: int) -> Tuple[RasterGrid, Optional[int]]:
        scale = self.tags.get(T_MODEL_PIXEL_SCALE)
        tie = self.tags.get(T_MODEL_TIEPOINT)
        trans = self.tags.get(T_MODEL_TRANSFORM)
        if trans and len(trans) >= 16:
            ox, dx, dy = trans[3], trans[0], trans[5]
            oy = trans[7]
        elif scale and tie and len(scale) >= 2 and len(tie) >= 6:
            dx = float(scale[0])
            dy = -float(scale[1])
            ox = float(tie[3]) - float(tie[0]) * dx
            oy = float(tie[4]) - float(tie[1]) * dy
        else:
            ox, oy, dx, dy = 0.0, float(height), 1.0, -1.0

        epsg, crs = self._crs_from_geokeys()
        return RasterGrid(ox, oy, dx, dy, width, height, crs), epsg

    def _crs_from_geokeys(self) -> Tuple[Optional[int], object]:
        keys = self.tags.get(T_GEO_KEYS)
        ascii_params = self.tags.get(T_GEO_ASCII, "") or ""
        if not keys or len(keys) < 4:
            if "sinusoidal" in str(ascii_params).lower():
                return None, SINUSOIDAL
            return 4326, 4326
        n = int(keys[3])
        found: Dict[int, int] = {}
        for i in range(n):
            base = 4 + i * 4
            if base + 3 >= len(keys):
                break
            key_id, loc, count, value = (
                int(keys[base]),
                int(keys[base + 1]),
                int(keys[base + 2]),
                int(keys[base + 3]),
            )
            if loc == 0:
                found[key_id] = value
        # 3072 ProjectedCSTypeGeoKey, 2048 GeographicTypeGeoKey
        proj = found.get(3072)
        if proj and proj not in (32767, 0):
            return proj, proj
        # CT_Sinusoidal == 24 in ProjCoordTransGeoKey (3075)
        if found.get(3075) == 24 or "sinusoidal" in str(ascii_params).lower():
            return None, SINUSOIDAL
        geog = found.get(2048)
        if geog in (4326, 4322, None, 0, 32767):
            return 4326, 4326
        return geog, geog

    # -- pixel data --------------------------------------------------------
    def read(
        self,
        band: Optional[int] = None,
        window: Optional[Tuple[int, int, int, int]] = None,
    ) -> np.ndarray:
        """Read pixels.

        band=None returns (bands, rows, cols); an integer (1-based) returns a
        2-D array.  window is (row0, col0, nrows, ncols).
        """
        info = self.info
        if window is None:
            window = (0, 0, info.height, info.width)
        r0, c0, nr, nc = window
        r0 = max(0, r0)
        c0 = max(0, c0)
        nr = max(0, min(nr, info.height - r0))
        nc = max(0, min(nc, info.width - c0))
        if nr == 0 or nc == 0:
            shape = (info.bands, 0, 0) if band is None else (0, 0)
            return np.zeros(shape, dtype=info.dtype)

        full = self._read_blocks(r0, c0, nr, nc)   # (bands, nr, nc)
        if band is None:
            return full
        if not (1 <= band <= info.bands):
            raise TiffError(f"band {band} out of range 1..{info.bands}")
        return full[band - 1]

    def _read_blocks(self, r0: int, c0: int, nr: int, nc: int) -> np.ndarray:
        info = self.info
        planar = int(self._tag1(T_PLANAR, 1))
        predictor = int(self._tag1(T_PREDICTOR, 1))
        itemsize = np.dtype(info.dtype).itemsize
        out = np.zeros((info.bands, nr, nc), dtype=info.dtype)

        bh, bw = info.block_size
        if info.tiled:
            offsets = self.tags.get(T_TILE_OFFSETS) or []
            counts = self.tags.get(T_TILE_BYTES) or []
            tiles_across = (info.width + bw - 1) // bw
            tiles_down = (info.height + bh - 1) // bh
        else:
            offsets = self.tags.get(T_STRIP_OFFSETS) or []
            counts = self.tags.get(T_STRIP_BYTES) or []
            tiles_across = 1
            tiles_down = (info.height + bh - 1) // bh
            bw = info.width

        nplanes = info.bands if planar == 2 else 1
        spp_block = 1 if planar == 2 else info.bands

        row_blocks = range(r0 // bh, (r0 + nr - 1) // bh + 1)
        col_blocks = range(c0 // bw, (c0 + nc - 1) // bw + 1)

        for plane in range(nplanes):
            for by in row_blocks:
                for bx in col_blocks:
                    idx = by * tiles_across + bx
                    if planar == 2:
                        idx += plane * tiles_across * tiles_down
                    if idx >= len(offsets):
                        continue
                    self._fh.seek(int(offsets[idx]))
                    raw = self._fh.read(int(counts[idx]))
                    buf = _decompress(raw, info.compression)
                    expected = bh * bw * spp_block * itemsize
                    if len(buf) < expected:
                        buf = buf + b"\x00" * (expected - len(buf))
                    arr = np.frombuffer(buf[:expected], dtype=info.dtype)
                    arr = arr.reshape(bh, bw, spp_block).copy()
                    if predictor == 2:
                        np.cumsum(arr, axis=1, dtype=arr.dtype, out=arr)

                    y_from = max(r0, by * bh)
                    y_to = min(r0 + nr, (by + 1) * bh, info.height)
                    x_from = max(c0, bx * bw)
                    x_to = min(c0 + nc, (bx + 1) * bw, info.width)
                    if y_to <= y_from or x_to <= x_from:
                        continue
                    sub = arr[
                        y_from - by * bh : y_to - by * bh,
                        x_from - bx * bw : x_to - bx * bw,
                        :,
                    ]
                    ty, tx = y_from - r0, x_from - c0
                    if planar == 2:
                        out[plane, ty : ty + sub.shape[0], tx : tx + sub.shape[1]] = sub[:, :, 0]
                    else:
                        for b in range(info.bands):
                            out[b, ty : ty + sub.shape[0], tx : tx + sub.shape[1]] = sub[:, :, b]
        return out


def _numpy_dtype(bits: int, sample_format: int, endian: str) -> np.dtype:
    if sample_format == 3:
        base = {32: "f4", 64: "f8"}.get(bits)
    elif sample_format == 2:
        base = {8: "i1", 16: "i2", 32: "i4", 64: "i8"}.get(bits)
    else:
        base = {8: "u1", 16: "u2", 32: "u4", 64: "u8"}.get(bits)
    if base is None:
        raise TiffError(f"unsupported sample: {bits} bits, format {sample_format}")
    return np.dtype(endian + base) if bits > 8 else np.dtype(base)


def read_info(path: str) -> TiffInfo:
    with GeoTiff(path) as t:
        return t.info


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------
def write_geotiff(
    path: str,
    data: np.ndarray,
    grid: RasterGrid,
    epsg: Optional[int] = 4326,
    nodata: Optional[float] = None,
    compress: bool = True,
    band_names: Optional[List[str]] = None,
) -> None:
    """Write a north-up GeoTIFF.  `data` is (bands, rows, cols) or (rows, cols)."""
    if data.ndim == 2:
        data = data[None, :, :]
    bands, height, width = data.shape
    data = np.ascontiguousarray(data)
    dt = data.dtype
    if dt.kind == "f":
        sample_format, bits = 3, dt.itemsize * 8
    elif dt.kind == "i":
        sample_format, bits = 2, dt.itemsize * 8
    else:
        sample_format, bits = 1, dt.itemsize * 8

    interleaved = np.ascontiguousarray(np.moveaxis(data, 0, -1))
    payload = interleaved.tobytes()
    compression = 8 if compress else 1
    body = zlib.compress(payload, 6) if compress else payload

    entries: List[Tuple[int, int, int, object]] = []
    entries.append((T_WIDTH, 4, 1, [width]))
    entries.append((T_LENGTH, 4, 1, [height]))
    entries.append((T_BITS, 3, bands, [bits] * bands))
    entries.append((T_COMPRESSION, 3, 1, [compression]))
    entries.append((T_PHOTOMETRIC, 3, 1, [1]))
    entries.append((T_SAMPLES, 3, 1, [bands]))
    entries.append((T_ROWS_PER_STRIP, 4, 1, [height]))
    entries.append((T_PLANAR, 3, 1, [1]))
    entries.append((T_SAMPLE_FORMAT, 3, bands, [sample_format] * bands))
    entries.append((T_MODEL_PIXEL_SCALE, 12, 3, [abs(grid.dx), abs(grid.dy), 0.0]))
    entries.append(
        (T_MODEL_TIEPOINT, 12, 6, [0.0, 0.0, 0.0, grid.origin_x, grid.origin_y, 0.0])
    )

    geo_keys, ascii_params = _geokeys_for(epsg, grid)
    entries.append((T_GEO_KEYS, 3, len(geo_keys), geo_keys))
    if ascii_params:
        entries.append((T_GEO_ASCII, 2, len(ascii_params) + 1, ascii_params))
    if nodata is not None:
        s = repr(float(nodata))
        entries.append((T_GDAL_NODATA, 2, len(s) + 1, s))

    # strip offset is patched once the layout is known; it always fits inline
    entries.append((T_STRIP_OFFSETS, 4, 1, [0]))
    entries.append((T_STRIP_BYTES, 4, 1, [len(body)]))
    entries.sort(key=lambda e: e[0])

    header = b"II" + struct.pack("<HI", 42, 8)
    ifd_size = 2 + len(entries) * 12 + 4
    extra_start = len(header) + ifd_size

    blobs: List[bytes] = []
    cursor = extra_start
    packed: List[bytes] = []

    for tag, typ, count, value in entries:
        fmt = {1: "B", 2: "s", 3: "H", 4: "I", 12: "d"}[typ]
        if typ == 2:
            raw = value.encode("ascii") + b"\x00"
        else:
            raw = struct.pack("<" + fmt * count, *value)
        if len(raw) <= 4:
            field = raw.ljust(4, b"\x00")
        else:
            blobs.append(raw)
            field = struct.pack("<I", cursor)
            cursor += len(raw) + (len(raw) % 2)
        packed.append(struct.pack("<HHI", tag, typ, count) + field)

    body_offset = cursor
    for i, (tag, typ, count, _v) in enumerate(entries):
        if tag == T_STRIP_OFFSETS:
            packed[i] = struct.pack("<HHI", tag, typ, count) + struct.pack(
                "<I", body_offset
            )

    ifd = struct.pack("<H", len(entries)) + b"".join(packed) + struct.pack("<I", 0)

    with open(path, "wb") as f:
        f.write(header)
        f.write(ifd)
        for blob in blobs:
            f.write(blob)
            if len(blob) % 2:
                f.write(b"\x00")
        if f.tell() != body_offset:
            raise TiffError(f"internal layout error: {f.tell()} != {body_offset}")
        f.write(body)


def _geokeys_for(epsg: Optional[int], grid: RasterGrid) -> Tuple[List[int], str]:
    """Minimal GeoKey directory: geographic, projected or sinusoidal."""
    if grid.crs == SINUSOIDAL or epsg is None and grid.crs == SINUSOIDAL:
        keys = [1, 1, 0, 4]
        keys += [1024, 0, 1, 1]        # GTModelType = projected
        keys += [1025, 0, 1, 1]        # RasterPixelIsArea
        keys += [3072, 0, 1, 32767]    # user defined
        keys += [3075, 0, 1, 24]       # CT_Sinusoidal
        keys[3] = 4
        return keys, "Sinusoidal MODIS sphere 6371007.181|"
    code = int(epsg or 4326)
    if code == 4326:
        keys = [1, 1, 0, 3]
        keys += [1024, 0, 1, 2]        # geographic
        keys += [1025, 0, 1, 1]
        keys += [2048, 0, 1, 4326]
        return keys, "WGS 84|"
    keys = [1, 1, 0, 3]
    keys += [1024, 0, 1, 1]            # projected
    keys += [1025, 0, 1, 1]
    keys += [3072, 0, 1, code]
    return keys, f"EPSG:{code}|"
