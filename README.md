# ReadMyNewsletter

**Free & open source.** Turn a cluttered newsletter inbox into one clean,
categorised reading digest — set it up once, then just log in and read. A
background job rebuilds your digest every day, so you never have to reconnect or
run anything by hand.

## The idea

1. Make **one dedicated email** just for newsletters (your "dummy" inbox).
2. Re-subscribe your newsletters there (or forward them — see below).
3. Sign up, connect that inbox once, and add your Claude API key.
4. Every day a worker logs in, pulls recent newsletters, summarises and
   categorises each with Claude, and saves a mobile-friendly digest.
5. **Just log in to read the latest** — grouped by topic, with read-time
   estimates. No inbox clutter.

## Two ways to use it

| | **Account app** (this is the default) | **Stateless CLI** |
| --- | --- | --- |
| How | Log in → dashboard of daily digests | Run a script, get an HTML file |
| Storage | Accounts + encrypted credentials in SQLite | Nothing stored |
| Daily refresh | Automatic (background scheduler) | You schedule cron yourself |
| Best for | "Set and forget", read on any device | Maximum privacy, one machine |

---

## Run the account app

```bash
git clone https://github.com/bhavyabatra19/readmynewsletter.git
cd readmynewsletter
pip install -r requirements.txt
export APP_SECRET="a-long-random-string"   # encrypts stored credentials
python app.py                              # http://127.0.0.1:5000
```

Open the page, **create an account**, connect your inbox, and your first digest
builds immediately. After that the built-in scheduler refreshes it daily — every
time you log in, the newest digest is waiting.

- `APP_SECRET` is the key that encrypts stored inbox passwords + API keys. Set it
  to a long random value and keep it stable, or logins can't decrypt their
  secrets. If unset, a random one is generated and saved to `.app_secret`.
- The app binds to `127.0.0.1` locally. Set `HOST=0.0.0.0 PORT=8000` to expose it.

### How the daily refresh works

- **In-process scheduler:** `python app.py` (and the Docker image) run a
  background thread that rebuilds every auto-refresh account's digest once its
  latest one is older than ~20h. Tune with `REFRESH_MIN_AGE_HOURS` and
  `SCHEDULER_TICK_SECONDS`.
- **External cron (for multi-worker setups):** run the web app under gunicorn and
  drive refreshes from cron instead:
  ```
  0 7 * * *  cd /app && python worker.py        # daily at 7am
  ```
  `python worker.py --force` refreshes everyone immediately, ignoring the age
  check.

---

## Deploy it (persistent host)

The account app needs somewhere to persist data and something to trigger the
daily refresh. Two shapes work:

- **Long-running host** (**Railway, Render, Fly.io, any VPS, Docker**) — uses the
  built-in SQLite + in-process scheduler. Simplest; nothing external to manage.
