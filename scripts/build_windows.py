"""Build a portable Windows x64 folder; run with requirements-windows installed."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
ASSETS = {
    "mihomo.zip": ("https://github.com/MetaCubeX/mihomo/releases/download/v1.19.15/mihomo-windows-amd64-compatible-v1.19.15.zip", "e9251191e459816c1aca55d495eabd50d525e83aff0cd445608d69da7e0f470c"),
    "Country.mmdb": ("https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/country.mmdb", "9840946762fa8c2673738bbbbab22bb720bde931f6dea327eb58e689babc37dc"),
    "geoip.dat": ("https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.dat", "d4ec4c5e5d5a38c1399964ea9fd8c94288426a3b31b483904b67819fc07f4466"),
    "geoip.metadb": ("https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.metadb", "1669d9ca839829ca376cf7dc54987c7a21abd8d26581d8adbbf3f03e8d95b34f"),
    "geosite.dat": ("https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geosite.dat", "7772be22495e316b22bec388b3ae4b3f3869fcb09cc1912c4bedc4095ca1572c"),
}


def download(url, target, digest=None):
    if target.exists() and (not digest or hashlib.sha256(target.read_bytes()).hexdigest() == digest):
        return
    print("Downloading", target.name, flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "ClashMerge-builder"})
    with urllib.request.urlopen(req, timeout=120) as response:
        data = response.read()
    if digest and hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError(f"{target.name}: SHA256 mismatch; verify the official release before updating ASSETS")
    target.write_bytes(data)


def prepare_vendor():
    VENDOR.mkdir(exist_ok=True)
    cache = ROOT / "data" / "windows-assets"
    cache.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, (url, digest) in ASSETS.items():
        target = cache / name
        download(url, target, digest)
        manifest[name] = {"url": url, "sha256": digest}
        if name == "mihomo.zip":
            with zipfile.ZipFile(target) as archive:
                exe = next(n for n in archive.namelist() if n.endswith(".exe"))
                (VENDOR / "clash-merge-mihomo.exe").write_bytes(archive.read(exe))
        else:
            shutil.copyfile(target, VENDOR / name)
    download("https://raw.githubusercontent.com/MetaCubeX/mihomo/v1.19.15/LICENSE", VENDOR / "MIHOMO-LICENSE")
    (VENDOR / "asset-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def build():
    if sys.platform != "win32":
        raise SystemExit("Build the Windows executable on Windows x64")
    for name in ("clash-merge-mihomo.exe", "geosite.dat", "geoip.dat", "Country.mmdb", "geoip.metadb", "MIHOMO-LICENSE"):
        if not (VENDOR / name).is_file():
            raise SystemExit(f"Missing vendor/{name}; run without --skip-download first")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
                    "--name", "ClashMerge", "--specpath", "build", "--add-data", str(ROOT / "app/static") + ";app/static",
                    "--add-data", str(VENDOR) + ";vendor", "--hidden-import", "pystray._win32",
                    "--collect-submodules", "uvicorn", "desktop.py"], cwd=ROOT, check=True)
    output = ROOT / "dist" / "ClashMerge"
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
    archive = shutil.make_archive(str(ROOT / "dist" / "ClashMerge-Windows-x64"), "zip", ROOT / "dist", "ClashMerge")
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive + ".sha256").write_text(digest + "  " + Path(archive).name + "\n", encoding="ascii")
    print("Built:", archive, "SHA256:", digest, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    if not args.skip_download:
        prepare_vendor()
    if not args.download_only:
        build()
