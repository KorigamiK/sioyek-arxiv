import argparse
import contextlib
import io
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import sioyek_open as app


ABSTRACT = '''<meta name="citation_title" content="A &amp; B: A Paper">
<a href="/abs/1706.03762v1">v1</a>
<a href="https://arxiv.org/abs/1706.03762v7">v7</a>'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sioyek-open-test-")
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
                    "sioyek-open://other?url=https://arxiv.org/abs/1706.03762"]:
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

    def test_mirex_bookmarklet_saves_in_configured_folder_and_opens_saved_pdf(self):
        papers = self.home / "My Papers"
        sioyek = self.home / "Applications/sioyek.app"
        sioyek.mkdir(parents=True)
        app.config_path().parent.mkdir(parents=True)
        app.config_path().write_text(json.dumps({
            "papers_dir": str(papers), "sioyek_app": str(sioyek),
        }))
        canonical = "https://music-ir.org/mirex/abstracts/2019/SDE1.pdf?download=1%202"

        def native_command(command, **kwargs):
            if command[0] == "/usr/bin/curl":
                self.assertEqual(command[-1], canonical)
                Path(command[command.index("--output") + 1]).write_bytes(b"%PDF-1.7\npaper")
            return subprocess.CompletedProcess(command, 0)

        output = io.StringIO()
        with patch.object(app.subprocess, "run", side_effect=native_command) as run, \
                contextlib.redirect_stdout(output):
            # Exercise the same custom URL and CLI path used by the bookmarklet.
            for url in [canonical.replace("//music-ir", "//www.music-ir") + "#page=3", canonical]:
                incoming = "sioyek-open://open?" + urlencode({"url": url})
                with patch.object(app.sys, "argv", ["sioyek-open", "open", "--notify", incoming]):
                    self.assertEqual(app.main(), 0)
        paths = output.getvalue().splitlines()
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], paths[1])
        saved = Path(paths[0])
        self.assertEqual(saved.parent, papers)
        self.assertRegex(saved.name, r"^SDE1 \[[0-9a-f]{12}\]\.pdf$")
        self.assertTrue(app.is_pdf(saved))
        record = app.library_records(papers)[0]
        self.assertEqual(record["open_count"], 2)
        self.assertEqual(record["files"][0]["path"], str(saved.resolve()))
        self.assertEqual(list(papers.glob("*.part")), [])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(sum(command[0] == "/usr/bin/curl" for command in commands), 1)
        self.assertEqual(commands[1:], [["/usr/bin/open", "-a", str(sioyek), str(saved)]] * 2)

    def test_mirex_alias_preserves_explicit_port(self):
        self.assertEqual(app.document_url("https://www.music-ir.org:443/mirex/paper.pdf?x=1#page=2"),
                         "https://music-ir.org:443/mirex/paper.pdf?x=1")

    def test_certificate_error_is_readable_and_does_not_try_other_hosts(self):
        detail = "curl: (60) SSL: no alternative certificate subject name matches target host name 'www.example.org'"
        for host in ["www.example.org", "music-ir.org"]:
            with self.subTest(host=host), \
                    patch.object(app.subprocess, "run", side_effect=subprocess.CalledProcessError(
                        60, ["/usr/bin/curl"], stderr=detail)) as run:
                with self.assertRaisesRegex(ValueError, "HTTPS certificate verification failed") as raised:
                    app.open_document(f"https://{host}/paper.pdf", self.home)
                self.assertIn(detail, str(raised.exception))
                self.assertNotIn("Command '[", str(raised.exception))
                run.assert_called_once()
                command = run.call_args.args[0]
                self.assertEqual(command[-1], f"https://{host}/paper.pdf")
                self.assertNotIn("--insecure", command)
                self.assertNotIn("-k", command)
                self.assertEqual(list(self.home.glob("*.pdf")), [])
                self.assertEqual(list(self.home.glob("*.part")), [])

    def test_download_error_detail_reaches_alert(self):
        detail = "curl: (22) The requested URL returned error: 403"
        with patch.object(app.subprocess, "run", side_effect=subprocess.CalledProcessError(
                    22, ["/usr/bin/curl"], stderr=detail)), \
                patch.object(app, "notify_error") as notify, \
                patch.object(app.sys, "argv", ["sioyek-open", "open", "--notify", "--no-open",
                                                "https://example.org/paper.pdf"]), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(app.main(), 1)
        message = notify.call_args.args[0]
        self.assertIn("Download failed for example.org (curl error 22)", message)
        self.assertIn(detail, message)
        self.assertIn(message, errors.getvalue())

    def test_download_error_without_stderr(self):
        with patch.object(app.subprocess, "run", side_effect=subprocess.CalledProcessError(
                    28, ["/usr/bin/curl"])), \
                self.assertRaisesRegex(ValueError, r"Download failed for example.org \(curl error 28\)"):
            app.fetch("https://example.org/paper.pdf", self.home / "paper.part")

    def test_web_response_must_be_a_pdf(self):
        with patch.object(app, "fetch", side_effect=lambda url, path: path.write_text("<html>sign in</html>")), \
                self.assertRaisesRegex(ValueError, "other than a PDF"):
            app.open_document("https://www.music-ir.org/mirex/paper.pdf", self.home)
        self.assertEqual(list(self.home.glob("*.pdf")), [])
        self.assertEqual(list(self.home.glob("*.part")), [])

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

    def test_setup_preserves_settings_and_stable_launcher(self):
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
        self.assertEqual(info["CFBundleURLTypes"][0]["CFBundleURLSchemes"], ["sioyek-open"])
        self.assertNotIn("NSCameraUsageDescription", info)
        self.assertIn(str(papers), (app.support_dir() / "bookmarklet.html").read_text())
        self.assertEqual((app.support_dir() / "bookmarklet.txt").read_text().strip(), app.BOOKMARKLET)

    def test_setup_failure_preserves_previous_app(self):
        sioyek = self.home / "sioyek.app"
        sioyek.mkdir()
        (app.handler_app() / "Contents").mkdir(parents=True)
        (app.handler_app() / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "local.sioyek.open"}))
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
            plistlib.dumps({"CFBundleIdentifier": "local.sioyek.open"}))
        app.support_dir().mkdir(parents=True)
        paper = self.home / "Downloads/Papers/keep.pdf"
        paper.parent.mkdir(parents=True)
        paper.write_bytes(b"%PDF-keep")
        app.config_path().parent.mkdir(parents=True)
        app.config_path().write_text("{}")
        app.record_document(paper, paper.parent)
        with patch.object(app.subprocess, "run"):
            app.uninstall()
        self.assertFalse(app.handler_app().exists())
        self.assertFalse(app.support_dir().exists())
        self.assertTrue(paper.exists())
        self.assertTrue(app.library_path(paper.parent).exists())
        self.assertTrue(app.config_path().exists())


if __name__ == "__main__":
    unittest.main()
