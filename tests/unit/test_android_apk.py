"""Tests for the Android shell and ``scripts/build_apk.py``.

The APK build needs the Android SDK build-tools, so most tests here check the
*structure* (manifest, resources, build-script helpers) and run everywhere. The
end-to-end checks only run once an APK exists in ``dist/``, and skip honestly
when it does not — a skipped test is reported as skipped, never as a pass.

What the tests pin:

* the app requests **exactly one** permission (``INTERNET``) and nothing a
  WebView shell has no business holding;
* the launcher activity is exported but the app is not marked debuggable;
* a build script helper bakes a URL without disturbing the other strings;
* the produced APK actually contains a manifest, resources and a dex, and is
  really signed.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build_apk.py"
ANDROID = ROOT / "android"
MANIFEST = ANDROID / "AndroidManifest.xml"
DIST = ROOT / "dist"
NS = "{http://schemas.android.com/apk/res/android}"


def _load_build_module():
    spec = importlib.util.spec_from_file_location("am_build_apk", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise unittest.SkipTest("build_apk.py is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():  # pragma: no cover - repository guard
            raise unittest.SkipTest("scripts/build_apk.py is absent")
        cls.mod = _load_build_module()

    def test_patch_default_url_replaces_only_that_value(self):
        original = (
            '<resources>\n'
            '  <string name="app_name">ActionMesh</string>\n'
            '  <string name="default_server_url">http://10.0.2.2:8000</string>\n'
            '</resources>\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "strings.xml"
            path.write_text(original, encoding="utf-8")
            self.mod.patch_default_url(path, "http://192.168.1.10:8080")
            text = path.read_text(encoding="utf-8")
        self.assertIn("<string name=\"default_server_url\">http://192.168.1.10:8080</string>", text)
        # The neighbouring string must be untouched.
        self.assertIn("<string name=\"app_name\">ActionMesh</string>", text)

    def test_tool_env_prefers_the_portable_jdk(self):
        env = self.mod.tool_env()
        self.assertEqual(env["JAVA_HOME"], str(self.mod.JDK))
        self.assertEqual(env["ANDROID_SDK_ROOT"], str(self.mod.SDK))
        self.assertIn(str(self.mod.JDK / "bin"), env["PATH"].split(os.pathsep))

    def test_missing_toolchain_reports_absent_paths(self):
        module = self.mod
        saved_jdk, saved_sdk = module.JDK, module.SDK
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module.JDK = pathlib.Path(tmp) / "jdk"
                module.SDK = pathlib.Path(tmp) / "sdk"
                missing = module.missing_toolchain()
        finally:
            module.JDK, module.SDK = saved_jdk, saved_sdk
        self.assertTrue(missing)
        self.assertTrue(all(not path.exists() for path in missing))

    def test_target_sdk_is_not_below_min_sdk(self):
        self.assertGreaterEqual(int(self.mod.TARGET_SDK), int(self.mod.MIN_SDK))

    def test_min_sdk_supports_adaptive_icons_without_pngs(self):
        # The app ships only adaptive (v26) icons, which is why minSdk is 26.
        self.assertGreaterEqual(int(self.mod.MIN_SDK), 26)


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MANIFEST.exists():  # pragma: no cover - repository guard
            raise unittest.SkipTest("android/AndroidManifest.xml is absent")
        cls.root = ET.parse(MANIFEST).getroot()

    def test_requests_only_internet(self):
        permissions = {node.get(NS + "name") for node in self.root.findall("uses-permission")}
        self.assertEqual(permissions, {"android.permission.INTERNET"})

    def test_package_name(self):
        self.assertEqual(self.root.get("package"), "app.actionmesh.control")

    def test_launcher_activity_is_exported(self):
        activity = self.root.find("application/activity")
        self.assertIsNotNone(activity)
        self.assertEqual(activity.get(NS + "exported"), "true")
        actions = {node.get(NS + "name") for node in activity.iter("action")}
        categories = {node.get(NS + "name") for node in activity.iter("category")}
        self.assertIn("android.intent.action.MAIN", actions)
        self.assertIn("android.intent.category.LAUNCHER", categories)

    def test_app_is_neither_debuggable_nor_backed_up(self):
        application = self.root.find("application")
        self.assertNotEqual(application.get(NS + "debuggable"), "true")
        self.assertNotEqual(application.get(NS + "allowBackup"), "true")

    def test_cleartext_is_declared_explicitly(self):
        # A LAN ActionMesh server is normally plain http; the trade-off is
        # documented in android/README.md rather than left implicit.
        application = self.root.find("application")
        self.assertEqual(application.get(NS + "usesCleartextTraffic"), "true")


class ResourceTests(unittest.TestCase):
    def _xml(self, *parts: str):
        path = ANDROID.joinpath(*parts)
        if not path.exists():  # pragma: no cover - repository guard
            raise unittest.SkipTest(f"{path} is absent")
        return ET.parse(path).getroot()

    def test_default_server_url_is_present(self):
        values = {node.get("name"): node.text for node in self._xml("res", "values", "strings.xml")}
        self.assertIn("default_server_url", values)
        self.assertTrue(values["default_server_url"].startswith("http"))

    def test_brand_colours_match_the_pwa_palette(self):
        values = {node.get("name"): node.text for node in self._xml("res", "values", "colors.xml")}
        self.assertEqual(values.get("brand_deep"), "#153b37")
        self.assertEqual(values.get("brand_mint"), "#a8e4d5")

    def test_adaptive_icon_layers_exist(self):
        foreground = ANDROID / "res" / "drawable" / "ic_launcher_foreground.xml"
        self.assertTrue(foreground.exists())
        icon = self._xml("res", "mipmap-anydpi-v26", "ic_launcher.xml")
        drawables = {node.get(NS + "drawable") for node in icon.iter()}
        self.assertIn("@color/brand_deep", drawables)
        self.assertIn("@drawable/ic_launcher_foreground", drawables)


def _find_apk():
    if not DIST.exists():
        return None
    apks = sorted(DIST.glob("actionmesh-*.apk"))
    return apks[-1] if apks else None


class BuiltApkTests(unittest.TestCase):
    """End-to-end checks on a real APK; skipped until one has been built."""

    @classmethod
    def setUpClass(cls):
        cls.apk = _find_apk()
        if cls.apk is None:
            raise unittest.SkipTest("no APK in dist/ (run scripts/build_apk.py)")

    def test_apk_contains_manifest_resources_and_dex(self):
        with zipfile.ZipFile(self.apk) as archive:
            names = set(archive.namelist())
        self.assertIn("AndroidManifest.xml", names)
        self.assertIn("resources.arsc", names)
        self.assertIn("classes.dex", names)

    def test_apk_is_a_valid_zip(self):
        with zipfile.ZipFile(self.apk) as archive:
            self.assertIsNone(archive.testzip())

    def test_apk_is_signed(self):
        data = self.apk.read_bytes()
        # minSdk 26 means apksigner applies the v2/v3 APK Signing Block rather
        # than a v1 META-INF signature, identified by this magic string.
        self.assertIn(b"APK Sig Block 42", data)

