#!/usr/bin/env python3
"""Fetch the case dataset (data/) from the organisers' public Yandex Disk folder.

Used by the Dockerfile so the deployment archive stays small: the ~44 MB of
GeoTIFFs are pulled by the build server instead of being uploaded.  Every file
is checked against the SHA-256 in data/file_catalog.csv; a mismatch aborts.
Standard library only.

    python3 tools/fetch_data.py [public_key] [dest_dir]
"""
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

PUBLIC_KEY = "https://disk.yandex.ru/d/N_r5CnqER_HqQA"
ZIP_PATH = "/data.zip"
API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(public_key: str, out: str, attempts: int = 6) -> None:
    """Download with resume: the link is re-requested and the transfer continues."""
    for attempt in range(1, attempts + 1):
        try:
            q = urllib.parse.urlencode({"public_key": public_key, "path": ZIP_PATH})
            with urllib.request.urlopen(f"{API}?{q}", timeout=60) as r:
                href = json.load(r)["href"]
            have = os.path.getsize(out) if os.path.exists(out) else 0
            req = urllib.request.Request(href, headers={"Range": f"bytes={have}-"} if have else {})
            with urllib.request.urlopen(req, timeout=60) as r, open(out, "ab" if have and r.status == 206 else "wb") as f:
                shutil.copyfileobj(r, f)
            return
        except Exception as exc:  # network errors are retried, anything else surfaces on the last try
            print(f"download attempt {attempt}/{attempts} failed: {exc}", file=sys.stderr)
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)


def main(public_key: str, dest: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "data.zip")
        download(public_key, archive)
        src = os.path.join(tmp, "data")
        with zipfile.ZipFile(archive) as z:
            z.extractall(src)
        with open(os.path.join(src, "file_catalog.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        bad = [r["relative_path"] for r in rows
               if sha256(os.path.join(src, r["relative_path"])) != r["sha256"]]
        if bad:
            print(f"checksum mismatch in {len(bad)} files, e.g. {bad[:3]}", file=sys.stderr)
            return 1
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    print(f"data OK: {len(rows)} files verified -> {dest}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(main(args[0] if args else PUBLIC_KEY, args[1] if len(args) > 1 else "data"))
