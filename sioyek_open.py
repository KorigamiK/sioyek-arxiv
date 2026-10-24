#!/usr/bin/env python3
"""Open arXiv papers, web PDFs, and local file:// PDFs in Sioyek."""

import argparse
import fcntl
import hashlib
import html
import json
import os
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


VERSION = "0.4.0"
BOOKMARKLET = "javascript:" + "".join(line.strip() for line in r"""
(()=>{
let u=new URL(location.href),same=true;
const meta={};
for(const e of [...document.querySelectorAll('meta[name],meta[property]')].slice(0,200)){
const k=(e.getAttribute('name')||e.getAttribute('property')||'').toLowerCase();
if(/^(citation_|dc[.:]|dcterms[.:]|prism[.:]|og:|twitter:)/.test(k)||['description','author','keywords'].includes(k)){
const v=(e.getAttribute('content')||'').trim().slice(0,2000);
if(v)(meta[k]??=[]).push(v);
}}
const arxiv=/^(www\.|export\.)?arxiv\.org$/.test(u.hostname)&&/^\/(abs|html|pdf)\//.test(u.pathname);
const pdf=/\.pdf$/i.test(u.pathname)||document.contentType==='application/pdf';
if(!arxiv&&!pdf){
const candidate=meta.citation_pdf_url?.[0]||document.querySelector('link[type="application/pdf"]')?.href;
if(candidate){try{u=new URL(candidate,location.href);}catch{alert('Invalid PDF link on this page.');return;}}
else{const s=prompt('Paste an arXiv, PDF, or local file:// PDF URL:','');if(!s)return;
try{u=new URL(s.trim());same=u.href===location.href;}catch{alert('Please enter a complete URL.');return;}}
}
if(!['https:','http:','file:'].includes(u.protocol)){alert('Use an http://, https://, or local file:// URL.');return;}
const m=same?{page_url:location.href.slice(0,4000),page_title:document.title.slice(0,1000),
canonical_url:(document.querySelector('link[rel="canonical"]')?.href||'').slice(0,4000),meta,json_ld:[]}:{};
if(same){for(const e of [...document.querySelectorAll('script[type="application/ld+json"]')].slice(0,4)){
if(e.textContent.length<=12000){try{m.json_ld.push(JSON.parse(e.textContent));}catch{}}
}}
let encoded=encodeURIComponent(JSON.stringify(m));
while(encoded.length>24000&&m.json_ld?.length){m.json_ld.pop();encoded=encodeURIComponent(JSON.stringify(m));}
while(encoded.length>24000&&Object.keys(m.meta||{}).length){delete m.meta[Object.keys(m.meta).pop()];encoded=encodeURIComponent(JSON.stringify(m));}
if(encoded.length>24000)encoded=encodeURIComponent('{}');
location.href='sioyek-open://open?url='+encodeURIComponent(u.href)+'&metadata='+encoded;
})()
""".splitlines())
ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.-]*/\d{7})(?:v[1-9]\d*)?"


def config_path():
    return Path.home() / ".config/sioyek-open/config.json"


def support_dir():
    return Path.home() / "Library/Application Support/Open in Sioyek"


def handler_app():
    return Path.home() / "Applications/Open in Sioyek.app"


def owns_handler(path):
    try:
        info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
        return info.get("CFBundleIdentifier") == "local.sioyek.open"
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def load_config():
    path = config_path()
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError(f"Expected a JSON object in {path}.")
    for key in ("papers_dir", "sioyek_app"):
        if key in config and (not isinstance(config[key], str) or not config[key].strip()):
            raise ValueError(f"Expected a nonempty path for {key} in {path}.")
    return config


def find_sioyek(config, override=None):
    if override:
        app = Path(override).expanduser().absolute()
        if not app.is_dir():
            raise ValueError(f"Sioyek app not found: {app}")
        return app
    candidates = []
    if config.get("sioyek_app"):
        candidates.append(Path(config["sioyek_app"]).expanduser())
    candidates.extend((Path("/Applications/sioyek.app"), Path.home() / "Applications/sioyek.app"))
    for app in candidates:
        if app.is_dir():
            return app.absolute()
    raise ValueError("Install Sioyek first, or run sioyek-open setup --sioyek-app /path/to/sioyek.app")


