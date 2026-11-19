# Corpus for Chrome

A Manifest V3 extension that saves the paper on the current page to your self-hosted Corpus library. It sends the page's DOI or URL to Corpus, and the **server** parses the publisher page or looks the DOI up. Saved papers land in **Inbox → Pending**, like papers from the daily watcher; nothing is admitted to your library until you approve it.

No build step: plain JavaScript, loaded as-is.

## Install (load unpacked)

1. Open `chrome://extensions` and switch on **Developer mode**.
2. Choose **Load unpacked** and select this `extensions/chrome/` folder.
3. Pin the extension, then open its **Options**.

## Set up the token

In **Options**, enter:

- **Corpus address**: your instance's origin, e.g. `https://corpus.example` (plain `http://` is accepted only for `localhost`).
- **Ingest token**: the server's ingest Bearer token. It is the value of `CORPUS_INGEST_TOKEN` if you set one, otherwise the contents of `ingest-token` in the server's data directory (see [docs/paper-watch.md](../../docs/paper-watch.md)).

Saving the options asks Chrome for permission to reach **only your Corpus origin**. The extension never requests `<all_urls>`; it reads the current tab only when you click the toolbar button (`activeTab` + `scripting`).

Settings are stored in `chrome.storage.local` in your browser profile; the token is not synced.

## Use

On a paper page (a publisher article, arXiv, a DOI link), click the toolbar button. The popup shows the detected title, authors and DOI/arXiv id, then **Save to Corpus**:

- *Saved. It is waiting in your Inbox.*: queued for approval.
- *Already in your library.*: Corpus recognised a duplicate.
- Otherwise the server's reason is shown (bad token, unreadable page, rate limit, unreachable server).

If the server cannot resolve the link itself, the extension retries once with the `citation_*` meta tags found on the page.

Topics are not selectable yet: the API that lists topics needs a signed-in browser session, not the ingest token, so papers go to the default topic and you file them from the Inbox.

## How it works

| File | Role |
| --- | --- |
| `lib/metadata.js` | Detects title/authors/DOI/arXiv id/year from `citation_*`, Dublin Core and Open Graph tags. Injected on click; shared with the Safari port. |
| `lib/api.js` | Corpus client: URL validation, `POST /api/ingest` with the Bearer token, result mapping. Network is injected, so it is unit-tested. |
| `background/service-worker.js` | Stateless: reads settings from storage per request and performs the fetch (host permission, so no server CORS change). |
| `popup/`, `options/` | UI. |

The server side is `POST /api/ingest`; an item may carry just `{"identifier": "<url, DOI or arXiv id>"}` and the server resolves it with the same metadata lookup as **Add paper**.

## Tests

```sh
cd extensions/chrome
node --test tests/        # Node 18+, no dependencies
```

## Package for the Chrome Web Store

```sh
extensions/chrome/scripts/package.sh
```

This writes `extensions/chrome/dist/corpus-chrome-<version>.zip` containing only the runtime files. Bump `version` in `manifest.json` for each upload. Because the permissions are optional host permissions, the store listing needs only a justification for `activeTab`, `scripting` and `storage`, plus a privacy note that data goes only to the user's own Corpus server.
