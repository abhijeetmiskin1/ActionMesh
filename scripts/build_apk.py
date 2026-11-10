"""Build a signed ActionMesh Android APK using only the Android SDK build-tools.

This replaces the two Windows-only PowerShell helpers (``android_toolchain.ps1``
and ``fetch_resumable.ps1``). It is standard-library only and portable: it
downloads a portable JDK and the Android command-line tools into
``android-toolchain/`` (git-ignored), installs a platform and build-tools through
``sdkmanager``, then compiles ``android/`` — a dependency-free WebView shell with
no AndroidX — using ``aapt2`` + ``javac`` + ``d8`` and signs it with a debug
keystore.

There is no Gradle, no Android Gradle Plugin and no Maven dependency resolution:
the entire build is the SDK build-tools. That keeps the toolchain small and the
build reproducible, and matches the project's dependency-light approach.

Usage:
    python scripts/build_apk.py                      # fetch if needed, then build
    python scripts/build_apk.py --url http://192.168.1.10:8080
    python scripts/build_apk.py --no-download        # never touch the network
    python scripts/build_apk.py --jdk /path --sdk /path
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android"
TOOLCHAIN = ROOT / "android-toolchain"
BUILD = ROOT / "build" / "apk"
DIST = ROOT / "dist"

SDK = TOOLCHAIN / "sdk"
JDK = TOOLCHAIN / "jdk"
CMDLINE = TOOLCHAIN / "cmdline-tools" / "latest"
KEYSTORE = TOOLCHAIN / "debug.keystore"

PLATFORM = "android-34"
BUILD_TOOLS = "34.0.0"
MIN_SDK = "26"
TARGET_SDK = "34"
APP_PACKAGE = "app.actionmesh.control"

USER_AGENT = "ActionMesh-BuildAPK/1.0 (+https://example.invalid)"

# Linux and macOS share the same vendor tokens ("linux" / "mac") for both the
# Adoptium API and Google's command-line-tools packages, so one variable covers
# both. Windows uses a different pair of tokens, so this script targets the two
# POSIX platforms; the re-runnable PowerShell helpers remain for Windows.
DOWNLOAD_OS = "mac" if sys.platform == "darwin" else "linux"

CMDLINE_URL = (
    "https://dl.google.com/android/repository/"
    f"commandlinetools-{DOWNLOAD_OS}-11076708_latest.zip"
)


# -- small helpers -----------------------------------------------------------


def log(message: str) -> None:
    print(message, flush=True)


def run(cmd: list[object], env: dict[str, str] | None = None) -> None:
    """Run a build tool, echoing it, and raise on a non-zero exit."""
    shown = " ".join(str(part) for part in cmd)
    log("  $ " + shown)
    subprocess.run([str(part) for part in cmd], check=True, env=env)


def android_jar() -> Path:
    return SDK / "platforms" / PLATFORM / "android.jar"


def build_tools_dir() -> Path:
    return SDK / "build-tools" / BUILD_TOOLS


def tool_env() -> dict[str, str]:
    """Environment with the portable JDK and the build-tools on PATH.

    ``sdkmanager``, ``d8`` and ``apksigner`` are wrapper scripts that call
    ``java``, so JAVA_HOME and PATH must point at the downloaded JDK.
    """
    env = dict(os.environ)
    env["JAVA_HOME"] = str(JDK)
    env["ANDROID_SDK_ROOT"] = str(SDK)
    env["ANDROID_HOME"] = str(SDK)
    env["PATH"] = os.pathsep.join(
        [str(JDK / "bin"), str(build_tools_dir()), env.get("PATH", "")]
    )
    return env


def _chmod_x(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if mode & 0o111 != 0o111:
        path.chmod(mode | 0o111)


def make_executables() -> None:
    """The SDK ships shell wrappers with the executable bit; zip extraction may
    not preserve it, so set it explicitly."""
    for folder in (JDK / "bin", CMDLINE / "bin"):
        for path in folder.glob("*"):
            _chmod_x(path)
    for name in ("aapt2", "d8", "zipalign", "apksigner", "aapt"):
        _chmod_x(build_tools_dir() / name)


def _download(url: str, dest: Path) -> None:
    """Fetch a URL, preferring curl (robust redirects/retries) and falling back
    to the standard library."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"  downloading {dest.name} <- {url}")
    if shutil.which("curl"):
        run(["curl", "-fL", "--retry", "8", "--retry-delay", "3",
             "--connect-timeout", "20", "-o", str(dest), url])
    else:  # pragma: no cover - exercised only where curl is absent
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=180) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"download failed: {url}")
    log(f"  saved {dest.name} ({dest.stat().st_size >> 20} MiB)")