def unwrap_url(value):
    url = urlsplit(value)
    if url.scheme == "sioyek-open":
        if url.netloc != "open" or url.path not in ("", "/"):
            raise ValueError("Expected sioyek-open://open?url=...")
        values = parse_qs(url.query).get("url", [])
        if len(values) != 1 or not values[0]:
            raise ValueError("Expected one URL in sioyek-open://open?url=...")
        return values[0]
    return value


def bookmark_metadata(value):
    url = urlsplit(value)
    if url.scheme != "sioyek-open":
        return {}
    values = parse_qs(url.query).get("metadata", [])
    if not values:
        return {}
    if len(values) != 1 or len(values[0]) > 24000:
        raise ValueError("Bookmarklet metadata is too large or repeated. Please update your bookmarklet.")
    try:
        data = json.loads(values[0])
    except (ValueError, RecursionError) as error:
        raise ValueError("Bookmarklet metadata is not valid JSON.") from error
    if not isinstance(data, dict):
        raise ValueError("Bookmarklet metadata must be a JSON object.")
    for key in ("page_url", "page_title", "canonical_url"):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"Invalid bookmarklet metadata: {key} must be text.")
    tags = data.get("meta", {})
    if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, list)
                                       or any(not isinstance(item, str) for item in v)
                                       for k, v in tags.items()):
        raise ValueError("Invalid bookmarklet citation tags.")
    if not isinstance(data.get("json_ld", []), list):
        raise ValueError("Invalid bookmarklet structured metadata.")
    return data


def normalize_metadata(data):
    """Keep citation fields useful for exports; retain the original data separately."""
    tags = {key.lower(): values for key, values in data.get("meta", {}).items()}

    def values(*keys):
        for key in keys:
            found = [" ".join(value.split()) for value in tags.get(key, []) if value.strip()]
            if found:
                return list(dict.fromkeys(found))
        return []

    fields = {}
    aliases = {
        "title": ("citation_title", "dc.title", "dc:title", "og:title", "twitter:title"),
        "authors": ("citation_author", "dc.creator", "dc:creator", "dcterms.creator", "author"),
        "date": ("citation_publication_date", "citation_date", "dc.date", "dc:date", "prism.publicationdate"),
        "doi": ("citation_doi", "dc.identifier", "dc:identifier", "prism.doi"),
        "journal": ("citation_journal_title", "prism.publicationname"),
        "conference": ("citation_conference_title",),
        "publisher": ("citation_publisher", "dc.publisher", "dc:publisher"),
        "volume": ("citation_volume", "prism.volume"),
        "issue": ("citation_issue", "prism.number"),
        "first_page": ("citation_firstpage", "prism.startingpage"),
        "last_page": ("citation_lastpage", "prism.endingpage"),
        "abstract": ("citation_abstract", "dc.description", "dc:description", "description", "og:description"),
        "keywords": ("citation_keywords", "dc.subject", "dc:subject", "keywords"),
        "language": ("citation_language", "dc.language", "dc:language"),
        "issn": ("citation_issn", "prism.issn"),
        "isbn": ("citation_isbn", "prism.isbn"),
        "pdf_url": ("citation_pdf_url",),
    }
    for field, keys in aliases.items():
        found = values(*keys)
        if found:
            fields[field] = found if field in ("authors", "keywords") else found[0]
    doi_candidates = [value for key in aliases["doi"] for value in tags.get(key, [])]

    # JSON-LD often supplies fields missing from citation meta tags. Only use
    # publication objects, rather than mistaking an Organization for the paper.
    pending = list(data.get("json_ld", []))
    for _ in range(100):
        if not pending:
            break
        item = pending.pop(0)
        if isinstance(item, list):
            pending.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("@graph"), list):
            pending.extend(item["@graph"])
        types = item.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if not isinstance(types, list) or not any(t in ("ScholarlyArticle", "Article", "Book", "Report", "CreativeWork")
                                                  for t in types if isinstance(t, str)):
            continue
        for field, keys in {"title": ("headline", "name"), "date": ("datePublished",),
                            "abstract": ("abstract", "description"), "language": ("inLanguage",)}.items():
            for key in keys:
                if isinstance(item.get(key), str) and item[key].strip():
                    fields.setdefault(field, item[key].strip())
                    break
        authors = item.get("author", [])
        authors = authors if isinstance(authors, list) else [authors]
        names = []
        for author in authors:
            if isinstance(author, dict):
                author = author.get("name") or " ".join(str(author.get(k, "")) for k in ("givenName", "familyName"))
            if isinstance(author, str) and author.strip():
                names.append(author.strip())
        if names:
            fields.setdefault("authors", list(dict.fromkeys(names)))
        identifiers = item.get("identifier", [])
        identifiers = identifiers if isinstance(identifiers, list) else [identifiers]
        for doi in identifiers:
            if isinstance(doi, dict):
                doi = doi.get("value")
            if isinstance(doi, str):
                doi_candidates.append(doi)
        publisher = item.get("publisher")
        if isinstance(publisher, dict):
            publisher = publisher.get("name")
        if isinstance(publisher, str) and publisher.strip():
            fields.setdefault("publisher", publisher.strip())
    fields.pop("doi", None)
    for candidate in doi_candidates:
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", candidate.strip(), flags=re.I)
        if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            fields["doi"] = doi
            break
    if "date" in fields:
        year = re.search(r"\b(\d{4})\b", fields["date"])
        if year:
            fields["year"] = year[1]
    return fields


