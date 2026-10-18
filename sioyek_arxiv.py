#!/usr/bin/env python3
"""Save an arXiv paper under its title and open the local PDF in Sioyek."""

import argparse
import fcntl
import html
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


VERSION = "0.1.0"
BOOKMARKLET = (
    "javascript:(()=>{if(!/^(www\\.|export\\.)?arxiv\\.org$/.test(location.hostname))"
    "{alert('Open an arXiv paper page first.');return;}"
    "location.href='sioyek-arxiv://open?url='+encodeURIComponent(location.href);})()"
)
ARXIV_ID = r"(?:\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.-]*/\d{7})(?:v[1-9]\d*)?"


def config_path():
    return Path.home() / ".config/sioyek/arxiv.json"


def support_dir():
    return Path.home() / "Library/Application Support/Sioyek arXiv"


def handler_app():
    return Path.home() / "Applications/Sioyek arXiv.app"


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
    raise ValueError("Install Sioyek first, or run sioyek-arxiv setup --sioyek-app /path/to/sioyek.app")


def paper_id(value):
    url = urlsplit(value)
    if url.scheme == "sioyek-arxiv":
        if url.netloc != "open":
            raise ValueError("Expected sioyek-arxiv://open?url=...")
        value = parse_qs(url.query).get("url", [""])[0]
        url = urlsplit(value)
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

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "citation_title":
            self.title = attrs.get("content", "")
        if tag == "a":
            self.links.append(attrs.get("href", ""))


def fetch(url, destination):
    subprocess.run([
        "/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
        "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "15",
        "--max-time", "180", "--retry", "2", "--retry-delay", "2",
        "--output", str(destination), url,
    ], check=True)


def describe(identifier):
    with tempfile.TemporaryDirectory(prefix="sioyek-arxiv-") as temp:
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


def save_paper(url, directory):
    identifier = paper_id(url)
    title, identifier = describe(identifier)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename(title, identifier)
    # Serialize repeated clicks, and never expose a partially downloaded PDF.
    with (directory / ".sioyek-arxiv.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            if is_pdf(destination):
                return destination
            raise ValueError(f"Existing file is not a PDF; move it aside first: {destination}")
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".arxiv-", suffix=".part",
                                         delete=False) as stream:
            partial = Path(stream.name)
        try:
            fetch("https://arxiv.org/pdf/" + identifier, partial)
            if not is_pdf(partial):
                raise ValueError("arXiv returned something other than a PDF; nothing was saved.")
            # link() refuses to overwrite an existing file, even in a race.
            os.link(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    return destination


def notify_error(message):
    # Pass the message as argv rather than interpolating it into AppleScript.
    subprocess.run([
        "/usr/bin/osascript", "-e",
        'on run argv\ndisplay alert "Sioyek arXiv" message (item 1 of argv)\nend run',
        message,
    ], check=False)


def applescript_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launcher_command():
    # The Homebrew wrapper supplies its stable opt path. Never bake a Cellar
    # version or Python patch version into the per-user application.
    installed = os.environ.get("SIOYEK_ARXIV_LAUNCHER")
    if installed:
        return [installed]
    return [sys.executable, str(Path(__file__).absolute())]


def bookmarklet_page(directory):
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>arXiv → Sioyek</title>'
        '<style>body{font:18px system-ui;max-width:740px;margin:60px auto;line-height:1.6;'
        'padding:0 24px;color:#20242b;background:#f6f7fa}'
        'a.bookmark{display:inline-block;padding:12px 18px;background:#2456b8;'
        'color:white;border-radius:8px}textarea{box-sizing:border-box;width:100%;'
        'height:140px;padding:12px}code{overflow-wrap:anywhere}</style>'
        '<h1>arXiv → Sioyek</h1>'
        '<p>Drag this button onto your Firefox bookmarks toolbar:</p>'
        '<p><a class="bookmark" href="' + html.escape(BOOKMARKLET, quote=True)
        + '">Open in Sioyek</a></p>'
        '<p>On an arXiv abstract or HTML paper page, click the bookmark. '
        'Allow Firefox to open <b>Sioyek arXiv</b> when prompted. '
        'The PDF is saved in <code>' + html.escape(str(directory)) + '</code> '
        'with its title and version, then opened in Sioyek.</p>'
        '<p>If the toolbar is hidden, press <b>⌘⇧B</b>. '
        'Alternatively, create a bookmark and paste this into its URL field:</p>'
        '<textarea readonly aria-label="Bookmarklet URL">'
        + html.escape(BOOKMARKLET) + '</textarea>'
        '<p>If the bookmark does not run in Firefox’s PDF viewer, use the paper’s '
        '<code>/abs/</code> page.</p>'
        '<p>Change the download folder with '
        '<code>sioyek-arxiv setup --papers-dir /path/to/papers</code>.</p></html>'
    )


def open_bookmarklet(browser=None):
    page = support_dir() / "bookmarklet.html"
    if not page.exists():
        raise ValueError("Run sioyek-arxiv setup first.")
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
    with tempfile.TemporaryDirectory(prefix=".sioyek-arxiv-", dir=app.parent) as temporary:
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
            CFBundleIdentifier="local.sioyek.arxiv",
            CFBundleName="Sioyek arXiv",
            CFBundleShortVersionString=VERSION,
            LSUIElement=True,
            CFBundleURLTypes=[dict(CFBundleURLName="Sioyek arXiv",
                                  CFBundleURLSchemes=["sioyek-arxiv"],
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
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != "local.sioyek.arxiv":
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
    parser = argparse.ArgumentParser(prog="sioyek-arxiv", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sioyek-arxiv {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("setup", help="Install or update your macOS URL handler")
    install.add_argument("--papers-dir", help="PDF folder (default: existing setting or ~/Downloads/Papers)")
    install.add_argument("--sioyek-app", help="Path to Sioyek.app (auto-detected by default)")
    install.add_argument("--no-open", action="store_true", help="Do not open the bookmarklet page")
    install.add_argument("--browser", help="Browser to use for the setup page, e.g. Firefox")
    download = commands.add_parser("open", help="Save an arXiv PDF and open it in Sioyek")
    download.add_argument("url", help="arXiv URL or sioyek-arxiv://open?url=...")
    download.add_argument("--no-open", action="store_true", help="Download without opening Sioyek")
    download.add_argument("--notify", action="store_true", help="Show errors in a macOS alert")
    bookmark = commands.add_parser("bookmarklet", help="Open the bookmarklet setup page")
    bookmark.add_argument("--browser", help="Browser to use, e.g. Firefox")
    bookmark.add_argument("--print", action="store_true", dest="print_url", help="Print the bookmarklet URL")
    commands.add_parser("status", help="Show your current setup")
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
            print(f"Version: {VERSION}\nURL handler installed: {handler_app().is_dir()}")
            print(f"Papers: {config.get('papers_dir', '~/Downloads/Papers')}")
            print(f"Sioyek: {config.get('sioyek_app', 'auto-detect')}\nConfig: {config_path()}")
        elif args.command == "open":
            paper_id(args.url)
            config = load_config()
            directory = Path(config.get("papers_dir", "~/Downloads/Papers")).expanduser()
            app = find_sioyek(config) if not args.no_open else None
            destination = save_paper(args.url, directory)
            print(destination, flush=True)
            if app:
                subprocess.run(["/usr/bin/open", "-a", str(app), str(destination)], check=True)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        message = str(error)
        print(message, file=sys.stderr, flush=True)
        if getattr(args, "notify", False):
            notify_error(message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
