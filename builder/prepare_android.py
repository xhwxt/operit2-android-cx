"""Backport dynamic_color's AGP 8 fix and check Gradle configuration in CI.

Only the runner's resolved dynamic_color 1.9.0 Gradle script is patched.
Application code, dependency versions, runtime payloads and signing checks stay intact.
The original/patched script, license and patch provenance accompany the APK.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

FIX_COMMIT = "44150ccd4d22f3b8da16e5bb07f73dea43418ee2"
FIX_URL = "https://github.com/material-foundation/flutter-packages/commit/" + FIX_COMMIT
OLD_BLOCK = """kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}"""
NEW_BLOCK = """val agpMajor = com.android.Version.ANDROID_GRADLE_PLUGIN_VERSION.substringBefore('.').toInt()

if (agpMajor < 9) {
    apply(plugin = "org.jetbrains.kotlin.android")
}

project.extensions.configure(org.jetbrains.kotlin.gradle.dsl.KotlinAndroidProjectExtension::class.java) {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}"""


def _record_no_backport(dist: Path, version: str, reason: str) -> dict[str, str]:
    report = {"package": "dynamic_color", "version": version, "change": reason}
    directory = dist / "compatibility"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dynamic_color-status.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(__file__, directory / "prepare_android.py")
    return report


def patch_dynamic_color(app: Path, dist: Path) -> dict[str, str]:
    """Apply the upstream eight-line compatibility change, idempotently."""
    plugins = json.loads((app / ".flutter-plugins-dependencies").read_text(encoding="utf-8-sig"))
    matches = [p for p in plugins["plugins"]["android"] if p["name"] == "dynamic_color"]
    if not matches:
        return _record_no_backport(dist, "absent", "Not present in this upstream revision; no backport")
    if len(matches) != 1:
        raise RuntimeError("Ambiguous resolved dynamic_color Android plugin")
    package = Path(matches[0]["path"])
    version = re.search(r"(?m)^version:\s*([^\r\n#]+)", (package / "pubspec.yaml").read_text(encoding="utf-8"))
    if version is None:
        raise RuntimeError("Cannot determine dynamic_color package version")
    resolved_version = version.group(1).strip().strip("\"'")
    if resolved_version != "1.9.0":
        print(f"dynamic_color {resolved_version}: retaining upstream build script; Gradle will validate it", flush=True)
        return _record_no_backport(dist, resolved_version, "No legacy backport applied")
    gradle = package / "android" / "build.gradle.kts"
    text = gradle.read_text(encoding="utf-8")
    if text.count(NEW_BLOCK) == 1:
        before, after = text.replace(NEW_BLOCK, OLD_BLOCK, 1), text
    elif text.count(OLD_BLOCK) == 1:
        before, after = text, text.replace(OLD_BLOCK, NEW_BLOCK, 1)
        gradle.write_text(after, encoding="utf-8", newline="\n")
    else:
        print("dynamic_color has a different upstream build script; retaining it for actual Gradle validation", flush=True)
        return _record_no_backport(dist, "1.9.0", "Unrecognized upstream script retained; no speculative patch")

    report_dir = dist / "compatibility" / "dynamic_color-1.9.0"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "build.gradle.kts.before").write_text(before, encoding="utf-8", newline="\n")
    (report_dir / "build.gradle.kts").write_text(after, encoding="utf-8", newline="\n")
    (report_dir / "agp8.patch").write_text("".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="a/android/build.gradle.kts", tofile="b/android/build.gradle.kts",
    )), encoding="utf-8", newline="\n")
    shutil.copy2(package / "LICENSE", report_dir / "LICENSE")
    report = {
        "package": "dynamic_color", "version": "1.9.0", "upstream_fix": FIX_URL,
        "change": "Backport Kotlin Android plugin application for AGP < 9 and typed extension configuration only",
        "original_gradle_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "patched_gradle_sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(),
        "application_code": "unchanged", "dependency_versions": "unchanged",
    }
    (report_dir / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(__file__, dist / "compatibility" / "prepare_android.py")
    print(f"dynamic_color 1.9.0: upstream AGP 8 backport applied ({FIX_COMMIT[:7]})", flush=True)
    return report


def prepare_gradle_wrapper(flutter: str, android: Path) -> Path:
    """Copy only missing wrapper files from the SDK, as flutter_tools does."""
    cache = Path(flutter).parent.parent / "bin" / "cache" / "artifacts" / "gradle_wrapper"
    for relative in ("gradlew", "gradlew.bat", "gradle/wrapper/gradle-wrapper.jar"):
        destination = android / relative
        if not destination.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache / relative, destination)
    return android / ("gradlew.bat" if os.name == "nt" else "gradlew")


ALIYUN_HOST = "maven.aliyun.com"
ALIYUN_REPOSITORY_MIRRORS = {
    "https://maven.aliyun.com/repository/google": "https://dl.google.com/dl/android/maven2/",
    "https://maven.aliyun.com/repository/public": "https://repo1.maven.org/maven2/",
    "https://maven.aliyun.com/repository/central": "https://repo1.maven.org/maven2/",
    "https://maven.aliyun.com/repository/gradle-plugin": "https://plugins.gradle.org/m2/",
}


def prefer_official_maven_repositories(root: Path, dist: Path | None = None) -> dict:
    """Replace the Aliyun Maven mirrors with the official repositories.

    GitHub-hosted runners are outside China, where the Aliyun mirrors frequently
    answer with 502. Gradle treats a repository 5xx as a hard failure instead of
    falling through to the next repository, so a flaky mirror breaks the whole
    build. Only URLs are rewritten: structure, ordering and syntax stay intact,
    and any unmapped Aliyun path aborts the build instead of being patched
    silently.
    """
    android = root / "apps" / "flutter" / "app" / "android"
    changed = 0
    files: list[str] = []
    for path in sorted(android.rglob("*.gradle*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if ALIYUN_HOST not in text:
            continue
        for mirror, official in ALIYUN_REPOSITORY_MIRRORS.items():
            changed += text.count(mirror)
            text = text.replace(mirror, official)
        found = set(re.findall(r"https://maven\.aliyun\.com/[A-Za-z0-9._/-]+", text))
        unknown = sorted(found - set(ALIYUN_REPOSITORY_MIRRORS))
        if unknown:
            raise RuntimeError(f"Unmapped Aliyun repository URL(s): {unknown}")
        path.write_text(text, encoding="utf-8", newline="\n")
        files.append(path.relative_to(root).as_posix())
    if files:
        print(f"Maven mirrors: replaced {changed} Aliyun URL(s) in {len(files)} Gradle file(s)", flush=True)
    else:
        print("Maven mirrors: no Aliyun repository URL left to replace", flush=True)
    report = {
        "change": "Aliyun Maven mirrors replaced with the official repositories",
        "reason": "Aliyun answers 5xx from GitHub-hosted runners and Gradle treats repository 5xx as a hard failure",
        "files": files,
        "replaced_urls": changed,
    }
    if dist is not None and files:
        directory = dist / "compatibility"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "maven-mirrors.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        shutil.copy2(__file__, directory / "prepare_android.py")
    return report


def preflight() -> None:
    root = Path.cwd()
    sys.path.insert(0, str(root / "tools" / "build_scripts"))
    from common import (
        ANDROID_LOCAL_PROPERTIES, DIST_DIR, FLUTTER_APP_DIR, flutter_command,
        flutter_pub_get, read_properties, run, staged_non_ohos_flutter_dependencies,
        write_properties,
    )
    from build_flutter_android import configure_android_flutter_sdk, ensure_android_signing

    ensure_android_signing()
    flutter = flutter_command()
    configure_android_flutter_sdk(flutter)
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        local = read_properties(ANDROID_LOCAL_PROPERTIES)
        local["sdk.dir"] = Path(sdk).as_posix()
        write_properties(ANDROID_LOCAL_PROPERTIES, local)
    android = FLUTTER_APP_DIR / "android"
    gradle = prepare_gradle_wrapper(flutter, android)
    with staged_non_ohos_flutter_dependencies():
        flutter_pub_get()
        flutter_pub_get(enforce_lockfile=True)
        patch_dynamic_color(FLUTTER_APP_DIR, DIST_DIR)
        # 'help' configures all projects but does not compile an APK or run preBuild.
        run([str(gradle), "help", "--no-daemon", "--console=plain", "--stacktrace",
             "-Ptarget-platform=android-arm64"], cwd=android)
    print("Gradle configuration passed; APK compilation still has to succeed.", flush=True)


if __name__ == "__main__":
    preflight()
