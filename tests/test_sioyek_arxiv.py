import argparse
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sioyek_arxiv as app


ABSTRACT = '''<meta name="citation_title" content="A &amp; B: A Paper">
<a href="/abs/1706.03762v1">v1</a>
<a href="https://arxiv.org/abs/1706.03762v7">v7</a>'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sioyek-arxiv-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def fake_fetch(self, url, destination):
        if "/abs/" in url:
            destination.write_text(ABSTRACT, encoding="utf-8")
        else:
            destination.write_bytes(b"%PDF-1.7\ntest paper")

    def test_supported_urls(self):
        for url, identifier in [
            ("https://arxiv.org/abs/1706.03762", "1706.03762"),
            ("https://arxiv.org/html/1706.03762v7", "1706.03762v7"),
            ("https://arxiv.org/pdf/hep-th/9901001v2.pdf#page=3", "hep-th/9901001v2"),
            ("sioyek-arxiv://open?url=https%3A%2F%2Farxiv.org%2Fabs%2F1706.03762", "1706.03762"),
            ("sioyek-open://open?url=https%3A%2F%2Farxiv.org%2Fabs%2F1706.03762", "1706.03762"),
        ]:
            with self.subTest(url=url):
                self.assertEqual(app.paper_id(url), identifier)

    def test_rejects_other_hosts_and_invalid_ids(self):
        for url in ["https://evil.example/abs/1706.03762",
                    "https://arxiv.org.evil.example/abs/1706.03762",
                    "https://arxiv.org:9000/abs/1706.03762",
                    "https://user@arxiv.org/abs/1706.03762",
                    "https://arxiv.org/abs/../../file", "file:///tmp/file.pdf",
                    "sioyek-arxiv://other?url=https://arxiv.org/abs/1706.03762"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                app.paper_id(url)

    def test_latest_and_explicit_versions(self):
        with patch.object(app, "fetch", side_effect=self.fake_fetch):
            self.assertEqual(app.describe("1706.03762"), ("A & B: A Paper", "1706.03762v7"))
            self.assertEqual(app.describe("1706.03762v2")[1], "1706.03762v2")

    def test_filename_is_safe_and_fits(self):
        name = app.filename("é" * 300 + "/:*?", "hep-th/9901001v2")
        self.assertLess(len(name.encode("utf-8")), 256)
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith("[hep-th_9901001v2].pdf"))

    def test_download_and_duplicate_reuse(self):
        folder = self.home / "papers"
        with patch.object(app, "fetch", side_effect=self.fake_fetch) as fetch:
            path = app.save_paper("https://arxiv.org/abs/1706.03762", folder)
            before = path.stat().st_mtime_ns
            again = app.save_paper("https://arxiv.org/abs/1706.03762", folder)
        self.assertEqual(path, again)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(path.name, "A & B- A Paper [1706.03762v7].pdf")
        self.assertEqual(sum("/pdf/" in call.args[0] for call in fetch.call_args_list), 1)
        self.assertEqual(list(folder.glob("*.part")), [])

    def test_failed_download_has_no_final_or_partial_file(self):
        folder = self.home / "papers"
        with patch.object(app, "describe", return_value=("Paper", "1706.03762v7")), \
                patch.object(app, "fetch", side_effect=lambda url, path: path.write_text("<html>error</html>")):
            with self.assertRaisesRegex(ValueError, "other than a PDF"):
                app.save_paper("https://arxiv.org/abs/1706.03762", folder)
        self.assertEqual(list(folder.glob("*.pdf")), [])
        self.assertEqual(list(folder.glob("*.part")), [])

    def test_existing_non_pdf_is_never_overwritten(self):
        path = self.home / "Paper [1706.03762v7].pdf"
        path.write_text("keep me")
        with patch.object(app, "describe", return_value=("Paper", "1706.03762v7")), \
                self.assertRaisesRegex(ValueError, "move it aside"):
            app.save_paper("https://arxiv.org/abs/1706.03762", self.home)
        self.assertEqual(path.read_text(), "keep me")

    def test_rejects_malformed_config(self):
        app.config_path().parent.mkdir(parents=True)
        for value in [[], {"papers_dir": 42}, {"sioyek_app": ""}]:
            app.config_path().write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                app.load_config()

    def fake_native_command(self, command, **kwargs):
        if command[0] == "/usr/bin/osacompile":
            contents = Path(command[2]) / "Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_bytes(plistlib.dumps({"NSCameraUsageDescription": "unused"}))
        return subprocess.CompletedProcess(command, 0)

    def setup_args(self, **kwargs):
        values = dict(papers_dir=None, sioyek_app=None, no_open=True, browser=None)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_setup_migrates_existing_settings_and_stable_launcher(self):
        sioyek = self.home / "Applications/sioyek.app"
        sioyek.mkdir(parents=True)
        papers = self.home / "My Papers"
        config = {"papers_dir": str(papers), "sioyek_app": str(sioyek), "future_option": True}
        app.config_path().parent.mkdir(parents=True)
        app.config_path().write_text(json.dumps(config))
        launcher = "/opt/homebrew/opt/sioyek-open/bin/sioyek-open"
        with patch.object(app.sys, "platform", "darwin"), \
                patch.dict(os.environ, {"SIOYEK_OPEN_LAUNCHER": launcher}), \
                patch.object(app.subprocess, "run", side_effect=self.fake_native_command):
            app.setup(self.setup_args())
            app.setup(self.setup_args())
        self.assertEqual(app.load_config(), config)
        script = (app.support_dir() / "handler.applescript").read_text()
        self.assertIn(launcher, script)
        self.assertIn("open --notify", script)
        self.assertNotIn("Cellar", script)
        info = plistlib.loads((app.handler_app() / "Contents/Info.plist").read_bytes())
        self.assertEqual(info["CFBundleURLTypes"][0]["CFBundleURLSchemes"], ["sioyek-open", "sioyek-arxiv"])
        self.assertNotIn("NSCameraUsageDescription", info)
        self.assertIn(str(papers), (app.support_dir() / "bookmarklet.html").read_text())
        self.assertEqual((app.support_dir() / "bookmarklet.txt").read_text().strip(), app.BOOKMARKLET)

    def test_setup_failure_preserves_previous_app(self):
        sioyek = self.home / "sioyek.app"
        sioyek.mkdir()
        app.handler_app().mkdir(parents=True)
        marker = app.handler_app() / "keep.txt"
        marker.write_text("original")

        def fail_registration(command, **kwargs):
            if command[0].endswith("lsregister"):
                raise subprocess.CalledProcessError(1, command)
            return self.fake_native_command(command, **kwargs)

        with patch.object(app.sys, "platform", "darwin"), \
                patch.object(app.subprocess, "run", side_effect=fail_registration), \
                self.assertRaises(subprocess.CalledProcessError):
            app.setup(self.setup_args(sioyek_app=str(sioyek)))
        self.assertEqual(marker.read_text(), "original")
        self.assertFalse(app.config_path().exists())

    def test_uninstall_keeps_downloads_and_settings(self):
        app.handler_app().mkdir(parents=True)
        (app.handler_app() / "Contents").mkdir()
        (app.handler_app() / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "local.sioyek.arxiv"}))
        app.support_dir().mkdir(parents=True)
        paper = self.home / "Downloads/Papers/keep.pdf"
        paper.parent.mkdir(parents=True)
        paper.write_bytes(b"%PDF-keep")
        app.config_path().parent.mkdir(parents=True)
        app.config_path().write_text("{}")
        with patch.object(app.subprocess, "run"):
            app.uninstall()
        self.assertFalse(app.handler_app().exists())
        self.assertFalse(app.support_dir().exists())
        self.assertTrue(paper.exists())
        self.assertTrue(app.config_path().exists())


if __name__ == "__main__":
    unittest.main()
