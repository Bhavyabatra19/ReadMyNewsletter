# ReadMyNewsletter

**Free, open-source, self-hosted.** Turn a cluttered newsletter inbox into one
clean, categorised reading digest you can skim on your phone whenever you have a
spare ten minutes — without handing your inbox or your API key to anyone.

Everyone runs their **own** copy and brings their **own** credentials. This
project ships **no keys of any kind** — not the maintainer's, not anyone's.

## The idea

1. Make **one dedicated email** just for newsletters (your "dummy" inbox).
2. Re-subscribe your newsletters there (or forward them — see below).
3. Run ReadMyNewsletter. It logs into that inbox, pulls recent newsletters,
   summarises and categorises each one with Claude, and builds a single
   mobile-friendly HTML digest grouped by topic, with read-time estimates.
4. Read on the go. Two ways to run it: a **web tool** (a page where you type your
   inbox + key and get a digest back) or the **command line**.

## Privacy first (read this)

- **Your credentials are never stored.** In the web tool they're used for one
  request to build the digest, then dropped — nothing is written to disk, logged,
  or put in a database.
- **No secrets live in this repo.** You supply your own IMAP app password and
  your own Anthropic API key at run time.
- **Run it on your own machine** (the default) and those credentials never leave
  it. Only expose the app to a network if you understand the tradeoffs (see
  [Hosting it for others](#hosting-it-for-others)).
- **It never marks your mail as read** (uses IMAP `BODY.PEEK`).

---

## Option A — the web tool (easiest)

A tiny local web app: open a page, fill in the form, get your digest.

```bash
git clone https://github.com/bhavyabatra19/readmynewsletter.git
cd readmynewsletter
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000** in your browser. Pick your provider, enter
your newsletter address + app password, optionally paste your Anthropic API key,
and hit **Build my digest**. The digest opens right in the browser — save it, or
email it to yourself to read later.

That's the whole thing. No accounts, no signup, no server of ours involved.

### Deploy your own on Vercel (one click-ish)

Want a hosted URL you can open from anywhere instead of running it locally? The
app ships ready for Vercel's Python runtime.

```bash
npm i -g vercel      # if you don't have it
vercel               # from the repo root — follow the prompts
vercel --prod        # promote to your production URL
```

Or import the GitHub repo at [vercel.com/new](https://vercel.com/new) and deploy
with the defaults — no build settings to change.

How it's wired: `api/index.py` exposes the same Flask `app` as a serverless
function, and `vercel.json` routes every path to it (`maxDuration` is bumped to
60s because fetching + summarising mail can take a while).

**Still no secrets to configure** — visitors type their own inbox + Claude key
into the page each time; you don't add any environment variables. A couple of
serverless caveats: outbound IMAP must be reachable from the function, and very
large inboxes can bump the 60-second limit (lower "days" if so). If you want a
guaranteed-private setup, running locally is still the surest option.

## Option B — the command line

Prefer a script you can schedule? Same engine, no web UI.

```bash
pip install -r requirements.txt
cp config.example.env .env      # then edit .env with your inbox + key
set -a && source .env && set +a # load your .env

python newsletter_digest.py          # last 7 days, unread only
python newsletter_digest.py --days 3 # last 3 days
python newsletter_digest.py --all    # include already-read mail
python newsletter_digest.py --no-ai  # skip Claude, group by sender
```

The digest lands in `digests/digest_YYYY-MM-DD.html`. Open in any browser.
`.env` and `digests/` are git-ignored so you can't commit them by accident.

---

## Setup (about 10 minutes, one time)

### 1. Create the dummy inbox
Any provider with IMAP works. Gmail is easiest. Two ways to fill it:

- **Subscribe fresh:** use this new address when signing up for newsletters.
- **Move existing ones:** in your main inbox, add a filter that forwards anything
  with an "Unsubscribe" link (or specific senders) to the new address.
  In Gmail: Settings → Filters → forward to your dummy address.

### 2. Get an app password (important)
IMAP needs an **app password**, not your normal login password, if you have
2-factor auth on (you should).

- **Gmail:** enable 2-Step Verification, then myaccount.google.com → Security →
  App passwords → generate one.
- **Outlook/Office365:** account.microsoft.com → Security → Advanced → App passwords.
- **Yahoo:** Account Security → Generate app password.
- **Proton Mail:** requires Proton Bridge (uses 127.0.0.1).

### 3. Get a Claude API key (optional but recommended)
Sign in at console.anthropic.com and create a key. This powers the summaries and
categorisation — usually a few cents per run on Haiku. Without a key, untick
"Use AI" in the web tool (or use the CLI's `--no-ai`) to group by sender with no
summaries.

---

## Read it on the go

- **Simplest:** in the web tool, email the digest to yourself and open it on your
  phone. The HTML is fully self-contained.
- **CLI:** put the `digests/` folder in Dropbox / Google Drive / iCloud and open
  the file from your phone's Drive app, or host it anywhere static.

## Schedule it (set and forget)

The CLI is the piece to schedule.

**macOS / Linux (cron)** — every day at 7am:
```
0 7 * * * cd /path/to/readmynewsletter && set -a && . ./.env && set +a && /usr/bin/python3 newsletter_digest.py >> run.log 2>&1
```

**Windows** — Task Scheduler → Create Basic Task → Daily → run
`python.exe newsletter_digest.py` in this folder (set the env vars in the task).

**GitHub Actions** — fork the repo, add **your own** repo secrets for each
variable, and run `newsletter_digest.py` on a `schedule:` cron. Because the
secrets live in *your* fork's settings, nothing sensitive is ever committed.

## Hosting it for others

The web tool defaults to `127.0.0.1` so it's private to your machine. You *can*
run it for other people, but then their inbox passwords and API keys pass through
your server — so only do this if you'll:

- serve it over **HTTPS** (put it behind a reverse proxy / tunnel),
- and be transparent that credentials are used in-memory only and never stored.

Bind it wider with `HOST=0.0.0.0 PORT=8000 python app.py`. The safest, most
private setup remains: everyone runs their own copy locally.

## Notes & tuning

- **Newsletter detection** uses the `List-Unsubscribe` header, which virtually
  all real newsletters send — so regular personal email is ignored automatically.
- **Categories** live in `CATEGORIES` near the top of `newsletter_digest.py` —
  edit them to match what you actually subscribe to.
- **Model name:** `DIGEST_MODEL` (CLI) / the Model field (web) default to a Haiku
  model. If the API rejects it, check docs.claude.com for the current model
  string.
- **Cost control:** each newsletter is truncated to ~3,500 characters before
  summarising, and batched 5 per API call.

## Project layout

| File | What it is |
| --- | --- |
| `app.py` | The web app — Flask frontend (onboarding + on-device settings) around the engine. Runs locally with `python app.py`. |
| `api/index.py` | Vercel serverless entry point — serves the same `app`. |
| `vercel.json` | Vercel routing + function config. |
| `newsletter_digest.py` | The engine: IMAP fetch, Claude summaries, HTML digest. Usable as a CLI on its own. |
| `sample_digest.html` | An example of what the finished digest looks like. |
| `config.example.env` | Template for the CLI's `.env` (copy to `.env`). |
| `requirements.txt` | Python dependencies. |

## Contributing

Issues and PRs welcome — this is meant to help people read newsletters without
the inbox clutter. MIT licensed; see [LICENSE](LICENSE).