# -- toolchain provisioning --------------------------------------------------


def resolve_jdk_url() -> str:
    architecture = "aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
    api = (
        "https://api.adoptium.net/v3/assets/feature_releases/17/ga"
        f"?architecture={architecture}&image_type=jdk&os={DOWNLOAD_OS}&vendor=eclipse&page_size=1"
    )
    request = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as resp:
        data = json.load(resp)
    return data[0]["binaries"][0]["package"]["link"]


def ensure_jdk() -> None:
    if (JDK / "bin" / "javac").exists():
        return
    tarball = TOOLCHAIN / "jdk.tar.gz"
    if not tarball.exists():
        _download(resolve_jdk_url(), tarball)
    raw = TOOLCHAIN / "jdkraw"
    if raw.exists():
        shutil.rmtree(raw)
    raw.mkdir(parents=True)
    with tarfile.open(tarball) as archive:
        archive.extractall(raw)
    inner = next(path for path in raw.iterdir() if path.is_dir())
    mac_home = inner / "Contents" / "Home"
    if mac_home.is_dir():
        inner = mac_home
    if JDK.exists():
        shutil.rmtree(JDK)
    inner.rename(JDK)
    shutil.rmtree(raw)
    tarball.unlink(missing_ok=True)
    log(f"  JDK ready: {JDK}")


def ensure_cmdline() -> None:
    if (CMDLINE / "bin" / "sdkmanager").exists():
        return
    zpath = TOOLCHAIN / "cmdline-tools.zip"
    if not zpath.exists():
        _download(CMDLINE_URL, zpath)
    tmp = TOOLCHAIN / "cmdraw"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(zpath) as archive:
        archive.extractall(tmp)
    (TOOLCHAIN / "cmdline-tools").mkdir(parents=True, exist_ok=True)
    if CMDLINE.exists():
        shutil.rmtree(CMDLINE)
    (tmp / "cmdline-tools").rename(CMDLINE)
    shutil.rmtree(tmp)
    zpath.unlink(missing_ok=True)
    log(f"  cmdline-tools ready: {CMDLINE}")


def ensure_sdk() -> None:
    if android_jar().exists() and (build_tools_dir() / "aapt2").exists():
        return
    make_executables()
    env = tool_env()
    sdkmanager = CMDLINE / "bin" / "sdkmanager"
    log("  accepting SDK licences")
    subprocess.run(
        [str(sdkmanager), f"--sdk_root={SDK}", "--licenses"],
        input=b"y\n" * 16,
        env=env,
        check=False,
    )
    run([sdkmanager, f"--sdk_root={SDK}", "platform-tools",
         f"platforms;{PLATFORM}", f"build-tools;{BUILD_TOOLS}"], env=env)
    make_executables()
    log("  SDK components installed")


# -- build -------------------------------------------------------------------


def patch_default_url(strings_xml: Path, url: str) -> None:
    """Bake a default server URL into the copied strings resource.

    The build works on a copy of ``android/`` so the tracked source keeps its
    neutral emulator default.
    """
    text = strings_xml.read_text(encoding="utf-8")
    text = re.sub(
        r'(<string name="default_server_url">)[^<]*(</string>)',
        lambda match: match.group(1) + url + match.group(2),
        text,
    )
    strings_xml.write_text(text, encoding="utf-8")
    log(f"  default server url = {url}")