- **Serverless** (**Vercel**) — uses hosted Postgres + Vercel Cron (see
  [Deploy on Vercel](#deploy-on-vercel) below).

**Docker**
```bash
docker build -t readmynewsletter .
docker run -p 8000:8000 -e APP_SECRET="..." -v rmn_data:/data readmynewsletter
```
The volume at `/data` persists the SQLite DB and generated secret.

**Railway / Render / Fly** — point them at the repo. The included `Procfile`
(`web: python app.py`) and `Dockerfile` work out of the box. Set `APP_SECRET` (and
`COOKIE_SECURE=1` once you're on HTTPS) as environment variables, and attach a
persistent volume mounted where `DATABASE_PATH` points.

### Deploy on Vercel

Vercel is serverless (read-only filesystem, no long-running process), so account
mode needs a hosted database and a cron trigger — both of which are built in:

1. **Import the repo** into Vercel. `vercel.json` + `api/index.py` are already wired.
2. **Add a Postgres database.** In the project's *Storage* tab add **Neon** (or any
   Postgres). This automatically sets `DATABASE_URL` / `POSTGRES_URL` — the app
   detects it and uses Postgres instead of SQLite. (No env var = it falls back to
   ephemeral `/tmp` SQLite, which won't persist between requests.)
3. **Set environment variables** (Project → Settings → Environment Variables):
   - `APP_SECRET` — a long random string (encrypts stored credentials). **Required
     and must stay stable**, or logins can't decrypt their secrets.
   - `CRON_SECRET` — a random string that authorises the daily refresh. Vercel
     sends it automatically to the cron endpoint.
   - `COOKIE_SECURE=1` — since Vercel serves HTTPS.
4. **Redeploy.** The included cron (`vercel.json` → `/tasks/refresh`, daily at
   07:00 UTC) rebuilds every account's digest. Adjust the schedule there.

That's it — signup, login, onboarding, and the dashboard all work, and data
persists in Postgres across cold starts. (Prefer not to manage a database? A
persistent host with the built-in SQLite + scheduler is simpler still.)

---

## Setup (about 10 minutes, one time)

### 1. Create the dummy inbox
Any provider with IMAP works. Gmail is easiest. Two ways to fill it:

- **Subscribe fresh:** use this new address when signing up for newsletters.
- **Move existing ones:** in your main inbox, add a filter that forwards anything
  with an "Unsubscribe" link (or specific senders) to the new address.

### 2. Get an app password (important)
IMAP needs an **app password**, not your normal login password, if you have
2-factor auth on (you should).

- **Gmail:** 2-Step Verification → myaccount.google.com → Security → App passwords.
- **Outlook/Office365:** account.microsoft.com → Security → Advanced → App passwords.
- **Yahoo:** Account Security → Generate app password.
- **Proton Mail:** requires Proton Bridge (127.0.0.1).

### 3. Get a Claude API key (optional but recommended)
From console.anthropic.com — usually a few cents per run on Haiku. Without one,
untick "Use AI" and digests group by sender with no summaries.

---

## Privacy & security

- **Account passwords** are hashed (scrypt), never stored in the clear.
- **Inbox app-password and API key** are **encrypted at rest** (AES via Fernet,
  keyed from `APP_SECRET`). They're required in encrypted form *only* because the
  daily worker must log in while you're away — that's the tradeoff for "don't
  connect daily". They're never shown back to you or written to logs.
- **CSRF protection** on every state-changing form; session cookies are
  HttpOnly + SameSite (+ Secure when `COOKIE_SECURE=1`).
- **It never marks your mail as read** (IMAP `BODY.PEEK`).
- **No secrets live in this repository.** `APP_SECRET`, the database, and
  `.app_secret` are all git-ignored.
- Want **zero server-side storage**? Use the stateless CLI below and schedule it
  yourself.

---

## Stateless CLI (no account, nothing stored)

```bash
pip install -r requirements.txt
cp config.example.env .env      # edit with your inbox + key
set -a && source .env && set +a

python newsletter_digest.py          # last 7 days, unread only
python newsletter_digest.py --days 3
python newsletter_digest.py --all    # include already-read mail
python newsletter_digest.py --no-ai  # skip Claude, group by sender
```

Digests land in `digests/digest_YYYY-MM-DD.html`. Schedule with cron:
```
0 7 * * * cd /path/to/readmynewsletter && set -a && . ./.env && set +a && python3 newsletter_digest.py >> run.log 2>&1
```

---

## Notes & tuning

- **Newsletter detection** uses the `List-Unsubscribe` header, so ordinary
  personal mail is ignored automatically.
- **Categories** live in `CATEGORIES` near the top of `newsletter_digest.py`.
- **Model** defaults to a Haiku model; change it per-account in Settings or via
  `DIGEST_MODEL` for the CLI. Check docs.claude.com for current model strings.
- **Cost control:** each newsletter is truncated to ~3,500 chars and batched 5
  per API call.

## Project layout

| File | What it is |
| --- | --- |
| `app.py` | The account web app: login, dashboard, setup, digest views. Starts the scheduler. |
| `store.py` | Persistence (SQLite locally, Postgres via `DATABASE_URL`) + credential encryption (Fernet) + password hashing. |
| `worker.py` | Daily-refresh logic, the background scheduler, and a `python worker.py` cron entry. |
| `newsletter_digest.py` | The engine: IMAP fetch, Claude summaries, HTML digest. Also a standalone CLI. |
| `Dockerfile` / `Procfile` | Deploy to a container / PaaS. |
| `api/index.py` / `vercel.json` | Optional Vercel entry (see the Vercel note). |
| `sample_digest.html` | Example of the finished digest. |
| `config.example.env` | Template for the CLI's `.env`. |

## Contributing

Issues and PRs welcome — this exists to help people read newsletters without the
inbox clutter. MIT licensed; see [LICENSE](LICENSE).
