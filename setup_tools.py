#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIO public-safe standalone setup for Termux.

This version intentionally does NOT hardcode private repository names,
private resource filenames, private signing-key URLs, or protector internals.

Usage:
  python aio_setup_tools_public_safe.py
  python aio_setup_tools_public_safe.py --verify-only
  python aio_setup_tools_public_safe.py --minimal
  python aio_setup_tools_public_safe.py --manifest-file ./aio_setup_manifest.json
  python aio_setup_tools_public_safe.py --manifest-url https://example.com/aio_setup_manifest.json

Optional manifest format:
{
  "downloads": [
    {"name":"apksigner.jar", "url":"https://...", "dst":"$PREFIX/bin/apksigner.jar", "min_size":51200},
    {"name":"resource.zip", "url":"https://...", "dst":"$HOME/toolkit/resource.zip", "min_size":1, "required":false}
  ]
}

Notes:
  - clang.zip is never required by default. Use the Termux clang package instead.
  - Any manifest item can be optional with {"required": false}.
"""

import os
import sys
import json
import time
import shlex
import shutil
import zipfile
import urllib.request
import subprocess
from pathlib import Path

BOLD="\033[1m"; RESET="\033[0m"; RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"; BLUE="\033[94m"; CYAN="\033[96m"
def c(t, col=""): return f"{col}{BOLD}{t}{RESET}" if col else str(t)
def info(m): print(c("i INFO", CYAN), m)
def ok(m): print(c("✓ OK  ", GREEN), m)
def warn(m): print(c("! WARN", YELLOW), m)
def err(m): print(c("× ERR ", RED), m)

HOME = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
PREFIX = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
BIN_PATH = PREFIX / "bin"
SHARE_PATH = PREFIX / "share"
TOOLKIT_DIR = Path(os.environ.get("AIO_TOOLKIT_DIR", str(HOME / "toolkit"))).expanduser().resolve()
LOG_DIR = HOME / "aio_setup_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"aio_setup_public_{int(time.time())}.log"
MARKER = SHARE_PATH / ".aio_public_setup_ready"

CORE_PACKAGES = ["python", "aapt", "android-tools", "clang", "binutils", "p7zip", "zip", "unzip", "curl"]
OPTIONAL_PACKAGES = ["radare2"]
JAVA_PACKAGES = ["openjdk-21", "openjdk-17", "openjdk-11"]
PY_MODULES = ["requests", "certifi", "pycryptodome", "r2pipe"]

# Public upstream tools only. Private toolkit resources must be supplied by manifest.
PUBLIC_DOWNLOADS = [
    {"name":"ManifestEditor_qomg.jar", "url":"https://github.com/qomg/AndroidManifestEditor/releases/download/v1.0.2/ManifestEditor-1.0.2.jar", "dst":"$PREFIX/bin/ManifestEditor_qomg.jar", "min_size":51200},
    {"name":"smali.jar", "url":"https://bitbucket.org/JesusFreke/smali/downloads/smali-2.5.2.jar", "dst":"$PREFIX/bin/smali.jar", "min_size":51200},
    {"name":"baksmali.jar", "url":"https://bitbucket.org/JesusFreke/smali/downloads/baksmali-2.5.2.jar", "dst":"$PREFIX/bin/baksmali.jar", "min_size":51200},
]

TOOL_GROUPS_CORE = {
    "java": ["java"], "zip": ["zip"], "unzip": ["unzip"], "7z": ["7z"],
    "curl": ["curl"], "aapt": ["aapt"], "zipalign": ["zipalign"],
    "clang": ["clang"], "readelf": ["llvm-readelf", "readelf"], "strip": ["llvm-strip", "strip"],
    "smali": ["smaliwill"], "baksmali": ["baksmaliwill"],
}
TOOL_GROUPS_OPTIONAL = {"radare2": ["r2", "radare2"], "jadx": ["jadx"]}

def log_write(text):
    with LOG_FILE.open("a", encoding="utf-8", errors="ignore") as f:
        f.write(str(text).rstrip("\n") + "\n")

def run_cmd(cmd, timeout=900, quiet=False):
    shown = " ".join(shlex.quote(str(x)) for x in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
    log_write("\n$ " + shown)
    if not quiet: info(shown)
    try:
        p = subprocess.run(cmd, shell=not isinstance(cmd, (list, tuple)), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=timeout)
        log_write(p.stdout or "")
        log_write(f"[exit={p.returncode}]")
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n[TIMEOUT]"
        log_write(out)
        return 124, out
    except Exception as e:
        log_write(f"[EXCEPTION] {type(e).__name__}: {e}")
        return 1, str(e)

def which_any(names): return next((x for x in names if shutil.which(x)), None)
def file_ok(path, min_size=1):
    try: return Path(path).exists() and Path(path).stat().st_size >= int(min_size)
    except Exception: return False

def expand_path(s):
    return Path(str(s).replace("$PREFIX", str(PREFIX)).replace("$HOME", str(HOME)).replace("$TOOLKIT_DIR", str(TOOLKIT_DIR))).expanduser()

def is_required_download(d):
    """
    Manifest resources are required by default, except clang.zip.

    Public releases should not require a private clang.zip archive; native compilation
    uses the Termux clang package instead. Manifest authors can still force any
    resource to be required with {"required": true}, or mark normal resources
    optional with {"required": false}.
    """
    try:
        req = d.get("required", None)
        if isinstance(req, str):
            req = req.strip().lower() not in ("0", "false", "no", "optional")
        if req is not None:
            return bool(req)
        name = str(d.get("name", "") or "").lower()
        dst = str(d.get("dst", "") or "").lower()
        if name == "clang.zip" or dst.endswith("/clang.zip") or dst.endswith("\\clang.zip"):
            return False
    except Exception:
        pass
    return True

def download_file(url, dst, min_size=1, mode=None):
    dst = expand_path(dst)
    if file_ok(dst, min_size):
        ok(f"Ada: {dst.name}")
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    try:
        if tmp.exists(): tmp.unlink()
    except Exception: pass
    info(f"Download {dst.name}")
    curl = shutil.which("curl")
    if curl:
        rc, _ = run_cmd([curl, "-fL", "--retry", "3", "--connect-timeout", "20", "-o", str(tmp), str(url)], timeout=420, quiet=True)
        success = rc == 0 and file_ok(tmp, min_size)
    else:
        success = False
        try:
            req = urllib.request.Request(str(url), headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=90) as res, tmp.open("wb") as out:
                shutil.copyfileobj(res, out)
            success = file_ok(tmp, min_size)
        except Exception as e:
            log_write(f"download error {dst.name}: {type(e).__name__}: {e}")
    if not success:
        try:
            if tmp.exists(): tmp.unlink()
        except Exception: pass
        err(f"Gagal download: {dst.name}")
        return False
    tmp.replace(dst)
    if mode is not None: os.chmod(dst, mode)
    ok(f"Downloaded: {dst.name}")
    return True

def pkg_install(packages):
    failed=[]
    for pkg in packages:
        rc,_=run_cmd(["pkg","install","-y",pkg], timeout=900, quiet=True)
        if rc==0: ok(f"pkg: {pkg}")
        else: failed.append(pkg); warn(f"pkg gagal: {pkg}")
    return failed

def pip_install(modules):
    pip = shutil.which("pip") or shutil.which("pip3")
    if not pip: return modules[:]
    failed=[]
    for mod in modules:
        rc,_=run_cmd([pip,"install","--upgrade",mod], timeout=900, quiet=True)
        if rc==0: ok(f"pip: {mod}")
        else: failed.append(mod); warn(f"pip gagal: {mod}")
    return failed

def ensure_java():
    if shutil.which("java"):
        ok("java tersedia")
        return True
    for pkg in JAVA_PACKAGES:
        pkg_install([pkg])
        if shutil.which("java"):
            ok(f"java tersedia dari {pkg}")
            return True
    return False

def ensure_wrappers():
    ok_all=True
    for wrapper, jar in {"smaliwill": BIN_PATH/"smali.jar", "baksmaliwill": BIN_PATH/"baksmali.jar"}.items():
        out=BIN_PATH/wrapper
        if not file_ok(jar, 51200):
            warn(f"Jar belum ada: {jar.name}"); ok_all=False; continue
        out.write_text(f'#!/data/data/com.termux/files/usr/bin/sh\nexec java -jar "{jar}" "$@"\n', encoding="utf-8")
        os.chmod(out, 0o755)
        ok(f"wrapper: {wrapper}")
    return ok_all

def safe_extract_zip(zip_path, dst_dir):
    zip_path=Path(zip_path); dst_dir=Path(dst_dir); dst_real=dst_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as z:
        for m in z.infolist():
            target=(dst_dir/m.filename).resolve()
            if not str(target).startswith(str(dst_real)+os.sep) and target != dst_real:
                raise RuntimeError(f"unsafe zip entry: {m.filename}")
        z.extractall(dst_dir)

def ensure_jadx(skip=False):
    if skip: warn("skip jadx (--minimal)"); return True
    if shutil.which("jadx"): ok("jadx tersedia"); return True
    pkg_install(["jadx"])
    if shutil.which("jadx"): return True
    try:
        api="https://api.github.com/repos/skylot/jadx/releases/latest"
        req=urllib.request.Request(api, headers={"User-Agent":"Mozilla/5.0", "Accept":"application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=45) as res:
            data=json.loads(res.read().decode("utf-8","ignore"))
        asset_url=""
        for asset in data.get("assets",[]):
            name=str(asset.get("name","")).lower(); url=str(asset.get("browser_download_url", ""))
            if name.startswith("jadx-") and name.endswith(".zip") and "gui" not in name and url:
                asset_url=url; break
        if not asset_url: return False
        zpath=BIN_PATH/"jadx-cli.zip"; dpath=BIN_PATH/"jadx-cli"
        if not download_file(asset_url, zpath, 500*1024): return False
        if dpath.exists(): shutil.rmtree(dpath, ignore_errors=True)
        dpath.mkdir(parents=True, exist_ok=True)
        safe_extract_zip(zpath, dpath)
        jadx_bin=None
        for root,_,files in os.walk(dpath):
            if "jadx" in files and Path(root).name == "bin":
                jadx_bin=Path(root)/"jadx"; break
        if not jadx_bin: return False
        os.chmod(jadx_bin, 0o755)
        wrapper=BIN_PATH/"jadx"
        wrapper.write_text(f'#!/data/data/com.termux/files/usr/bin/sh\nexec "{jadx_bin}" "$@"\n', encoding="utf-8")
        os.chmod(wrapper, 0o755)
        ok("jadx wrapper dibuat")
        return True
    except Exception as e:
        log_write(f"jadx fallback error: {type(e).__name__}: {e}")
        warn("JADX fallback gagal")
        return False

def load_manifest(args):
    downloads=[]
    if "--manifest-file" in args:
        i=args.index("--manifest-file")
        if i+1 < len(args):
            data=json.loads(Path(args[i+1]).read_text(encoding="utf-8"))
            downloads.extend(data.get("downloads", []))
    if "--manifest-url" in args:
        i=args.index("--manifest-url")
        if i+1 < len(args):
            req=urllib.request.Request(args[i+1], headers={"User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as res:
                data=json.loads(res.read().decode("utf-8","ignore"))
            downloads.extend(data.get("downloads", []))
    env_manifest=os.environ.get("AIO_SETUP_MANIFEST", "").strip()
    if env_manifest and Path(env_manifest).exists():
        data=json.loads(Path(env_manifest).read_text(encoding="utf-8"))
        downloads.extend(data.get("downloads", []))
    return downloads

def verify(minimal=False, manifest_downloads=None):
    manifest_downloads = manifest_downloads or []
    missing_tools=[]
    groups=dict(TOOL_GROUPS_CORE)
    if not minimal: groups.update(TOOL_GROUPS_OPTIONAL)
    for label,names in groups.items():
        if not which_any(names): missing_tools.append(label)
    missing_files=[]
    for d in PUBLIC_DOWNLOADS + manifest_downloads:
        if not is_required_download(d):
            continue
        if not file_ok(expand_path(d.get("dst", "")), int(d.get("min_size", 1))):
            missing_files.append(d.get("name") or Path(str(d.get("dst", ""))).name)
    return missing_tools, missing_files

def print_summary(missing_tools, missing_files):
    print("\n" + c("═"*60, BLUE))
    if not missing_tools and not missing_files:
        ok("Semua dependency yang dideklarasikan sudah siap.")
        SHARE_PATH.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(str(int(time.time())), encoding="utf-8")
        try: os.chmod(MARKER, 0o600)
        except Exception: pass
    else:
        warn("Masih ada dependency yang belum lengkap.")
        if missing_tools:
            print(c("Missing tools:", YELLOW)); [print("  -", x) for x in missing_tools]
        if missing_files:
            print(c("Missing files/resources:", YELLOW)); [print("  -", x) for x in missing_files]
    print(c("Log detail:", CYAN), LOG_FILE)
    print(c("Toolkit dir:", CYAN), TOOLKIT_DIR)
    print(c("═"*60, BLUE))

def main():
    args=sys.argv[1:]
    aset=set(args)
    verify_only="--verify-only" in aset
    minimal="--minimal" in aset
    force="--force" in aset
    no_public_downloads="--no-public-downloads" in aset
    manifest_downloads=load_manifest(args)
    print(c("AIO Public-Safe Setup Tools", BLUE))
    print("Log:", LOG_FILE)
    print("PREFIX:", PREFIX)
    print("TOOLKIT_DIR:", TOOLKIT_DIR)
    if not manifest_downloads:
        warn("Tidak ada manifest private. Hanya public upstream tools yang akan dipasang.")
    if MARKER.exists() and not force and not verify_only:
        mt,mf=verify(minimal=minimal, manifest_downloads=manifest_downloads if not no_public_downloads else manifest_downloads)
        if not mt and not mf:
            print_summary(mt,mf); return 0
    if verify_only:
        mt,mf=verify(minimal=minimal, manifest_downloads=manifest_downloads)
        print_summary(mt,mf); return 0 if not mt and not mf else 2
    run_cmd(["pkg","update","-y"], timeout=900, quiet=True)
    info("Install paket utama"); pkg_install(CORE_PACKAGES)
    if not minimal:
        info("Install paket optional"); pkg_install(OPTIONAL_PACKAGES)
    info("Cek Java"); ensure_java()
    info("Install Python modules"); pip_install(PY_MODULES)
    if not no_public_downloads:
        info("Download public upstream tools")
        for d in PUBLIC_DOWNLOADS:
            download_file(d["url"], d["dst"], int(d.get("min_size", 1)))
    if manifest_downloads:
        info("Download manifest-declared private/public resources")
        for d in manifest_downloads:
            success = download_file(d["url"], d["dst"], int(d.get("min_size", 1)))
            if not success and not is_required_download(d):
                warn(f"optional resource skipped: {d.get('name') or d.get('dst')}")
    info("Buat wrapper smaliwill/baksmaliwill"); ensure_wrappers()
    info("Setup JADX"); ensure_jadx(skip=minimal)
    mt,mf=verify(minimal=minimal, manifest_downloads=manifest_downloads)
    print_summary(mt,mf)
    return 0 if not mt and not mf else 2

if __name__ == "__main__":
    raise SystemExit(main())