def build(args: argparse.Namespace) -> Path:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    src = BUILD / "src"
    shutil.copytree(ANDROID, src)
    if args.url:
        patch_default_url(src / "res" / "values" / "strings.xml", args.url)

    env = tool_env()
    tools = build_tools_dir()
    manifest = src / "AndroidManifest.xml"
    gen = BUILD / "gen"
    gen.mkdir(parents=True)

    log("compiling resources (aapt2)")
    flata = BUILD / "resources.flata"
    run([tools / "aapt2", "compile", "--dir", src / "res", "-o", flata], env=env)
    base = BUILD / "base.apk"
    run([tools / "aapt2", "link", "-o", base, "-I", android_jar(),
         "--manifest", manifest, "--java", gen,
         "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
         "--replace-version", "--version-code", str(args.version_code),
         "--version-name", args.version_name,
         flata], env=env)

    log("compiling java (javac)")
    classes = BUILD / "classes"
    classes.mkdir()
    sources = [str(path) for path in (src / "java").rglob("*.java")]
    sources += [str(path) for path in gen.rglob("*.java")]
    run([JDK / "bin" / "javac", "-source", "8", "-target", "8",
         "-bootclasspath", android_jar(), "-classpath", android_jar(),
         "-encoding", "UTF-8", "-d", classes, *sorted(sources)], env=env)

    log("dexing (d8)")
    classes_jar = BUILD / "classes.jar"
    with zipfile.ZipFile(classes_jar, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(classes.rglob("*.class")):
            archive.write(path, path.relative_to(classes))
    dex = BUILD / "dex"
    dex.mkdir()
    run([tools / "d8", "--lib", android_jar(), "--min-api", MIN_SDK,
         "--output", dex, classes_jar], env=env)

    log("packaging")
    unaligned = BUILD / "unaligned.apk"
    shutil.copy(base, unaligned)
    with zipfile.ZipFile(unaligned, "a") as archive:
        archive.write(dex / "classes.dex", "classes.dex",
                      compress_type=zipfile.ZIP_STORED)
    aligned = BUILD / "aligned.apk"
    run([tools / "zipalign", "-f", "-p", "4", unaligned, aligned], env=env)

    if not KEYSTORE.exists():
        log("generating debug keystore")
        run([JDK / "bin" / "keytool", "-genkeypair", "-keystore", KEYSTORE,
             "-storepass", "android", "-keypass", "android",
             "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048",
             "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"],
            env=env)

    DIST.mkdir(parents=True, exist_ok=True)
    output = DIST / f"actionmesh-{args.version_name}.apk"
    log("signing (apksigner)")
    run([tools / "apksigner", "sign", "--ks", KEYSTORE,
         "--ks-pass", "pass:android", "--key-pass", "pass:android",
         "--min-sdk-version", MIN_SDK, "--out", output, aligned], env=env)
    run([tools / "apksigner", "verify", "--print-certs", output], env=env)
    return output


def missing_toolchain() -> list[Path]:
    return [
        path
        for path in (
            JDK / "bin" / "javac",
            CMDLINE / "bin" / "sdkmanager",
            android_jar(),
            build_tools_dir() / "aapt2",
        )
        if not path.exists()
    ]


def main(argv: list[str]) -> int:
    global JDK, SDK
    parser = argparse.ArgumentParser(
        prog="build_apk.py",
        description="Build a signed ActionMesh Android APK with the SDK build-tools.",
    )
    parser.add_argument("--url", help="default server URL baked into the APK")
    parser.add_argument("--version-name", default="0.1.0")
    parser.add_argument("--version-code", default="1")
    parser.add_argument("--jdk", help="use an existing JDK instead of downloading one")
    parser.add_argument("--sdk", help="use an existing Android SDK instead of downloading one")
    parser.add_argument("--no-download", action="store_true",
                        help="never touch the network; fail if the toolchain is missing")
    args = parser.parse_args(argv[1:])

    if args.jdk:
        JDK = Path(args.jdk).resolve()
    if args.sdk:
        SDK = Path(args.sdk).resolve()

    if not ANDROID.exists():
        log(f"error: Android sources not found at {ANDROID}")
        return 2

    if args.no_download:
        missing = missing_toolchain()
        if missing:
            log("error: toolchain incomplete and --no-download was given:")
            for path in missing:
                log(f"  missing {path}")
            return 2
    else:
        ensure_jdk()
        ensure_cmdline()
        ensure_sdk()

    make_executables()
    output = build(args)
    log(f"APK built: {output} ({output.stat().st_size >> 10} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


