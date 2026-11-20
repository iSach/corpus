# Claude reviews

Corpus can ask Claude to review papers. For each paper you get:

- **A digest**: a one-line summary, the paper's core contributions, its method, its reported results, its limitations, and what kind of paper it is (method, benchmark, survey…).
- **Relevance for each of its topics**: a 0–10 score, a priority, the paper's role for the topic, Claude's confidence, an explanation tied to the topic's description and relevance criteria, and open questions.
- **Other topics it fits**, among your topics that have a description or criteria.
- **Quality scores** (credibility, methodological quality, reimplementation), only once Claude has read the full text.

Claude reads the title and abstract, or the paper's text once its PDF has been downloaded. Reviews appear on Inbox cards and in the library's detail pane. They are drafts: Claude never approves or dismisses a paper, never sets your personal scores, and never replaces an assessment you or a discovery agent recorded. Its relevance score and quality scores become the paper's draft scores, which you can accept in Edit details.

## What gets reviewed, and when

One background queue handles, in this order:

1. **Inbox papers**, as they arrive.
2. **Updates**: a reviewed paper whose abstract, topics or topic briefs changed, or whose PDF text has arrived. A high-relevance Inbox paper, or one with open questions, gets its PDF prepared automatically, so Claude reviews it a second time from the full text.
3. **Library papers** that were never reviewed, only after you tick **Review library papers too** on the **Claude** page.

Dismissed papers are never reviewed. Editing a topic that a paper isn't filed under doesn't trigger a new review.

## Pacing and usage limits

The queue is built so it doesn't overload a subscription or an API key:

- One review at a time, at most one every `CORPUS_AI_MIN_INTERVAL` seconds (default 180).
- At most `CORPUS_AI_DAILY_LIMIT` reviews per UTC day (default 30; 0 removes the limit).
- When Claude reports a usage or rate limit, the whole queue pauses until the reset time in Claude's message (plus two minutes). If the message gives no time, it waits 15 minutes, then doubles up to 4 hours.
- When Claude can't be reached or the sign-in fails, the queue backs off the same way. The paper keeps its place and the attempt isn't counted against it.
- A paper whose review itself fails (malformed output, a refusal) is retried 30 minutes, then 90 minutes later. After a third failure it waits for you: **REVIEW NOW** on its card queues it again.

The **Claude** page shows the state: the next review, a pause and its reason, what's waiting and reviews today. **RESUME NOW** lifts a pause early. The Claude tab is marked ‖ while the queue is paused, or with a number when reviews failed three times. The page also lists tokens and the estimated cost per day for the last two weeks, and the latest reviews with their token counts. The pacing state is kept in `ai-queue.json` in the data folder, so a restart doesn't reset the daily count or a pause.

A subscription is shared with your own Claude use. Keep the daily limit well below what you need for yourself.

## Set up

Everything below is on the **Claude** page, under **Settings** and **Keys**; only the administrator sees them. Keys saved there override the server environment, and **REMOVE** falls back to it again. They are stored in `service-config.json` in the data folder (readable only by the Corpus service), not in a database, so nightly backups don't copy them. They are never shown again after saving.

### With your Claude subscription

This runs the Claude CLI in headless mode (`claude -p`) with no tools, settings, memory or saved sessions. Paper text can't make it run anything.

1. Make the CLI available to the service user, for example:

   ```sh
   sudo install -m 0755 "$(readlink -f ~/.local/bin/claude)" /usr/local/bin/claude
   ```

   and set `CORPUS_CLAUDE_BIN=/usr/local/bin/claude` in `/etc/corpus/corpus.env`, then restart Corpus. The path can only be set on the server, never from the browser. Corpus disables the CLI's auto-updater, so update it by repeating the copy.
2. On a computer where you're signed in to Claude, run `claude setup-token`.
3. Paste the token into **CLAUDE SUBSCRIPTION TOKEN** on the Claude page.

The CLI runs with its own empty home folder (`claude-home` in the data folder), so it never sees your personal Claude settings.

### With an Anthropic API key

Paste it into **ANTHROPIC API KEY**. With both a key and a subscription token, the key is used, unless **USE** is set to **Subscription only**. Requests use structured outputs and prompt caching, and, for current models, a server-side fallback model when a paper trips a safety classifier. The Claude page shows an estimate of the spend at list prices.

### Who gets reviews

Each person on the **People** list of the **ADMIN** page has a **CLAUDE: ON/OFF** switch. It is on for you and off for everyone else until you turn it on. Your subscription only ever reviews your own library; other people's libraries are reviewed only with the API key.

## Settings

The Claude page sets everything except `CORPUS_CLAUDE_BIN` and `CORPUS_AI_REVIEWS`. The environment variables give the defaults:

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | | Subscription token from `claude setup-token`. |
| `CORPUS_CLAUDE_BIN` | `claude` | Path to the Claude CLI. |
| `ANTHROPIC_API_KEY` | | Anthropic API key. |
| `CORPUS_AI_BACKEND` | `auto` | `auto`, `api`, `cli` or `off`. |
| `CORPUS_AI_MODEL` | `claude-opus-5-5` | Claude model ID. |
| `CORPUS_AI_EFFORT` | `high` | How long Claude thinks per review: `low`, `medium`, `high`, `xhigh` or `max`. Lower effort uses fewer tokens. |
| `CORPUS_AI_MIN_INTERVAL` | `180` | Seconds between reviews. |
| `CORPUS_AI_DAILY_LIMIT` | `30` | Reviews per UTC day; 0 for no limit. |
| `CORPUS_AI_REVIEWS` | `1` | `0` pauses reviews without deleting earlier ones. |

Credentials never appear in API responses, the browser configuration or logs. Error messages from Claude are reduced to a short reason, because they can echo paper text.

## What Claude is sent

The paper's title, first authors, venue, year and abstract; the paper's text when its PDF has been downloaded (up to about 80,000 characters from the first page on, roughly the main body of a typical paper); the name, description and relevance criteria of the paper's topics; and the name and description of your other topics, for suggestions. Nothing else from your library is sent.
