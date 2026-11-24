"""Build a portable Windows x64 folder; run with requirements-windows installed.

Asset integrity (review item 21):
- mihomo and its LICENSE come from a fixed release tag and are pinned by SHA256.
- geodata comes from meta-rules-dat, which only publishes a moving ``latest``
  release (the release branch is force-pushed, no history to pin). Each file
  is verified against the ``.sha256sum`` published next to it in the same
  release; the digest actually used is recorded in vendor/asset-manifest.json.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
CACHE = ROOT / "data" / "windows-assets"
MIHOMO_VERSION = "v1.19.15"
# name -> (url, sha256): fixed tags, pinned digests.
PINNED = {
    "mihomo.zip": (f"https://github.com/MetaCubeX/mihomo/releases/download/{MIHOMO_VERSION}/"
                   f"mihomo-windows-amd64-compatible-{MIHOMO_VERSION}.zip",
                   "e9251191e459816c1aca55d495eabd50d525e83aff0cd445608d69da7e0f470c"),
    "MIHOMO-LICENSE": (f"https://raw.githubusercontent.com/MetaCubeX/mihomo/{MIHOMO_VERSION}/LICENSE",
                       "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"),
}
GEODATA_BASE = "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/"
# local name -> upstream name; verified against <upstream>.sha256sum.
GEODATA = {
    "Country.mmdb": "country.mmdb",
    "geoip.dat": "geoip.dat",
    "geoip.metadb": "geoip.metadb",
    "geosite.dat": "geosite.dat",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ClashMerge-builder"})
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read()


def parse_sumfile(text: str, upstream: str) -> str:
    """``<64 hex>  <name>`` (sha256sum format); the name, when present,
    must be the file it is published for."""
    m = re.fullmatch(r"\s*([0-9a-fA-F]{64})(?:\s+\*?(\S+))?\s*", text)
    if not m or (m.group(2) and Path(m.group(2)).name != upstream):
        raise RuntimeError(f"{upstream}.sha256sum: unexpected content {text[:120]!r}")
    return m.group(1).lower()


def download_pinned(name: str, url: str, digest: str) -> Path:
    target = CACHE / name
    if target.is_file() and sha256(target.read_bytes()) == digest:
        return target
    print("Downloading", name, flush=True)
    data = fetch(url)
    actual = sha256(data)
    if actual != digest:
        raise RuntimeError(f"{name}: SHA256 mismatch (expected {digest}, got {actual}); "
                           "verify the official release before updating PINNED")
    target.write_bytes(data)
    return target


def download_geodata(name: str, upstream: str) -> tuple[Path, str]:
    """Verify against the upstream sumfile. ``latest`` can be republished
    between the two requests, so a mismatch is retried once with a fresh
    sumfile before failing."""
    target = CACHE / name
    for attempt in range(2):
        expected = parse_sumfile(fetch(GEODATA_BASE + upstream + ".sha256sum").decode("ascii", "replace"),
                                 upstream)
        if target.is_file() and sha256(target.read_bytes()) == expected:
            return target, expected
        print("Downloading", name, flush=True)
        data = fetch(GEODATA_BASE + upstream)
        actual = sha256(data)
        if actual == expected:
            target.write_bytes(data)
            return target, expected
        if attempt == 0:
            print(f"{name}: SHA256 mismatch, upstream may have just republished; retrying", flush=True)
            time.sleep(5)
    raise RuntimeError(f"{name}: SHA256 mismatch against upstream {upstream}.sha256sum "
                       f"(expected {expected}, got {actual})")


def prepare_vendor():
    VENDOR.mkdir(exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, (url, digest) in PINNED.items():
        target = download_pinned(name, url, digest)
        manifest[name] = {"url": url, "sha256": digest, "verified_by": "pinned"}
        if name == "mihomo.zip":
            with zipfile.ZipFile(target) as archive:
                exe = next(n for n in archive.namelist() if n.endswith(".exe"))
                (VENDOR / "clash-merge-mihomo.exe").write_bytes(archive.read(exe))
        else:
            shutil.copyfile(target, VENDOR / name)
    for name, upstream in GEODATA.items():
        target, digest = download_geodata(name, upstream)
        shutil.copyfile(target, VENDOR / name)
        manifest[name] = {"url": GEODATA_BASE + upstream, "sha256": digest,
                          "verified_by": GEODATA_BASE + upstream + ".sha256sum",
                          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (VENDOR / "asset-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def build(dist_dir=None):
    dist_dir = Path(dist_dir).resolve() if dist_dir else ROOT / "dist"
    if sys.platform != "win32":
        raise SystemExit("Build the Windows executable on Windows x64")
    for name in ("clash-merge-mihomo.exe", "geosite.dat", "geoip.dat", "Country.mmdb", "geoip.metadb", "MIHOMO-LICENSE"):
        if not (VENDOR / name).is_file():
            raise SystemExit(f"Missing vendor/{name}; run without --skip-download first")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
                    "--distpath", str(dist_dir),
                    "--name", "ClashMerge", "--specpath", "build", "--add-data", str(ROOT / "app/static") + ";app/static",
                    "--add-data", str(VENDOR) + ";vendor", "--hidden-import", "pystray._win32",
                    "--collect-submodules", "uvicorn", "desktop.py"], cwd=ROOT, check=True)
    output = dist_dir / "ClashMerge"
    shutil.copyfile(ROOT / "WINDOWS.md", output / "Windows使用说明.md")
    shutil.copyfile(ROOT / "THIRD_PARTY.md", output / "THIRD_PARTY.md")
    licenses = output / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(VENDOR / "MIHOMO-LICENSE", licenses / "MIHOMO-LICENSE")
    # Include installed dependency license texts and version inventory.
    inventory = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        inventory.append(f"{name}=={dist.version}")
        for entry in dist.files or []:
            if any(word in str(entry).lower() for word in ("license", "copying")) and ".dist-info/" in str(entry).replace("\\", "/"):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    shutil.copyfile(source, licenses / (name + "-" + str(entry).replace("/", "_").replace("\\", "_")))
    (licenses / "versions.txt").write_text("\n".join(sorted(inventory)), encoding="utf-8")
    archive = shutil.make_archive(str(dist_dir / "ClashMerge-Windows-x64"), "zip", dist_dir, "ClashMerge")
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive + ".sha256").write_text(digest + "  " + Path(archive).name + "\n", encoding="ascii")
    print("Built:", archive, "SHA256:", digest, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--dist-dir", help="Build into a separate directory when an older EXE is locked")
    args = parser.parse_args()
    if not args.skip_download:
        prepare_vendor()
    if not args.download_only:
        build(args.dist_dir)