def document_url(value):
    value = unwrap_url(value)
    url = urlsplit(value)
    if url.scheme == "file":
        if url.netloc.lower() not in ("", "localhost") or not url.path.startswith("/"):
            raise ValueError("Use a local file:// PDF URL, without a remote hostname.")
        path = unquote(url.path)
        if "\x00" in path:
            raise ValueError("The local file URL contains an invalid path.")
        return value
    if (url.scheme not in ("https", "http") or not url.hostname
            or url.username is not None or url.password is not None
            or url.port == 0):
        raise ValueError("Use an arXiv URL, an http(s) PDF URL, or a local file:// PDF URL.")
    # MIREX publishes www links, but its HTTPS certificate covers music-ir.org.
    # Canonicalize this known alias before both downloading and naming the PDF.
    # Do not guess alternate hosts for arbitrary certificate failures.
    if url.hostname == "www.music-ir.org":
        authority = "music-ir.org" + (f":{url.port}" if url.port is not None else "")
        url = url._replace(netloc=authority)
    # Fragments such as #page=3 do not identify a different downloadable file.
    return url._replace(fragment="").geturl()


def paper_id(value):
    url = urlsplit(unwrap_url(value))
    if (url.scheme not in ("https", "http")
            or url.hostname not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org")
            or url.username or url.password or url.port not in (None, 80, 443)):
        raise ValueError("Open an arXiv abstract, HTML, or PDF page first.")
    match = re.fullmatch(r"/(?:abs|pdf|html)/(" + ARXIV_ID + r")(?:\.pdf)?/?",
                         unquote(url.path))
    if not match:
        raise ValueError("This URL does not identify an arXiv paper.")
    return match.group(1)


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.links = []
        self.tags = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "citation_title":
            self.title = attrs.get("content", "")
        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            content = attrs.get("content")
            if key and content:
                self.tags.setdefault(key, []).append(content)
        if tag == "a":
            self.links.append(attrs.get("href", ""))


