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

Local `file://` PDFs are validated and opened at their existing location,
without copying them into the papers folder. URL-encoded spaces and Unicode
filenames are supported. Only local file URLs (empty host or `localhost`) are
accepted.

The downloader does not share Firefox's login session. For a PDF requiring
browser cookies or sign-in, download it in Firefox first, then use its local
`file://` URL.

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
```

Keep the checkout and Python interpreter installed when using source setup. Use
the Homebrew installation for upgrade-stable paths.

Licensed under GPL-3.0-only.
