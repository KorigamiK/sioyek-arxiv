# arXiv → Sioyek

One bookmarklet click saves an arXiv paper with its title and version, then opens
the local PDF in [Sioyek](https://sioyek.info).

```text
arXiv abstract page → Open in Sioyek → ~/Downloads/Papers → Sioyek
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
brew install korigamik/tap/sioyek-arxiv
sioyek-arxiv setup --browser Firefox
```

The setup page opens in Firefox. Drag **Open in Sioyek** onto the bookmarks
toolbar. On an arXiv abstract or HTML paper page, click the bookmark and allow
Firefox to launch **Sioyek arXiv**. The browser may offer to remember that choice.
Use the paper's `/abs/` page if the bookmarklet does not run in the PDF viewer.

`setup` detects Sioyek in `/Applications` or `~/Applications`, keeps an existing
configuration, and defaults to saving papers in `~/Downloads/Papers`. It creates
a small per-user URL handler app; it does not change Sioyek itself.

## Customize

```sh
sioyek-arxiv setup --papers-dir ~/Documents/Papers
sioyek-arxiv setup --sioyek-app ~/Applications/sioyek.app
sioyek-arxiv setup --no-open
sioyek-arxiv status
sioyek-arxiv bookmarklet --browser Firefox
sioyek-arxiv bookmarklet --print
```

You can also edit `~/.config/sioyek/arxiv.json`:

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
sioyek-arxiv open https://arxiv.org/abs/1706.03762
sioyek-arxiv open --no-open https://arxiv.org/abs/1706.03762
```

Abstract, HTML, and PDF URLs are accepted, including old-style arXiv IDs. An
unversioned URL resolves the latest version listed on the abstract page. An
explicit version URL keeps that version. Existing copies are reused, incomplete
downloads stay temporary, and non-PDF responses are rejected. Only arXiv paper
URLs are supported; arbitrary publisher pages and login-gated PDFs are outside
this helper's scope.

## Updates and existing installations

```sh
brew update
brew upgrade sioyek-arxiv
```

The registered app launches the package through Homebrew's stable `opt` path,
so formula upgrades do not embed obsolete Cellar or Python patch-version paths.
Rerun `sioyek-arxiv setup` to refresh the app and setup page when needed.

If you used the original helper in
[KorigamiK/sioyek](https://github.com/KorigamiK/sioyek), install this formula and
run `setup`. It uses the same `sioyek-arxiv://` scheme, settings file, and per-user
app location. Your existing bookmark and downloaded papers continue to work.

## Files and removal

- App: `~/Applications/Sioyek arXiv.app`
- Settings: `~/.config/sioyek/arxiv.json`
- Setup page: `~/Library/Application Support/Sioyek arXiv/bookmarklet.html`
- Errors and download log: `~/Library/Application Support/Sioyek arXiv/download.log`

To remove the per-user integration and package:

```sh
sioyek-arxiv uninstall
brew uninstall sioyek-arxiv
```

Remove the bookmark from your browser manually. Saved PDFs and settings are kept.
`brew uninstall` on its own removes the package, so run the first command before
removing it to unregister the per-user app too.

## Development

Requires Python 3.9 or newer; the Homebrew formula supplies Python automatically.
The URL handler and app setup require macOS. To run without Homebrew:

```sh
python3 sioyek_arxiv.py setup --browser Firefox
python3 -m unittest discover -s tests -v
```

Keep the checkout and Python interpreter installed when using source setup. Use
the Homebrew installation for upgrade-stable paths.

Licensed under GPL-3.0-only. Initially developed as the arXiv helper scripts in
the Sioyek fork linked above.