def fetch(url, destination):
    try:
        subprocess.run([
            "/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
            "--proto", "=http,https", "--proto-redir", "=http,https", "--connect-timeout", "15",
            "--max-time", "180", "--retry", "2", "--retry-delay", "2",
            "--output", str(destination), url,
        ], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        host = urlsplit(url).hostname
        if error.returncode == 60:
            message = f"HTTPS certificate verification failed for {host}. The download was stopped."
        else:
            message = f"Download failed for {host} (curl error {error.returncode})."
        detail = (error.stderr or "").strip()
        raise ValueError(message + (f"\n\n{detail}" if detail else "")) from error


def describe(identifier, observations=None):
    with tempfile.TemporaryDirectory(prefix="sioyek-open-") as temp:
        page = Path(temp) / "abstract.html"
        fetch("https://arxiv.org/abs/" + identifier, page)
        metadata = Metadata()
        metadata.feed(page.read_text(encoding="utf-8"))
    if not metadata.title.strip():
        raise ValueError("arXiv did not return a paper title. Please try again.")
    if not re.search(r"v\d+$", identifier):
        versions = []
        for link in metadata.links:
            match = re.fullmatch(r"/abs/" + re.escape(identifier) + r"v([1-9]\d*)",
                                 urlsplit(link).path)
            if match:
                versions.append(int(match.group(1)))
        if not versions:
            raise ValueError("Could not identify the paper version on arXiv.")
        identifier += "v" + str(max(versions))
    if observations is not None:
        raw = {"meta": metadata.tags}
        fields = normalize_metadata(raw)
        fields["arxiv_id"] = identifier
        observations.append(dict(kind="arxiv", url="https://arxiv.org/abs/" + identifier,
                                 raw=raw, metadata=fields))
    return " ".join(metadata.title.split()), identifier


def filename(title, identifier):
    title = unicodedata.normalize("NFC", title)
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "-", title).strip(" .")
    # Leave room for the identifier and .pdf within macOS's filename limit.
    title = title.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{title or 'Paper'} [{identifier.replace('/', '_')}].pdf"


def is_pdf(path):
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(5) == b"%PDF-"


def save_paper(url, directory, observations=None):
    identifier = paper_id(url)
    title, identifier = describe(identifier, observations) if observations is not None else describe(identifier)
    return download_pdf("https://arxiv.org/pdf/" + identifier, directory, filename(title, identifier))


