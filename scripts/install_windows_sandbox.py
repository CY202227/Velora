"""Install a pinned public native runtime from a verified archive; never run setup."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "1.0.1"
URL = f"https://registry.npmjs.org/@microsoft/mxc-sdk/-/mxc-sdk-{VERSION}.tgz"
INTEGRITY = "2cP925G5+lQyKzjBVha8uNtVd7XESgDBtgi1yCYYSo9M3xiFyZWtCO4SWPGTk509rdI6nqjyGcZxvEP3t6l8Hg=="
ROOT = Path(__file__).resolve().parents[1]


def install(archive: Path, destination: Path, arch: str) -> Path:
    with archive.open("rb") as stream:
        digest = base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()
    if digest != INTEGRITY:
        raise ValueError("native runtime archive integrity mismatch")
    prefix = f"package/bin/{arch}/"
    hashes = {}
    with tarfile.open(archive) as bundle:
        for item in bundle.getmembers():
            if item.name in ("package/LICENSE.md", "package/package.json"):
                relative = Path(item.name).name
            elif item.name.startswith(prefix) and "/" not in item.name[len(prefix):]:
                relative = item.name[len(prefix):]
            else:
                continue
            if not item.isfile() or not relative or relative in (".", ".."):
                raise ValueError("unexpected native runtime archive member")
            data = bundle.extractfile(item).read()
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            hashes[relative] = hashlib.sha256(data).hexdigest()
    executable = destination / "wxc-exec.exe"
    if not executable.is_file():
        raise ValueError("native executor missing from archive")
    (destination / "installation.json").write_text(json.dumps({
        "version": VERSION, "source": URL, "archive_sha512": INTEGRITY, "files": hashes,
    }, indent=2), encoding="utf-8")
    return executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--arch", choices=("x64", "arm64"), default="arm64" if platform.machine().lower() == "arm64" else "x64")
    args = parser.parse_args()
    archive = args.archive
    if archive is None:
        if not args.download:
            parser.error("choose --archive or explicitly enable --download")
        archive = ROOT / "velora_data" / "sandbox-runtime" / f"mxc-{VERSION}.tgz"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    destination = ROOT / "velora_data" / "sandbox-runtime" / "windows" / VERSION / args.arch
    print(install(archive, destination, args.arch))


if __name__ == "__main__":
    main()
