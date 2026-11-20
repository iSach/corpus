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
3. **Library papers** that were never reviewed, only after you tick **review library papers too** in the Inbox's Claude bar.

Dismissed papers are never reviewed. Editing a topic that a paper isn't filed under doesn't trigger a new review.

## Pacing and usage limits

The queue is built so it doesn't overload a subscription or an API key:

- One review at a time, at most one every `CORPUS_AI_MIN_INTERVAL` seconds (default 180).
- At most `CORPUS_AI_DAILY_LIMIT` reviews per UTC day (default 30; 0 removes the limit).
- When Claude reports a usage or rate limit, the whole queue pauses until the reset time in Claude's message (plus two minutes). If the message gives no time, it waits 15 minutes, then doubles up to 4 hours.
- When Claude can't be reached or the sign-in fails, the queue backs off the same way. The paper keeps its place and the attempt isn't counted against it.
- A paper whose review itself fails (malformed output, a refusal) is retried 30 minutes, then 90 minutes later. After a third failure it waits for you: **REVIEW NOW** on its card queues it again.

The Claude bar in the Inbox shows the state: the next review, a pause and its reason, what's waiting, reviews today and this month. **RESUME NOW** lifts a pause early. The pacing state is kept in `ai-queue.json` in the data folder, so a restart doesn't reset the daily count or a pause.

A subscription is shared with your own Claude use. Keep the daily limit well below what you need for yourself.

## Set up with your Claude subscription

This uses the Claude CLI in headless mode (`claude -p`) with no tools, settings, memory or saved sessions. Paper text can't make it run anything.

1. On a machine where you're signed in to Claude, run `claude setup-token` and copy the long-lived token it prints.
2. Make the CLI available to the service user, for example:

   ```sh
   sudo install -m 0755 "$(readlink -f ~/.local/bin/claude)" /usr/local/bin/claude
   ```

   Corpus disables the CLI's auto-updater, so update it by repeating this step.
3. Add the token to the service's environment file (`/etc/corpus/corpus.env`), without echoing it to your shell history:

   ```sh
   sudoedit /etc/corpus/corpus.env
   # CLAUDE_CODE_OAUTH_TOKEN=<the token>
   # CORPUS_CLAUDE_BIN=/usr/local/bin/claude
   ```
4. Restart Corpus: `sudo systemctl restart corpus`.

The CLI runs with its own empty home folder (`claude-home` in the data folder), so it never sees your personal Claude settings. A subscription only reviews the owner's library: other accounts' libraries need an API key.

## Set up with an Anthropic API key

Set `ANTHROPIC_API_KEY` in the environment file and restart. With both a key and a subscription token, the key is used; set `CORPUS_AI_BACKEND=cli` to prefer the subscription, or `off` to stop reviews. Requests use structured outputs and prompt caching, and, for current models, a server-side fallback model when a paper trips a safety classifier. The Inbox bar shows an estimate of this month's spend at list prices.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | | Subscription token from `claude setup-token`. |
| `CORPUS_CLAUDE_BIN` | `claude` | Path to the Claude CLI. |
| `ANTHROPIC_API_KEY` | | Anthropic API key. |
| `CORPUS_AI_BACKEND` | `auto` | `auto`, `api`, `cli` or `off`. |
| `CORPUS_AI_MODEL` | `claude-opus-5-5` | Claude model ID. |
| `CORPUS_AI_MIN_INTERVAL` | `180` | Seconds between reviews. |
| `CORPUS_AI_DAILY_LIMIT` | `30` | Reviews per UTC day; 0 for no limit. |
| `CORPUS_AI_REVIEWS` | `1` | `0` pauses reviews without deleting earlier ones. |

Credentials never appear in API responses, the browser configuration or logs. Error messages from Claude are reduced to a short reason, because they can echo paper text.

## What Claude is sent

The paper's title, first authors, venue, year and abstract; the paper's text when its PDF has been downloaded (up to about 80,000 characters from the first page on, roughly the main body of a typical paper); the name, description and relevance criteria of the paper's topics; and the name and description of your other topics, for suggestions. Nothing else from your library is sent.