def download_pdf(url, directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    # Serialize repeated clicks, and never expose a partially downloaded PDF.
    with (directory / ".sioyek-open.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            if is_pdf(destination):
                return destination
            raise ValueError(f"Existing file is not a PDF; move it aside first: {destination}")
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".sioyek-open-", suffix=".part",
                                         delete=False) as stream:
            partial = Path(stream.name)
        try:
            fetch(url, partial)
            if not is_pdf(partial):
                raise ValueError("The URL returned something other than a PDF; nothing was saved.")
            # link() refuses to overwrite an existing file, even in a race.
            os.link(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    return destination


def open_document(value, directory, observations=None):
    value = document_url(value)
    url = urlsplit(value)
    if url.scheme == "file":
        path = Path(unquote(url.path))
        if not path.is_file():
            raise ValueError(f"Local PDF not found: {path}")
        if not is_pdf(path):
            raise ValueError(f"The local file is not a PDF: {path}")
        return path
    if url.hostname in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        return save_paper(value, directory, observations)
    # Keep the URL's readable filename and distinguish documents with the same
    # basename. Include the query in the identity, but never expose it in names.
    title = unquote(url.path.rsplit("/", 1)[-1])
    title = re.sub(r"\.pdf$", "", title, flags=re.IGNORECASE) or "Document"
    identifier = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return download_pdf(value, directory, filename(title, identifier))


def library_path(directory):
    return directory / "library.sqlite3"


def library_database(directory):
    directory.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(library_path(directory), timeout=15)
    try:
        database.execute("PRAGMA foreign_keys = ON")
        version = database.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("This library requires a newer version of sioyek-open.")
        database.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL,
                metadata TEXT NOT NULL, field_sources TEXT NOT NULL,
                added_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_opened_at TEXT, open_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                modified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                document_id TEXT NOT NULL REFERENCES documents(id),
                fingerprint TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
                metadata TEXT NOT NULL, raw TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                PRIMARY KEY (document_id, fingerprint)
            );
            PRAGMA user_version = 1;
        """)
    except BaseException:
        database.close()
        raise
    return database


def file_metadata(path):
    title = path.stem
    fields = {}
    match = re.search(r" \[(" + ARXIV_ID.replace("/", "_") + r")\]$", title)
    if match:
        fields["arxiv_id"] = match[1].replace("_", "/")
        title = title[:match.start()]
    else:
        title = re.sub(r" \[[0-9a-f]{12}\]$", "", title)
    fields["title"] = title or "Document"
    return fields


def record_document(path, directory, source_url=None, observations=()):
    """Catalog a validated file atomically; identical bytes share one document."""
    path = path.resolve()
    if not is_pdf(path):
        raise ValueError(f"Cannot catalog a non-PDF file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    identifier = digest.hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fallback = dict(kind="filename", url=path.as_uri(), raw={"filename": path.name}, metadata=file_metadata(path))
    incoming = [fallback, *observations]
    for observation in observations:
        if observation["kind"] == "browser" and observation["raw"].get("page_title", "").strip():
            title = observation["raw"]["page_title"].strip()
            incoming.append(dict(kind="page", url=observation["url"], raw={"page_title": title},
                                 metadata={"title": title}))
    if source_url:
        incoming.append(dict(kind="url", url=source_url, metadata={}, raw={}))
    priorities = {"filename": 0, "url": 0, "page": 1, "browser": 2, "arxiv": 3}
    with closing(library_database(directory)) as database, database:
        # Lock before reading so simultaneous saves merge the latest metadata.
        database.execute("BEGIN IMMEDIATE")
        row = database.execute("SELECT metadata, field_sources FROM documents WHERE id = ?", (identifier,)).fetchone()
        fields, origins = (json.loads(row[0]), json.loads(row[1])) if row else ({}, {})
        for observation in incoming:
            kind = observation["kind"]
            for key, value in observation["metadata"].items():
                previous = priorities.get(origins.get(key, {}).get("kind", "filename"), 0)
                if value and (key not in fields or priorities[kind] > previous
                              or (kind != "filename" and priorities[kind] == previous)):
                    fields[key] = value
                    origins[key] = {"kind": kind, "url": observation["url"]}
        database.execute("""
            INSERT INTO documents(id, size_bytes, metadata, field_sources, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,
                field_sources=excluded.field_sources, updated_at=excluded.updated_at
        """, (identifier, path.stat().st_size, json.dumps(fields, ensure_ascii=False),
              json.dumps(origins, ensure_ascii=False), now, now))
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        database.execute("""
            INSERT INTO files(path, document_id, modified_at) VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET document_id=excluded.document_id, modified_at=excluded.modified_at
        """, (str(path), identifier, modified))
        for observation in incoming:
            encoded = json.dumps(observation, sort_keys=True, ensure_ascii=False)
            fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            database.execute("""
                INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, fingerprint) DO UPDATE SET last_seen_at=excluded.last_seen_at
            """, (identifier, fingerprint, observation["kind"], observation["url"],
                  json.dumps(observation["metadata"], ensure_ascii=False),
                  json.dumps(observation["raw"], ensure_ascii=False), now, now))
    return identifier


def library_records(directory):
    if not library_path(directory).exists():
        return []
    with closing(library_database(directory)) as database, database:
        database.execute("BEGIN")
        records = []
        for row in database.execute("SELECT * FROM documents ORDER BY added_at, id"):
            identifier, size, metadata, origins, added, updated, opened, count = row
            files = [dict(path=path, modified_at=modified, exists=Path(path).is_file())
                     for path, modified in database.execute(
                         "SELECT path, modified_at FROM files WHERE document_id = ? ORDER BY path", (identifier,))]
            observations = [dict(kind=kind, url=url, metadata=json.loads(fields), raw=json.loads(raw),
                                 first_seen_at=first, last_seen_at=last)
                            for kind, url, fields, raw, first, last in database.execute(
                                "SELECT kind, url, metadata, raw, first_seen_at, last_seen_at FROM observations "
                                "WHERE document_id = ? ORDER BY first_seen_at, fingerprint", (identifier,))]
            records.append(dict(id=identifier, sha256=identifier, size_bytes=size, metadata=json.loads(metadata),
                                field_sources=json.loads(origins), added_at=added, updated_at=updated,
                                last_opened_at=opened, open_count=count, files=files, observations=observations))
    return records


def mark_opened(directory, identifier):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(library_database(directory)) as database, database:
        database.execute("UPDATE documents SET last_opened_at = ?, open_count = open_count + 1 WHERE id = ?",
                         (now, identifier))


def library_command(args, directory):
    if args.library_command == "scan":
        count = 0
        for path in sorted(directory.rglob("*")):
            if path.suffix.lower() == ".pdf" and path.is_file():
                if not is_pdf(path):
                    print(f"Skipped non-PDF: {path}", file=sys.stderr)
                    continue
                record_document(path, directory)
                count += 1
        print(f"Indexed {count} PDF files. Library: {library_path(directory)}")
        return
    records = library_records(directory)
    if args.library_command == "export":
        print(json.dumps(dict(schema_version=1, documents=records), ensure_ascii=False, indent=2))
    elif args.library_command == "show":
        matches = [record for record in records if record["id"].startswith(args.id)]
        if len(matches) != 1:
            raise ValueError("Use a unique document ID or prefix from library list.")
        print(json.dumps(matches[0], ensure_ascii=False, indent=2))
    else:
        for record in records:
            fields = record["metadata"]
            print(f"{record['id'][:12]}\t{fields.get('year', '')}\t{fields.get('title', 'Document')}")


def notify_error(message):
    # Pass the message as argv rather than interpolating it into AppleScript.
    subprocess.run([
        "/usr/bin/osascript", "-e",
        'on run argv\ndisplay alert "Open in Sioyek" message (item 1 of argv)\nend run',
        message,
    ], check=False)


def applescript_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launcher_command():
    # The Homebrew wrapper supplies its stable opt path. Never bake a Cellar
    # version or Python patch version into the per-user application.
    installed = os.environ.get("SIOYEK_OPEN_LAUNCHER")
    if installed:
        return [installed]
    return [sys.executable, str(Path(__file__).absolute())]


def bookmarklet_page(directory):
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Open in Sioyek</title>'
        '<style>body{font:18px system-ui;max-width:740px;margin:60px auto;line-height:1.6;'
        'padding:0 24px;color:#20242b;background:#f6f7fa}'
        'a.bookmark{display:inline-block;padding:12px 18px;background:#2456b8;'
        'color:white;border-radius:8px}textarea{box-sizing:border-box;width:100%;'
        'height:140px;padding:12px}code{overflow-wrap:anywhere}</style>'
        '<h1>Open in Sioyek</h1>'
        '<p>Drag this button onto your Firefox bookmarks toolbar:</p>'
        '<p><a class="bookmark" href="' + html.escape(BOOKMARKLET, quote=True)
        + '">Open in Sioyek</a></p>'
        '<p>On an arXiv paper page, a direct PDF URL, or a local '
        '<code>file://</code> PDF, click the bookmark. '
        'Allow Firefox to open <b>Open in Sioyek</b> when prompted. '
        'Web PDFs are saved in <code>' + html.escape(str(directory)) + '</code>. '
        'arXiv papers keep their title-and-version names; other PDFs use their '
        'URL filename and a short identifier. Local PDFs open in place.</p>'
        '<p>The local library records each PDF and any citation metadata exposed '
        'by the page. On article pages with a declared PDF link, the bookmark '
        'downloads that PDF and keeps the article metadata. Replace an older '
        'bookmark with this button to enable metadata capture.</p>'
        '<p>If the toolbar is hidden, press <b>⌘⇧B</b>. '
        'Alternatively, create a bookmark and paste this into its URL field:</p>'
        '<textarea readonly aria-label="Bookmarklet URL">'
        + html.escape(BOOKMARKLET) + '</textarea>'
        '<p>If a browser blocks bookmarklets in its PDF viewer, copy the PDF URL, '
        'go to an ordinary web page, click this bookmark, and paste the URL into '
        'the prompt. This also works for local <code>file://</code> PDF URLs '
        'and PDF download URLs that do not end in <code>.pdf</code>.</p>'
        '<p>Change the download folder with '
        '<code>sioyek-open setup --papers-dir /path/to/papers</code>.</p></html>'
    )


def open_bookmarklet(browser=None):
    page = support_dir() / "bookmarklet.html"
    if not page.exists():
        raise ValueError("Run sioyek-open setup first.")
    command = ["/usr/bin/open"]
    if browser:
        command.extend(["-a", browser])
    subprocess.run(command + [str(page)], check=True)


def setup(args):
    if sys.platform != "darwin":
        raise ValueError("The URL handler requires macOS.")
    config = load_config()
    directory = Path(args.papers_dir or config.get("papers_dir", "~/Downloads/Papers")).expanduser().absolute()
    sioyek = find_sioyek(config, args.sioyek_app)
    support = support_dir()
    app = handler_app()
    if app.exists() and not owns_handler(app):
        raise ValueError(f"Refusing to replace an unrelated application: {app}")
    app.parent.mkdir(parents=True, exist_ok=True)
    log = support / "download.log"
    command = shlex.join(launcher_command() + ["open", "--notify"])
    script = (
        "on open location incomingURL\n"
        "    do shell script " + applescript_string(command + " ")
        + " & quoted form of incomingURL & "
        + applescript_string(" >> " + shlex.quote(str(log)) + " 2>&1 < /dev/null &")
        + "\nend open location\n"
    )
    # Build and sign before replacing an existing installation.
    with tempfile.TemporaryDirectory(prefix=".sioyek-open-", dir=app.parent) as temporary:
        staging = Path(temporary)
        source = staging / "handler.applescript"
        source.write_text(script, encoding="utf-8")
        built = staging / app.name
        subprocess.run(["/usr/bin/osacompile", "-o", str(built), str(source)], check=True)
        plist = built / "Contents/Info.plist"
        info = plistlib.loads(plist.read_bytes())
        # osacompile inserts unrelated privacy descriptions into every applet.
        info = {key: value for key, value in info.items()
                if not (key.startswith("NS") and key.endswith("UsageDescription"))}
        info.update(
            CFBundleIdentifier="local.sioyek.open",
            CFBundleName="Open in Sioyek",
            CFBundleShortVersionString=VERSION,
            LSUIElement=True,
            CFBundleURLTypes=[dict(CFBundleURLName="Open in Sioyek",
                                  CFBundleURLSchemes=["sioyek-open"],
                                  CFBundleTypeRole="Viewer")],
        )
        plist.write_bytes(plistlib.dumps(info))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(built)], check=True)
        previous = staging / "previous.app"
        if app.exists():
            app.rename(previous)
        try:
            built.rename(app)
            subprocess.run([
                "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                "-f", str(app),
            ], check=True)
        except (OSError, subprocess.CalledProcessError):
            if app.exists():
                shutil.rmtree(app)
            if previous.exists():
                previous.rename(app)
            raise
    config.update(papers_dir=str(directory), sioyek_app=str(sioyek))
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    support.mkdir(parents=True, exist_ok=True)
    (support / "handler.applescript").write_text(script, encoding="utf-8")
    (support / "bookmarklet.txt").write_text(BOOKMARKLET + "\n", encoding="utf-8")
    (support / "bookmarklet.html").write_text(bookmarklet_page(directory), encoding="utf-8")
    print(f"Installed: {app}\nPapers: {directory}\nSioyek: {sioyek}\nConfig: {config_path()}")
    print(f"Bookmarklet page: {support / 'bookmarklet.html'}")
    if not args.no_open:
        open_bookmarklet(args.browser)


def uninstall():
    app = handler_app()
    if app.exists():
        if not owns_handler(app):
            raise ValueError(f"Refusing to remove an unrelated application: {app}")
        subprocess.run([
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
            "-u", str(app),
        ], check=True)
        shutil.rmtree(app)
    if support_dir().exists():
        shutil.rmtree(support_dir())
    print("Removed the per-user URL handler and bookmarklet setup files.")
    print(f"Saved PDFs and settings in {config_path()} were kept. Remove the browser bookmark manually.")


def main():
    parser = argparse.ArgumentParser(prog="sioyek-open", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sioyek-open {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("setup", help="Install or update your macOS URL handler")
    install.add_argument("--papers-dir", help="PDF folder (default: existing setting or ~/Downloads/Papers)")
    install.add_argument("--sioyek-app", help="Path to Sioyek.app (auto-detected by default)")
    install.add_argument("--no-open", action="store_true", help="Do not open the bookmarklet page")
    install.add_argument("--browser", help="Browser to use for the setup page, e.g. Firefox")
    download = commands.add_parser("open", help="Open an arXiv paper, web PDF, or local PDF in Sioyek")
    download.add_argument("url", help="arXiv URL, http(s) PDF URL, file:// PDF URL, or sioyek-open://open?url=...")
    download.add_argument("--no-open", action="store_true", help="Download without opening Sioyek")
    download.add_argument("--notify", action="store_true", help="Show errors in a macOS alert")
    bookmark = commands.add_parser("bookmarklet", help="Open the bookmarklet setup page")
    bookmark.add_argument("--browser", help="Browser to use, e.g. Firefox")
    bookmark.add_argument("--print", action="store_true", dest="print_url", help="Print the bookmarklet URL")
    commands.add_parser("status", help="Show your current setup")
    library = commands.add_parser("library", help="Inspect, import, or export your local PDF catalog")
    catalog = library.add_subparsers(dest="library_command", required=True)
    catalog.add_parser("list", help="List document IDs and titles")
    show = catalog.add_parser("show", help="Show a document's metadata and sources as JSON")
    show.add_argument("id", help="Document SHA-256 or a unique prefix from library list")
    catalog.add_parser("scan", help="Index existing PDFs in the papers folder without moving them")
    catalog.add_parser("export", help="Write the complete library as JSON to stdout")
    commands.add_parser("uninstall", help="Remove your URL handler, keeping saved PDFs and settings")
    args = parser.parse_args()
    try:
        if args.command == "setup":
            setup(args)
        elif args.command == "bookmarklet":
            if args.print_url:
                print(BOOKMARKLET)
            else:
                open_bookmarklet(args.browser)
        elif args.command == "uninstall":
            uninstall()
        elif args.command == "status":
            config = load_config()
            print(f"Version: {VERSION}\nURL handler installed: {owns_handler(handler_app())}")
            print(f"Papers: {config.get('papers_dir', '~/Downloads/Papers')}")
            print(f"Sioyek: {config.get('sioyek_app', 'auto-detect')}\nConfig: {config_path()}")
            directory = Path(config.get("papers_dir", "~/Downloads/Papers")).expanduser()
            print(f"Library: {library_path(directory)}")
        elif args.command == "library":
            config = load_config()
            directory = Path(config.get("papers_dir", "~/Downloads/Papers")).expanduser()
            library_command(args, directory)
        elif args.command == "open":
            source_url = document_url(args.url)
            browser = bookmark_metadata(args.url)
            config = load_config()
            directory = Path(config.get("papers_dir", "~/Downloads/Papers")).expanduser()
            app = find_sioyek(config) if not args.no_open else None
            observations = [dict(kind="url", url=unwrap_url(args.url), metadata={}, raw={})]
            if browser:
                observations.append(dict(kind="browser", url=browser.get("page_url") or source_url,
                                         raw=browser, metadata=normalize_metadata(browser)))
            destination = open_document(args.url, directory, observations)
            try:
                identifier = record_document(destination, directory, source_url, observations)
            except (sqlite3.Error, OSError) as error:
                raise ValueError(f"PDF saved at {destination}, but the library at {library_path(directory)} "
                                 f"could not be updated: {error}") from error
            print(destination, flush=True)
            if app:
                subprocess.run(["/usr/bin/open", "-a", str(app), str(destination)], check=True)
                mark_opened(directory, identifier)
    except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as error:
        message = str(error)
        print(message, file=sys.stderr, flush=True)
        if getattr(args, "notify", False):
            notify_error(message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
