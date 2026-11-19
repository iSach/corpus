# Corpus Saver — Safari Web Extension

Saves the paper on the current page to your Corpus Inbox. **Untested in real Safari**: it was written on Linux, where Xcode and Safari are unavailable. Unit tests and the manifest validator pass; the packaging steps below have not been run.

## How it works

1. The popup injects `src/extract.js` into the active tab (`activeTab` + `scripting`) and reads `citation_*`, Dublin Core and Open Graph meta tags, JSON-LD and the URL for a title, authors, DOI, venue, year and abstract. There are no publisher-specific scrapers.
2. **Save to Corpus** messages the non-persistent background script, which POSTs one proposal to `POST /api/ingest` with `Authorization: Bearer <token>`.
3. The server normalizes the DOI / arXiv ID, runs its duplicate check, and queues the paper in the **Inbox** for approval. Nothing is approved automatically.

Server limitation worth knowing: the Bearer token is accepted only by `/api/ingest` and the review routes. `/api/metadata` and `/api/capture` need a browser session, so the server cannot look up metadata for a bare URL on the extension's behalf. Pages without `citation_*` tags fall back to the document title, with no authors.

## Settings

Open the extension's options (Safari Settings → Extensions → Corpus Saver, or the popup's **Options** link):

- **Corpus URL**, e.g. `https://corpus.example.com`. Saving requests access to that origin only.
- **Ingest token**: `CORPUS_INGEST_TOKEN` or `data/ingest-token` on the server. Stored in `storage.local` in this browser profile; it is not synced.
- **Topic ID** (optional): an existing topic/field ID. Empty uses the server default.

## Build on macOS

Requires macOS with Xcode (full app, not only command line tools) and Safari 16.4+.

```sh
xcrun safari-web-extension-converter extensions/safari \
  --app-name "Corpus Saver" --macos-only
```

This creates an Xcode project next to the source and opens it. Add `--project-location <dir>` to put it elsewhere, and `--no-open` to skip launching Xcode. For iPhone/iPad, omit `--macos-only` (the converter then produces both targets, or pass `--ios-only`); see the iOS notes below.

Then:

1. In Xcode choose the **Corpus Saver** (macOS) scheme and press **Run**. Select your personal team under Signing & Capabilities if asked (a free Apple ID works for local builds).
2. In Safari open **Develop → Allow Unsigned Extensions** (enable the Develop menu in Safari Settings → Advanced first). This setting resets each time Safari quits.
3. Safari Settings → **Extensions**: tick **Corpus Saver**.
4. Same pane: under **Edit Websites…** (the extension's per-site access), set your Corpus host and the publisher sites you use to **Allow**, or **Ask**. Without access to the Corpus host, `fetch` fails on CORS, and the popup reports "Could not reach Corpus".
5. Open the options page, enter the URL and token, then try a paper page (an arXiv abstract page, or a publisher page with `citation_title`).

`activeTab` lets the popup read a page only after you click the toolbar button on it.

After changing files in `extensions/safari`, re-run the converter or copy the changes into the Xcode project's `Resources` folder, then rebuild. The converter copies the source; it does not link it.

### Distribution (Apple Developer Program, $99/yr)

Personal use needs none of this. To run the extension without the unsigned toggle, or to share it:

- **Notarized direct download**: enroll in the Apple Developer Program, sign the app with a Developer ID Application certificate, archive in Xcode (Product → Archive → Distribute App → Direct Distribution), and let Xcode notarize it.
- **Mac App Store / TestFlight**: use an Apple Distribution certificate, create the app in App Store Connect, upload from the Organizer, and pass App Review. Reviewers may ask why an extension needs access to arbitrary sites; the justification is that the user's self-hosted Corpus URL is not known in advance, which is why only `optional_host_permissions` are declared.

## Safari differences

- `browser.*` is the native, promise-based namespace; `src/shim.js` aliases `chrome` and `browser` so either name works.
- Background: use `background.scripts` with `"persistent": false`. Do not rely on a persistent page or on global state surviving between messages. Keep state in `storage.local`.
- Permissions are granted per site by the user in Safari, and **can be revoked at any time**. `permissions.request` and `optional_host_permissions` exist but behave differently from Chrome: the prompt may not appear, or may cover only the current session. The code tolerates a denied request and tells the user where to enable access.
- `host_permissions` are not auto-granted at install; they show as "Ask" until allowed.
- Unsupported or partial in Safari: `sidePanel`, `offscreen`, `identity`, `tabGroups`, `downloads`, `history`, `bookmarks`, `declarativeNetRequestFeedback`, `webRequestBlocking`, `runtime.getContexts`, `action.openPopup`, ES-module background scripts, and `options_ui.open_in_tab: false`. `scripts/validate.js` flags these. The list reflects documentation at the time of writing; recheck Apple's compatibility notes for your Safari version.
- The toolbar popup is closed when it loses focus, so the network call lives in the background script.
- Mixed content: an `http://` Corpus URL is blocked from `https://` pages but is fine from the background script; plain `http` also needs App Transport Security consideration in the host app only if you add native code, which this extension does not.

### iOS / iPadOS limitations

- Build with the converter's iOS target; it needs an iOS device or simulator and a signed host app (free accounts have 7-day provisioning).
- There is no Develop menu on iOS: unsigned extensions cannot be side-loaded without Xcode. TestFlight or the App Store (paid program) is the practical route.
- Popups render as a sheet, and the options page may open inside Settings. Test that layout.
- Site access is granted from the aA menu in Safari ("Manage Extensions") per site, and access is often "this time only".
- Background scripts are suspended aggressively, so a save that outlasts the popup can be cut off. This extension keeps requests short and reports the failure.

## Validate and test

No build step; Node 20+ only.

```sh
cd extensions/safari
node scripts/validate.js   # manifest + unsupported-API scan; exit 1 on errors
node --test test/          # metadata extraction and payload tests
```

## Layout

```
manifest.json          MV3 manifest
src/shim.js            browser/chrome alias
src/extract.js         page metadata extraction (injected on demand)
src/payload.js         ingest request building and response summary
src/background.js      non-persistent save handler
popup/ options/        UI
scripts/validate.js    manifest and API checks
test/                  node:test unit tests
```
