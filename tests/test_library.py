import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import sioyek_open as app


class LibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sioyek-library-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.papers = self.home / "My Papers"
        self.papers.mkdir()
        app.config_path().parent.mkdir(parents=True)
        app.config_path().write_text(json.dumps({"papers_dir": str(self.papers)}))

    def pdf(self, name="paper.pdf", content=b"%PDF-1.7\ntest paper"):
        path = self.papers / name
        path.write_bytes(content)
        return path

    def cli(self, *args):
        with patch.object(app.sys, "argv", ["sioyek-open", *args]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(), 0)
        return output.getvalue()

    def test_bookmarklet_metadata_is_saved_and_survives_plain_reopens(self):
        data = {
            "page_url": "https://journal.example/article/1", "page_title": "Article | Journal",
            "canonical_url": "https://journal.example/article/1",
            "meta": {"citation_title": ["A & B: 音楽"], "citation_author": ["Doe, Jane", "李四"],
                     "citation_publication_date": ["2024/05/12"],
                     "citation_doi": ["https://doi.org/10.1234/music.1"],
                     "citation_journal_title": ["Music Journal"], "citation_volume": ["7"],
                     "citation_issue": ["2"], "citation_firstpage": ["10"], "citation_lastpage": ["20"],
                     "citation_keywords": ["music; AI"], "description": ["An abstract."],
                     "citation_pdf_url": ["https://example.org/paper.pdf"]},
            "json_ld": [{"@type": "ScholarlyArticle", "publisher": {"name": "Example Press"}}],
        }
        incoming = "sioyek-open://open?" + urlencode({"url": "https://example.org/paper.pdf", "metadata": json.dumps(data)})
        with patch.object(app, "fetch", side_effect=lambda url, path: path.write_bytes(b"%PDF-1.7\ntest paper")) as fetch:
            saved = Path(self.cli("open", "--no-open", incoming).strip())
            self.cli("open", "--no-open", "https://example.org/paper.pdf")
            # A PDF viewer's short page title must not erase the citation title.
            sparse = "sioyek-open://open?" + urlencode({"url": "https://example.org/paper.pdf",
                                                       "metadata": json.dumps({"page_title": "paper.pdf"})})
            self.cli("open", "--no-open", sparse)
            fetch.assert_called_once()
        records = app.library_records(self.papers)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["metadata"]["title"], "A & B: 音楽")
        self.assertEqual(record["metadata"]["authors"], ["Doe, Jane", "李四"])
        self.assertEqual(record["metadata"]["doi"], "10.1234/music.1")
        self.assertEqual(record["metadata"]["year"], "2024")
        self.assertEqual(record["metadata"]["publisher"], "Example Press")
        self.assertEqual(record["metadata"]["last_page"], "20")
        self.assertEqual(record["field_sources"]["title"]["kind"], "browser")
        self.assertEqual(record["files"][0]["path"], str(saved.resolve()))
        self.assertEqual(record["open_count"], 0)
        self.assertIsNone(record["last_opened_at"])
        captured = [observation for observation in record["observations"] if observation["kind"] == "browser"]
        self.assertIn(data, [observation["raw"] for observation in captured])
        self.assertEqual(json.loads(self.cli("library", "export"))["documents"], records)
        self.assertEqual(json.loads(self.cli("library", "show", record["id"][:12])), record)

    def test_arxiv_metadata_is_captured_during_existing_abstract_fetch(self):
        abstract = '''<meta name="citation_title" content="A &amp; B">
        <meta name="citation_author" content="Jane Doe">
        <meta name="citation_author" content="John Roe">
        <meta name="citation_date" content="2017/06/12">
        <meta name="citation_doi" content="10.1234/attention">
        <a href="/abs/1706.03762v7">v7</a>'''

        def fetch(url, path):
            if "/abs/" in url:
                path.write_text(abstract)
            else:
                path.write_bytes(b"%PDF-1.7\narxiv paper")

        with patch.object(app, "fetch", side_effect=fetch) as mocked:
            self.cli("open", "--no-open", "https://arxiv.org/abs/1706.03762")
        self.assertEqual(mocked.call_count, 2)
        record = app.library_records(self.papers)[0]
        self.assertEqual(record["metadata"]["arxiv_id"], "1706.03762v7")
        self.assertEqual(record["metadata"]["authors"], ["Jane Doe", "John Roe"])
        self.assertEqual(record["metadata"]["title"], "A & B")
        self.assertEqual(record["field_sources"]["title"]["kind"], "arxiv")

    def test_json_ld_graph_uses_publication_not_organization(self):
        fields = app.normalize_metadata({"json_ld": [{"@graph": [
            {"@type": "Organization", "name": "Wrong title"},
            {"@type": ["ScholarlyArticle"], "headline": "Actual title", "datePublished": "2022-02-01",
             "author": [{"givenName": "Jane", "familyName": "Doe"}, "John Roe"],
             "identifier": {"propertyID": "DOI", "value": "doi:10.1234/abc"},
             "abstract": "Abstract from structured data"},
        ]}]})
        self.assertEqual(fields["title"], "Actual title")
        self.assertEqual(fields["authors"], ["Jane Doe", "John Roe"])
        self.assertEqual(fields["doi"], "10.1234/abc")
        self.assertEqual(fields["year"], "2022")
        self.assertNotIn("title", app.normalize_metadata({"page_title": "Generic page"}))

    def test_doi_is_selected_among_multiple_identifiers(self):
        fields = app.normalize_metadata({"meta": {"dc.identifier": ["https://example.org/paper", "doi:10.1234/music"]}})
        self.assertEqual(fields["doi"], "10.1234/music")

    def test_identical_bytes_share_record_but_different_versions_do_not(self):
        first = self.pdf("first.pdf")
        second = self.pdf("copy.pdf")
        changed = self.pdf("version2.pdf", b"%PDF-1.7\nchanged paper")
        identifier = app.record_document(first, self.papers)
        self.assertEqual(app.record_document(second, self.papers), identifier)
        self.assertNotEqual(app.record_document(changed, self.papers), identifier)
        records = app.library_records(self.papers)
        self.assertEqual(len(records), 2)
        original = next(record for record in records if record["id"] == identifier)
        self.assertEqual(len(original["files"]), 2)
        self.assertEqual(original["metadata"]["title"], "first")

    def test_parallel_saves_keep_one_record_and_one_copy_of_each_observation(self):
        path = self.pdf()
        observation = dict(kind="browser", url="https://example.org/paper", metadata={"title": "Paper"},
                           raw={"meta": {"citation_title": ["Paper"]}})
        with ThreadPoolExecutor(max_workers=4) as workers:
            identifiers = list(workers.map(lambda _: app.record_document(
                path, self.papers, source_url="https://example.org/paper.pdf", observations=[observation]), range(8)))
        self.assertEqual(len(set(identifiers)), 1)
        records = app.library_records(self.papers)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["observations"]), 3)

    def test_parallel_metadata_updates_do_not_lose_fields(self):
        path = self.pdf()
        fields = {"title": "Paper", "doi": "10.1234/abc", "year": "2024", "journal": "Journal",
                  "publisher": "Press", "abstract": "Abstract", "volume": "2", "issue": "1"}

        def save(item):
            key, value = item
            observation = dict(kind="browser", url="https://example.org/paper", metadata={key: value}, raw={key: value})
            return app.record_document(path, self.papers, observations=[observation])

        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(save, fields.items()))
        record = app.library_records(self.papers)[0]
        self.assertEqual(record["metadata"], fields)

    def test_scan_indexes_existing_files_without_moving_and_preserves_arxiv_id(self):
        old = self.pdf("Old Paper [hep-th_9901001v2].pdf")
        self.pdf("Modern Paper [1706.03762v7].pdf", b"%PDF-1.7\nother")
        self.pdf("ignored.pdf", b"<html>error</html>")
        self.cli("library", "scan")
        self.cli("library", "scan")
        records = app.library_records(self.papers)
        self.assertEqual(len(records), 2)
        self.assertEqual({record["metadata"]["arxiv_id"] for record in records}, {"hep-th/9901001v2", "1706.03762v7"})
        self.assertTrue(old.exists())
        self.assertTrue(all(record["open_count"] == 0 for record in records))

    def test_local_files_stay_in_place_and_missing_files_remain_in_catalog(self):
        outside = self.home / "local.pdf"
        outside.write_bytes(b"%PDF-1.7\nlocal")
        self.assertEqual(Path(self.cli("open", "--no-open", outside.as_uri()).strip()), outside)
        record = app.library_records(self.papers)[0]
        app.mark_opened(self.papers, record["id"])
        outside.unlink()
        record = app.library_records(self.papers)[0]
        self.assertFalse(record["files"][0]["exists"])
        self.assertEqual(record["open_count"], 1)
        self.assertIsNotNone(record["last_opened_at"])

    def test_invalid_metadata_is_rejected_before_downloading(self):
        for data in ["not json", "[]", '{"meta":{"citation_author":"not a list"}}',
                     '{"page_title":42}', '{"json_ld":{}}', "x" * 24001]:
            incoming = "sioyek-open://open?" + urlencode({"url": "https://example.org/paper.pdf", "metadata": data})
            with self.subTest(data=data[:40]), patch.object(app, "fetch") as fetch, \
                    patch.object(app.sys, "argv", ["sioyek-open", "open", "--no-open", incoming]), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(), 1)
                fetch.assert_not_called()
        self.assertFalse(app.library_path(self.papers).exists())

    def test_future_schema_is_not_modified(self):
        with contextlib.closing(sqlite3.connect(app.library_path(self.papers))) as database, database:
            database.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ValueError, "newer version"):
            app.record_document(self.pdf(), self.papers)
        with contextlib.closing(sqlite3.connect(app.library_path(self.papers))) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_empty_library_list_does_not_create_database(self):
        self.assertEqual(self.cli("library", "list"), "")
        self.assertEqual(json.loads(self.cli("library", "export")), {"schema_version": 1, "documents": []})
        self.assertFalse(app.library_path(self.papers).exists())


if __name__ == "__main__":
    unittest.main()
