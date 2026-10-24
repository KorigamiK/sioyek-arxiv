# Open in Sioyek

One bookmarklet opens arXiv papers, direct web PDF URLs, and local `file://` PDFs
in [Sioyek](https://sioyek.info). Web PDFs are saved in your papers folder;
local PDFs open in place.

```text
arXiv or web PDF → Open in Sioyek → ~/Downloads/Papers → Sioyek
local file:// PDF → Open in Sioyek → existing file in Sioyek
```

For example:

```text
Attention Is All You Need [1706.03762v7].pdf
```

The macOS helper runs only when called. It needs no browser extension, server,
API key, or Python packages beyond the standard library.

## Install with Homebrew

Install [Sioyek](https://github.com/ahrm/sioyek/releases) first. The normal
Homebrew cask is also supported:

```sh
brew install --cask sioyek
```

Then install the helper and set up your account:

```sh
brew install korigamik/tap/sioyek-open
sioyek-open setup --browser Firefox
```

The setup page opens in Firefox. Drag **Open in Sioyek** onto the bookmarks
toolbar. On an arXiv paper page or a PDF URL, click the bookmark and allow
Firefox to launch **Open in Sioyek**. The browser may offer to remember that choice.
If Firefox blocks the bookmarklet in its PDF viewer, copy the PDF's address,
switch to an ordinary web page, click the bookmark, and paste the URL into the
prompt. This also accepts local `file://` URLs and PDF download URLs without
a `.pdf` extension.

`setup` detects Sioyek in `/Applications` or `~/Applications`, keeps an existing
configuration, and defaults to saving papers in `~/Downloads/Papers`. It creates
a small per-user URL handler app; it does not change Sioyek itself.

## Customize

```sh
sioyek-open setup --papers-dir ~/Documents/Papers
sioyek-open setup --sioyek-app ~/Applications/sioyek.app
sioyek-open setup --no-open
sioyek-open status
sioyek-open bookmarklet --browser Firefox
sioyek-open bookmarklet --print
```

You can also edit `~/.config/sioyek-open/config.json`:

```json
{
  "papers_dir": "~/Downloads/Papers",
  "sioyek_app": "/Applications/sioyek.app"
}
```

Run `setup` again after changing settings if you want the setup page to show the
new folder. The download command reads settings on each invocation.

## Use from a terminal

```sh
sioyek-open open https://arxiv.org/abs/1706.03762
sioyek-open open --no-open https://arxiv.org/abs/1706.03762
sioyek-open open 'https://example.org/reports/paper.pdf'
sioyek-open open 'file:///Users/you/Documents/My%20Paper.pdf'
```

Abstract, HTML, and PDF URLs are accepted, including old-style arXiv IDs. An
unversioned URL resolves the latest version listed on the abstract page. An
explicit version URL keeps that version. Existing copies are reused, incomplete
downloads stay temporary, and non-PDF responses are rejected.

Other HTTP(S) URLs must return a PDF. Their filenames use the URL's basename
and a short identifier, such as `paper [a4b9e6c281de].pdf`, so different URLs
with the same filename do not overwrite each other. Query parameters are
included in the identifier; page fragments such as `#page=3` are not. Repeated
opens reuse the saved file; remove it to download a fresh copy from a changing
URL. Redirects and extensionless PDF download endpoints are supported.

MIREX links at `www.music-ir.org` automatically use `music-ir.org`, whose HTTPS
certificate is valid for that hostname. The path and query are preserved, and
both forms reuse the same saved PDF in your configured papers folder. Certificate
verification stays enabled; other sites with invalid certificates show an error
with curl's explanation instead of the full download command.

Local `file://` PDFs are validated and opened at their existing location,
without copying them into the papers folder. URL-encoded spaces and Unicode
filenames are supported. Only local file URLs (empty host or `localhost`) are
accepted.

The downloader does not share Firefox's login session. For a PDF requiring
browser cookies or sign-in, download it in Firefox first, then use its local
`file://` URL.

## Local library and metadata

Every successful `open` catalogs the PDF in `library.sqlite3` inside your
configured papers folder, including when using `--no-open`. The library is local
and uses Python's built-in SQLite support. Existing bookmarklets still work;
run `setup` and replace your browser bookmark with the new button to enable
page metadata capture.

The new bookmarklet captures citation, Dublin Core, Open Graph, and JSON-LD
metadata exposed by the current page. On an article page declaring a PDF via
`citation_pdf_url` or a PDF `<link>`, it downloads that PDF while retaining the
article's metadata. If you paste a different document URL into the prompt,
unrelated metadata from the current page is excluded. No extra metadata service
is contacted. arXiv citation metadata is retained from the abstract-page request
the helper already makes.

When present, normalized fields include title, ordered authors, DOI, arXiv ID
and version, publication date and year, journal or conference, publisher, volume,
issue, first and last page, abstract, keywords, language, ISSN, ISBN, and PDF URL.
The raw page data and its source URL are retained for future bibliography and
export support. Capture is bounded to keep the bookmarklet URL manageable;
large structured blocks or excess tags may be omitted. A direct PDF viewer often
exposes only a filename or page title. Embedded PDF properties are not extracted,
and missing bibliographic fields remain empty.

Each record also keeps the file's SHA-256, size, local paths, indexing timestamps,
and successful opens through the helper. Identical PDF bytes share one record;
different bytes remain separate even if they have the same DOI. Reopening adds
metadata without erasing richer citation data with a filename or generic page
title. Original metadata observations are kept with their source and first/last
seen times. Local PDFs remain at their original paths. Missing files retain
their catalog entries and are marked as missing in JSON output.

```sh
sioyek-open library scan                 # Index PDFs already in your papers folder
sioyek-open library list                 # IDs, years, and titles
sioyek-open library show <id-prefix>     # Metadata, paths, and original sources as JSON
sioyek-open library export > library.json
```

`scan` includes subfolders, leaves the PDFs untouched, and does not fetch metadata
online. It imports filename-derived titles and recognizes arXiv identifiers in
the helper's existing filenames. Reopening a paper using the new bookmarklet can
enrich that record. JSON export includes the complete catalog with a schema
version; formatted citations, BibTeX, and RIS exports are future additions.

Back up the SQLite catalog along with your PDFs. Changing `papers_dir` selects
the catalog in the new folder. Uninstalling the helper keeps both the PDFs and
the catalog.

## Updates

```sh
brew update
brew upgrade sioyek-open
```

The registered app launches the package through Homebrew's stable `opt` path,
so formula upgrades do not embed obsolete Cellar or Python patch-version paths.
Rerun `sioyek-open setup` to refresh the app and setup page when needed.

## Files and removal

- App: `~/Applications/Open in Sioyek.app`
- Settings: `~/.config/sioyek-open/config.json`
- Library: `<papers_dir>/library.sqlite3`
- Setup page: `~/Library/Application Support/Open in Sioyek/bookmarklet.html`
- Errors and download log: `~/Library/Application Support/Open in Sioyek/download.log`

To remove the per-user integration and package:

```sh
sioyek-open uninstall
brew uninstall sioyek-open
```

Remove the bookmark from your browser manually. Saved PDFs and settings are kept.
`brew uninstall` on its own removes the package, so run the first command before
removing it to unregister the per-user app too.

## Development

Requires Python 3.9 or newer; the Homebrew formula supplies Python automatically.
The URL handler and app setup require macOS. To run without Homebrew:

```sh
python3 sioyek_open.py setup --browser Firefox
python3 -m unittest discover -s tests -v
bun test tests/bookmarklet.test.js
```

Keep the checkout and Python interpreter installed when using source setup. Use
the Homebrew installation for upgrade-stable paths.

Licensed under GPL-3.0-only.
