#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIO public-safe standalone setup for Termux.

Fixed v4: preserves legacy base call `java -jar $PREFIX/bin/apksigner.jar` by installing a real compatible apksigner.jar.

This version intentionally does NOT hardcode private repository names,
private resource filenames, private signing-key URLs, or protector internals.

Usage:
  python aio_setup_tools_public_safe_v3_apksigner_fix.py
  python aio_setup_tools_public_safe_v3_apksigner_fix.py --verify-only
  python aio_setup_tools_public_safe_v3_apksigner_fix.py --minimal
  python aio_setup_tools_public_safe_v3_apksigner_fix.py --manifest-file ./aio_setup_manifest.json
  python aio_setup_tools_public_safe_v3_apksigner_fix.py --manifest-url https://example.com/aio_setup_manifest.json

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
  - apksigner is installed as $PREFIX/bin/apksigner.jar so old/base script calls keep working.
"""

import os
import sys
import json
import time
import shlex
import shutil
import zipfile
import tempfile
import stat
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

APKSIGNER_OPT = PREFIX / "opt" / "aio-apksigner-sdk"
APKSIGNER_LIB = APKSIGNER_OPT / "lib"
APKSIGNER_WRAPPER = BIN_PATH / "aio-apksigner-sdk"
APKSIGNER_JAR = BIN_PATH / "apksigner.jar"
BUILD_TOOLS_URLS = [
    "https://dl.google.com/android/repository/build-tools_r35_linux.zip",
    "https://dl.google.com/android/repository/build-tools_r34_linux.zip",
    "https://dl.google.com/android/repository/build-tools_r33_linux.zip",
]

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
    "apksigner.jar": ["apksigner.jar"],
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


def probe_apksigner_sdk(path=APKSIGNER_WRAPPER):
    """Return True only for Android SDK apksigner CLI, not Termux lightweight apksigner."""
    try:
        path = Path(path)
        if not path.exists():
            return False
        p = subprocess.run([str(path), "sign", "--help"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=25)
        out = p.stdout or ""
        return ("--ks" in out and "--v2-signing-enabled" in out)
    except Exception as e:
        log_write(f"probe_apksigner_sdk error: {type(e).__name__}: {e}")
        return False



def probe_legacy_apksigner_jar(path=APKSIGNER_JAR):
    """Return True when the old/base call `java -jar apksigner.jar` works."""
    try:
        path = Path(path)
        if not path.exists() or path.stat().st_size < 51200:
            return False
        if not shutil.which("java"):
            return False
        p = subprocess.run(["java", "-jar", str(path), "sign", "--help"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=35)
        out = p.stdout or ""
        return ("--ks" in out and "--v2-signing-enabled" in out)
    except Exception as e:
        log_write(f"probe_legacy_apksigner_jar error: {type(e).__name__}: {e}")
        return False


def create_legacy_apksigner_fat_jar(lib_dir):
    """
    Build a self-contained $PREFIX/bin/apksigner.jar from Android SDK build-tools jars.

    This intentionally preserves the base protector call style:
        java -jar $PREFIX/bin/apksigner.jar ...

    We do NOT create a shell masquerading as a jar. We create a real executable jar
    with Main-Class com.android.apksigner.ApkSignerTool and merged dependencies.
    """
    lib_dir = Path(lib_dir)
    jars = []
    main = lib_dir / "apksigner.jar"
    if main.is_file():
        jars.append(main)
    for j in sorted(lib_dir.glob("*.jar")):
        if j not in jars:
            jars.append(j)
    if not jars:
        raise RuntimeError(f"no apksigner SDK jars found in {lib_dir}")

    BIN_PATH.mkdir(parents=True, exist_ok=True)
    tmp = APKSIGNER_JAR.with_suffix(".jar.tmp")
    if tmp.exists():
        try: tmp.unlink()
        except Exception: pass

    manifest = (
        "Manifest-Version: 1.0\r\n"
        "Main-Class: com.android.apksigner.ApkSignerTool\r\n"
        "Created-By: AIO setup legacy apksigner compatibility\r\n"
        "\r\n"
    ).encode("utf-8")

    skip_ext = (".SF", ".RSA", ".DSA", ".EC")
    seen = {"META-INF/MANIFEST.MF"}
    copied = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("META-INF/MANIFEST.MF", manifest)
        for jar in jars:
            try:
                with zipfile.ZipFile(jar, "r") as zin:
                    for info_z in zin.infolist():
                        name = info_z.filename.replace("\\", "/")
                        upper = name.upper()
                        if not name or name.endswith("/"):
                            continue
                        if upper == "META-INF/MANIFEST.MF":
                            continue
                        if upper.startswith("META-INF/") and upper.endswith(skip_ext):
                            continue
                        if name in seen:
                            continue
                        data = zin.read(info_z.filename)
                        zi = zipfile.ZipInfo(name)
                        zi.date_time = (1980, 1, 1, 0, 0, 0)
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        out.writestr(zi, data)
                        seen.add(name)
                        copied += 1
            except Exception as e:
                log_write(f"merge jar error {jar}: {type(e).__name__}: {e}")
    tmp.replace(APKSIGNER_JAR)
    os.chmod(APKSIGNER_JAR, 0o644)
    ok(f"legacy apksigner.jar dibuat: {APKSIGNER_JAR.name} ({copied} entries)")
    return APKSIGNER_JAR


def ensure_legacy_apksigner_jar(download_sdk=True):
    """
    Ensure the old/base apksigner invocation works:
        java -jar $PREFIX/bin/apksigner.jar ...

    This is the Android 16/Termux compatibility fix: the protector may keep calling
    apksigner.jar exactly like before, while setup guarantees the jar exists and is executable.
    """
    if probe_legacy_apksigner_jar(APKSIGNER_JAR):
        ok("legacy apksigner.jar tersedia")
        return True

    # If user has a real SDK apksigner lib dir, build the compat jar from it.
    lib_dir = find_existing_apksigner_lib_dir()
    if not lib_dir and download_sdk:
        lib_dir = download_and_install_buildtools_apksigner()
    if lib_dir:
        create_legacy_apksigner_fat_jar(lib_dir)
        if probe_legacy_apksigner_jar(APKSIGNER_JAR):
            ok("legacy apksigner.jar siap untuk base script")
            return True

    warn("apksigner.jar belum siap. Jalankan setup dengan internet aktif atau set ANDROID_HOME/ANDROID_SDK_ROOT.")
    return False

def make_apksigner_wrapper(lib_dir):
    """Create $PREFIX/bin/aio-apksigner-sdk from a build-tools lib directory."""
    lib_dir = Path(lib_dir)
    BIN_PATH.mkdir(parents=True, exist_ok=True)
    APKSIGNER_OPT.mkdir(parents=True, exist_ok=True)
    script = (
        '#!/data/data/com.termux/files/usr/bin/sh\n'
        '# SDK-style apksigner wrapper generated by AIO public setup.\n'
        f'DIR="{str(lib_dir)}"\n'
        'CP=""\n'
        'for j in "$DIR"/*.jar; do\n'
        '  if [ -f "$j" ]; then\n'
        '    if [ -z "$CP" ]; then CP="$j"; else CP="$CP:$j"; fi\n'
        '  fi\n'
        'done\n'
        'if [ -z "$CP" ]; then\n'
        '  echo "AIO apksigner wrapper error: no jars in $DIR" >&2\n'
        '  exit 2\n'
        'fi\n'
        'exec java -cp "$CP" com.android.apksigner.ApkSignerTool "$@"\n'
    )
    APKSIGNER_WRAPPER.write_text(script, encoding="utf-8")
    APKSIGNER_WRAPPER.chmod(APKSIGNER_WRAPPER.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ok(f"wrapper: {APKSIGNER_WRAPPER}")
    return APKSIGNER_WRAPPER


def find_existing_sdk_apksigner():
    candidates = []
    env = os.environ.get("AIO_APKSIGNER", "").strip()
    if env:
        candidates.append(Path(env))
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env_name, "").strip()
        if root:
            bt = Path(root) / "build-tools"
            if bt.is_dir():
                for child in sorted(bt.iterdir(), reverse=True):
                    candidates.append(child / "apksigner")
    candidates.append(APKSIGNER_WRAPPER)
    for c in candidates:
        if probe_apksigner_sdk(c):
            return c
    return None


def find_existing_apksigner_lib_dir():
    dirs = [APKSIGNER_LIB]
    for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env_name, "").strip()
        if root:
            bt = Path(root) / "build-tools"
            if bt.is_dir():
                for child in sorted(bt.iterdir(), reverse=True):
                    dirs.append(child / "lib")
    dirs.append(BIN_PATH)
    for d in dirs:
        if (d / "apksigner.jar").is_file():
            return d
    return None


def copy_apksigner_libs_from_extracted(root):
    found = []
    for p in Path(root).rglob("apksigner.jar"):
        if p.is_file():
            found.append(p.parent)
    if not found:
        return None
    src = found[0]
    APKSIGNER_LIB.mkdir(parents=True, exist_ok=True)
    copied = 0
    for jar in src.glob("*.jar"):
        shutil.copy2(jar, APKSIGNER_LIB / jar.name)
        copied += 1
    ok(f"copied {copied} apksigner jar(s)")
    return APKSIGNER_LIB


def download_and_install_buildtools_apksigner():
    if not shutil.which("java"):
        raise RuntimeError("java tidak ditemukan. Install openjdk dulu.")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        last_err = None
        for url in BUILD_TOOLS_URLS:
            zip_path = td / "build-tools.zip"
            info(f"Download Android build-tools public: {url.rsplit('/',1)[-1]}")
            try:
                if not download_file(url, zip_path, 5 * 1024 * 1024):
                    continue
                xdir = td / "x"
                xdir.mkdir(parents=True, exist_ok=True)
                safe_extract_zip(zip_path, xdir)
                lib_dir = copy_apksigner_libs_from_extracted(xdir)
                if lib_dir:
                    return lib_dir
            except Exception as e:
                last_err = e
                log_write(f"build-tools download/extract error: {type(e).__name__}: {e}")
        raise RuntimeError(f"gagal mendapatkan SDK apksigner public: {last_err}")


def ensure_apksigner_sdk(download_sdk=True):
    """
    Ensure SDK-style apksigner wrapper exists and works.

    This intentionally does not use /usr/bin/apksigner.jar and does not rely on
    Termux's lightweight `apksigner` CLI. The protector calls this wrapper.
    """
    if probe_apksigner_sdk(APKSIGNER_WRAPPER):
        ok("SDK-style apksigner tersedia")
        return True

    existing = find_existing_sdk_apksigner()
    if existing:
        if existing != APKSIGNER_WRAPPER:
            APKSIGNER_WRAPPER.write_text(
                f'#!/data/data/com.termux/files/usr/bin/sh\nexec "{existing}" "$@"\n',
                encoding="utf-8",
            )
            APKSIGNER_WRAPPER.chmod(APKSIGNER_WRAPPER.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ok(f"SDK apksigner existing: {existing}")
        return probe_apksigner_sdk(APKSIGNER_WRAPPER)

    lib_dir = find_existing_apksigner_lib_dir()
    if not lib_dir and download_sdk:
        lib_dir = download_and_install_buildtools_apksigner()
    if lib_dir:
        make_apksigner_wrapper(lib_dir)
        if probe_apksigner_sdk(APKSIGNER_WRAPPER):
            ok("SDK-style apksigner siap")
            return True

    warn("SDK-style apksigner belum siap. Jalankan setup dengan internet aktif atau set ANDROID_HOME/ANDROID_SDK_ROOT.")
    return False


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
        if label == "apksigner.jar":
            if not probe_legacy_apksigner_jar(APKSIGNER_JAR):
                missing_tools.append(label)
            continue
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
    print(c("AIO Public-Safe Setup Tools v4", BLUE))
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
    info("Setup legacy apksigner.jar compatible"); ensure_legacy_apksigner_jar(download_sdk=True)
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
