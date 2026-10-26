# Daybreak

A morning brief, meeting prep and proactive work assistant for **any Chromium browser** (Chrome, Edge, Brave, Arc,
Dia, Vivaldi, Opera, Chromium), running on **your own Claude subscription**. No API key is needed, and there are no
servers apart from the one on your computer.

- **The Morning Brief**: every weekday your new tab opens on a public-domain painting with *The Monday Brief* written
  across it, followed by these sections:
  - *Push your work forward*
  - *Top to-dos*: each with a link to its source and an *Ask Daybreak* button
  - *New updates*: one per workstream, with a status tag, the people involved and a source link
  - *Your day*: the schedule plus *Prep me*
  - *Looking ahead*: "what do you want to work on tomorrow?"; your answer feeds into the next brief

  The brief opens as its own tab the first time you use the browser that morning. ⌥⇧B opens it any time.
- **Meeting prep and pop-ups**: notes are built from your email, tickets and docs before each meeting. A card with
  **Ignore / Prep notes / Join** appears 5 minutes before the meeting and again when it starts.
- **Proactive pop-ups**: when someone is waiting on you or something is blocked (at most 3 an hour, work hours only).
- **Side-panel assistant** (⌥⇧D or the toolbar icon):
  - asks about the current page, your work and your day
  - drafts email replies (it never sends anything)
  - builds reports, decks and recaps as HTML documents
  - runs scheduled tasks ("every weekday at 5:30pm, recap what I shipped")

## How it works

```
Gmail · Google Calendar · Drive · Jira/Confluence   ← your claude.ai connectors (read-only)
Slack                                               ← the Slack web session already signed in to your browser
Bitbucket (optional)                                ← API token
        │
        ▼
 sensors ─► typed events (request · commitment · decision · blocker · context)
        ─► router ─► workstreams ─► synthesis (overview, timeline, open loops) ─► curator (today's surface)
        ─► Morning Brief · meeting prep · pop-ups · chat context
```

Two parts:

- **Helper** (`daybreak/`, Python ≥ 3.11): runs Claude through the
  [Claude Agent SDK](https://docs.claude.com/en/docs/agent-sdk/overview) using your Claude Code login.
  - Background runs load your claude.ai connectors with your hooks and plugins switched off.
  - A permission gate lets only read-only connector tools run. Chat can also create Gmail drafts.
  - Everything is stored in SQLite on your computer.
- **Extension** (`extension/`, Manifest V3, no build step): provides the new-tab brief, side panel, pop-ups and the
  Slack bridge.
  - It pairs with the helper through a native-messaging host and talks to it on `127.0.0.1:47821` with a token.
  - In browsers without a side panel (e.g. Arc, Opera), chat opens in a small window instead.

### Slack without installing an app

Many workspaces don't allow installing apps, so by default Daybreak reads Slack through the session you're already
signed in with:

1. It reads the Slack web client's `localConfig_v2` (your workspace token) from `app.slack.com`. If no Slack tab has
   it yet, `app.slack.com/client/` is opened once in a background tab so Slack writes it.
2. It calls read-only Web API methods with that token and your session cookie, the same calls the Slack web client
   makes: `activity.feed`, `client.counts`, `search.modules.messages` and `conversations.history`/`replies`, plus
   Slack's edge cache for names.

The token never leaves the browser. These are Slack's internal web-client APIs, so they can change, and they act with
your full Slack permissions, so follow your workspace's policies. If your workspace allows it, you can switch to the
claude.ai Slack connector in the extension's options instead.

## Setup

**Requirements:** Claude Code signed in with a Claude subscription (`claude` → `/login`). For the brief to be useful,
connect Gmail, Google Calendar, Google Drive and Atlassian in claude.ai → Settings → Connectors.

1. **Install the helper**
   ```bash
   git clone <this repo> daybreak && cd daybreak
   python3 -m venv .venv && .venv/bin/pip install -e .
   .venv/bin/daybreak install
   ```
   `install` does four things:
   - copies Daybreak into its own environment in the data folder
   - copies the extension there too
   - registers the native-messaging host in every Chromium browser it finds
   - starts the helper at login: a launchd agent on macOS, a systemd user service on Linux

   The login service runs from the data folder rather than your checkout. That matters on macOS, where background
   processes can't read ~/Documents or ~/Desktop.

   On Windows, the native host is registered, but you start `daybreak run` yourself or add a Task Scheduler task. To run
   the helper in the foreground instead of installing it, use `.venv/bin/daybreak run`.
2. **Load the extension**: open `chrome://extensions` (or `edge://`, `brave://`), turn on *Developer mode*, choose
   *Load unpacked*, then pick the `extension` folder that `install` printed (inside the data folder). Keep the new-tab
   change when the browser asks.
   After pulling new code, run `daybreak install` again and click reload on the extension.
3. **Slack**: stay signed in to your Slack workspace in that browser profile.

If pairing doesn't happen automatically, run `daybreak pair` and paste its output into the extension's options page.

Settings are in `config.toml` in the data folder:
- macOS: `~/Library/Application Support/Daybreak`
- Linux: `~/.local/share/daybreak`
- Windows: `%LOCALAPPDATA%\Daybreak`

They cover brief time, work hours, pop-ups, sources and models. A few can also be changed from the extension's options
page.

### Running in the background without keychain access

`daybreak run` normally uses your Claude Code login from the system keychain. If you run it somewhere without that
(SSH, containers, some service managers), create a long-lived token with `claude setup-token` and put
`CLAUDE_CODE_OAUTH_TOKEN=…` in a `.env` file in the data folder before `daybreak install`. Treat that token like a
password.

## Commands

| Command | What it does |
|---|---|
| `daybreak run` | run the helper in the foreground |
| `daybreak sense [--hours N]` | read sources and update workstreams now |
| `daybreak brief` | write today's brief now |
| `daybreak calendar` | refresh meetings |
| `daybreak prep [meeting-id\|next]` | build prep notes |
| `daybreak status` | show the helper's current state |
| `daybreak pair` | print the pairing blob for the options page |
| `daybreak install` / `uninstall` | register or remove the native host and the login service |

## Development

- `daybreak install --editable` points the login service and native host straight at your checkout. Load the
  extension from `extension/` in the repo instead of the data folder.
- `DAYBREAK_DEV=1 daybreak run` serves the extension pages at `http://127.0.0.1:47821/dev/newtab.html` with a stub
  `chrome` API, so you can work on the UI without reloading the extension.
- `python tests/e2e_extension.py` loads the real extension into a throwaway Playwright Chromium profile. It checks
  pairing, the brief, a meeting pop-up on a web page and the side panel. The helper must be running.
- The extension's fixed ID comes from the public key in `extension/manifest.json`. If you publish your own build with
  a different ID, add it to `extra_extension_ids` in `config.toml` and run `daybreak install` again.

## Privacy

- Your data stays on your computer, in the data folder.
- Model calls go through your own Claude account.
- Background runs are read-only, and nothing is ever sent on your behalf.
- The brief's paintings come from the Art Institute of Chicago's public-domain (CC0) collection.

Inspired by the Morning Brief in Dia's Better Days plan. Not affiliated with The Browser Company, Anthropic or Slack.

## License

MIT
