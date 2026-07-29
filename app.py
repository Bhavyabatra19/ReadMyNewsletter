#!/usr/bin/env python3
"""
ReadMyNewsletter — web app (account mode)
=========================================
Set up your newsletter inbox once, then just log in: a background worker
refreshes your digest daily, so every visit shows your latest reading room
without reconnecting.

  * Accounts:     email + password (hashed).
  * Connection:   your inbox (IMAP) + optional Anthropic API key, stored
                  ENCRYPTED at rest so the daily worker can run while you're away.
  * Dashboard:    your recent digests, newest first, plus "refresh now".

Nothing is shared between users; secrets are encrypted with a key derived from
APP_SECRET. Prefer zero storage? Use the stateless CLI (newsletter_digest.py).

Run locally:
    pip install -r requirements.txt
    python app.py            # http://127.0.0.1:5000  (also starts the scheduler)

Deploy: any persistent host (Docker / Railway / Render / Fly). See README.
"""

import os
import secrets
from functools import wraps

from flask import (
    Flask, Response, abort, flash, redirect, render_template_string,
    request, session, url_for,
)

import newsletter_digest as nd
import store
import worker

app = Flask(__name__)
app.secret_key = store.flask_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)

IMAP_PRESETS = [
    ("Gmail", "imap.gmail.com", 993),
    ("Outlook / Office 365", "outlook.office365.com", 993),
    ("Yahoo", "imap.mail.yahoo.com", 993),
    ("iCloud", "imap.mail.me.com", 993),
    ("Proton Mail (Bridge)", "127.0.0.1", 1143),
]
DEFAULT_MODEL = nd.MODEL
PRESET_HOSTS = [h for _, h, _ in IMAP_PRESETS]


# --------------------------------------------------------------------------- #
# Auth plumbing
# --------------------------------------------------------------------------- #

def current_user():
    uid = session.get("uid")
    return store.user_by_id(uid) if uid else None


def login_required(view):
    @wraps(view)
    def wrapped(*a, **k):
        if not session.get("uid"):
            return redirect(url_for("index"))
        return view(*a, **k)
    return wrapped


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or sent != session.get("_csrf"):
            abort(400, "Bad or missing CSRF token. Reload and try again.")


# --------------------------------------------------------------------------- #
# Public: landing / signup / login
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    if session.get("uid"):
        return redirect(url_for("dashboard"))
    return render_template_string(LANDING_PAGE, error=None, mode="login")


@app.post("/signup")
def signup():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not email or "@" not in email:
        return render_template_string(LANDING_PAGE, error="Enter a valid email.", mode="signup")
    if len(password) < 8:
        return render_template_string(LANDING_PAGE, error="Password must be at least 8 characters.", mode="signup")
    if store.user_by_email(email):
        return render_template_string(LANDING_PAGE, error="That email is already registered — log in instead.", mode="login")
    uid = store.create_user(email, password)
    session.clear()
    session["uid"] = uid
    csrf_token()
    return redirect(url_for("setup"))


@app.post("/login")
def login():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    user = store.verify_login(email, password)
    if not user:
        return render_template_string(LANDING_PAGE, error="Wrong email or password.", mode="login")
    session.clear()
    session["uid"] = user["id"]
    csrf_token()
    return redirect(url_for("dashboard"))


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Authed: dashboard / setup / refresh / digest view
# --------------------------------------------------------------------------- #

@app.get("/dashboard")
@login_required
def dashboard():
    uid = session["uid"]
    conn = store.get_connection(uid)
    digests = store.list_digests(uid)
    return render_template_string(
        DASHBOARD_PAGE,
        user=current_user(),
        conn=conn,
        digests=digests,
    )


@app.get("/setup")
@login_required
def setup():
    conn = store.get_connection(session["uid"]) or {}
    return render_template_string(
        SETUP_PAGE, presets=IMAP_PRESETS, preset_hosts=PRESET_HOSTS,
        default_model=DEFAULT_MODEL, conn=conn, error=None,
    )


@app.post("/setup")
@login_required
def setup_save():
    uid = session["uid"]
    f = request.form
    existing = store.get_connection(uid)

    host = (f.get("imap_host") or "").strip()
    user = (f.get("imap_user") or "").strip()
    password = f.get("imap_password") or ""
    api_key = f.get("api_key") or ""

    def fail(msg):
        merged = dict(existing or {})
        merged.update({
            "imap_host": host, "imap_port": f.get("imap_port", "993"),
            "imap_user": user, "imap_folder": f.get("imap_folder", "INBOX"),
            "model": f.get("model", DEFAULT_MODEL),
            "days": f.get("days", "7"),
            "use_ai": f.get("use_ai") == "on",
            "include_read": f.get("include_read") == "on",
            "auto_refresh": f.get("auto_refresh") == "on",
        })
        return render_template_string(
            SETUP_PAGE, presets=IMAP_PRESETS, preset_hosts=PRESET_HOSTS,
            default_model=DEFAULT_MODEL, conn=merged, error=msg,
        )

    if not host or not user:
        return fail("Inbox host and email address are required.")
    # On first save a password is required; on edit, blank means "keep existing".
    if not password and not (existing and existing_has_password(uid)):
        return fail("App password is required.")
    try:
        port = int(f.get("imap_port", "993"))
        days = max(1, int(f.get("days", "7")))
    except ValueError:
        return fail("Port and days must be numbers.")

    # Preserve stored secrets if the fields were left blank on edit.
    if not password:
        password = store.get_connection(uid, reveal=True)["imap_password"]
    if not api_key and existing and existing.get("has_api_key"):
        api_key = store.get_connection(uid, reveal=True)["api_key"]

    store.save_connection(uid, {
        "imap_host": host, "imap_port": port, "imap_user": user,
        "imap_password": password, "imap_folder": f.get("imap_folder", "INBOX").strip() or "INBOX",
        "api_key": api_key.strip(), "model": (f.get("model") or "").strip() or DEFAULT_MODEL,
        "days": days, "use_ai": f.get("use_ai") == "on",
        "include_read": f.get("include_read") == "on",
        "auto_refresh": f.get("auto_refresh") == "on",
    })

    # Build the first digest right away so the dashboard isn't empty.
    ok, msg = worker.refresh_user(uid)
    flash(msg if ok else f"Saved, but the first digest didn't build: {msg}")
    return redirect(url_for("dashboard"))


def existing_has_password(uid):
    conn = store.get_connection(uid)
    return bool(conn)  # a stored connection always has an encrypted password


@app.post("/refresh")
@login_required
def refresh_now():
    ok, msg = worker.refresh_user(session["uid"])
    flash(msg)
    return redirect(url_for("dashboard"))


@app.get("/digest/<int:digest_id>")
@login_required
def view_digest(digest_id):
    row = store.get_digest(session["uid"], digest_id)
    if not row:
        abort(404)
    html = _with_reader_toolbar(row["html"])
    return Response(html, mimetype="text/html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _with_reader_toolbar(html):
    """Inject a small fixed toolbar into a stored digest for easy phone use."""
    toolbar = """<div id="rmn-bar">
  <a href="/dashboard">← Dashboard</a>
  <button type="button" onclick="window.print()">Save as PDF</button>
  <button type="button" onclick="rmnDownload()">Download</button>
</div>
<style>
  #rmn-bar{position:fixed;top:0;left:0;right:0;z-index:999;display:flex;gap:10px;
    justify-content:center;align-items:center;padding:9px 12px;background:#1c1a17;
    font-family:"IBM Plex Mono",monospace;font-size:13px}
  #rmn-bar a,#rmn-bar button{color:#f7f4ee;background:transparent;border:1px solid #4a463f;
    border-radius:8px;padding:5px 12px;text-decoration:none;cursor:pointer;
    font-family:inherit;font-size:13px}
  #rmn-bar button:hover,#rmn-bar a:hover{background:#4f6df5;border-color:#4f6df5}
  body{padding-top:46px}
  @media print{#rmn-bar{display:none}body{padding-top:0}}
</style>
<script>
  function rmnDownload(){
    var el=document.getElementById('rmn-bar');if(el)el.remove();
    var html='<!DOCTYPE html>\\n'+document.documentElement.outerHTML;
    var blob=new Blob([html],{type:'text/html'});
    var a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download='newsletter-digest.html';a.click();
  }
</script>
"""
    marker = "<body>"
    idx = html.find(marker)
    if idx == -1:
        return html
    cut = idx + len(marker)
    return html[:cut] + "\n" + toolbar + html[cut:]


# --------------------------------------------------------------------------- #
# Shared styling + pages
# --------------------------------------------------------------------------- #

BASE_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{--ink:#1c1a17;--ink-soft:#5c574f;--line:#e6e1d7;--paper:#f7f4ee;--card:#fffdf8;--accent:#4f6df5}
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:"Newsreader",Georgia,serif;font-size:18px;line-height:1.55}
  .wrap{max-width:660px;margin:0 auto;padding:0 20px 80px}
  .top{display:flex;justify-content:space-between;align-items:center;padding:26px 0}
  .brand{font-family:"IBM Plex Mono",monospace;font-size:14px;letter-spacing:.02em;color:var(--ink);text-decoration:none;font-weight:500}
  .eyebrow{font-family:"IBM Plex Mono",monospace;font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-soft);margin:0 0 10px}
  h1{font-size:clamp(32px,7vw,46px);font-weight:600;line-height:1.03;margin:0;letter-spacing:-.01em}
  .lede{color:var(--ink-soft);margin:16px 0 0;font-size:19px}
  fieldset{border:1px solid var(--line);border-radius:14px;padding:20px 20px 4px;margin:0 0 22px}
  legend{font-family:"IBM Plex Mono",monospace;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink);padding:0 8px}
  .field{margin-bottom:20px}
  label{display:block;font-family:"IBM Plex Mono",monospace;font-size:12.5px;letter-spacing:.02em;text-transform:uppercase;color:var(--ink-soft);margin-bottom:7px}
  .hint{font-size:14.5px;color:var(--ink-soft);margin:5px 0 0}
  input,select{width:100%;font-family:"IBM Plex Mono",monospace;font-size:15px;padding:11px 13px;border:1px solid var(--line);border-radius:11px;background:var(--card);color:var(--ink)}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  .row{display:flex;gap:14px}.row>.field{flex:1}
  .check{display:flex;align-items:flex-start;gap:10px;margin-bottom:14px}
  .check input{margin-top:5px;width:auto}
  .check label{text-transform:none;letter-spacing:0;font-family:"Newsreader",serif;font-size:16.5px;color:var(--ink);margin:0}
  .btn{display:inline-block;text-align:center;cursor:pointer;border:none;border-radius:12px;background:var(--ink);color:var(--paper);font-family:"IBM Plex Mono",monospace;font-size:15px;letter-spacing:.03em;padding:14px 18px;text-decoration:none}
  .btn:hover{background:var(--accent)}
  .btn--full{width:100%}
  .btn--ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
  .btn--ghost:hover{background:var(--card);color:var(--accent);border-color:var(--accent)}
  .flash{margin:18px 0;padding:12px 15px;border:1px solid var(--accent);border-left-width:4px;border-radius:10px;background:#eef1fe;color:#2a3ba8;font-size:15.5px}
  .error{margin:18px 0;padding:12px 15px;border:1px solid #c2472f;border-left-width:4px;border-radius:10px;background:#fbeae6;color:#8f2f1c;font-size:15.5px}
  .privacy{margin-top:22px;padding:16px 18px;border:1px dashed var(--line);border-radius:12px;background:var(--card);font-size:15px;color:var(--ink-soft)}
  .privacy b{color:var(--ink);font-weight:600}
  .steps{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:26px 0 4px}
  .step{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 15px}
  .step .n{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--accent);font-weight:500}
  .step p{margin:6px 0 0;font-size:15.5px;line-height:1.35}
  @media (max-width:560px){.steps{grid-template-columns:1fr}}
  .tabs{display:flex;gap:8px;margin:24px 0 4px}
  .tab{flex:1;text-align:center;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--card);font-family:"IBM Plex Mono",monospace;font-size:13px;color:var(--ink-soft);cursor:pointer}
  .tab--on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:14px}
  .card a.title{font-size:21px;font-weight:600;color:var(--ink);text-decoration:none}
  .card a.title:hover{color:var(--accent)}
  .meta{font-family:"IBM Plex Mono",monospace;font-size:12.5px;color:var(--ink-soft);margin-top:5px}
  .muted{color:var(--ink-soft)}
  footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink-soft);text-align:center}
  a{color:var(--accent)}
</style>
"""

FLASH_BLOCK = """
{% with msgs = get_flashed_messages() %}
  {% if msgs %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
{% endwith %}
"""

LANDING_PAGE = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>ReadMyNewsletter</title>""" + BASE_STYLE + """</head><body><div class="wrap">
  <header style="padding:44px 0 22px;border-bottom:2px solid var(--ink)">
    <p class="eyebrow">Open source · set it up once · your keys are encrypted</p>
    <h1>ReadMyNewsletter</h1>
    <p class="lede">Connect a dedicated newsletter inbox once. We build one clean,
    categorised digest every day — just log in to read the latest. No reconnecting,
    no inbox clutter.</p>
    <div class="steps">
      <div class="step"><span class="n">01 / Sign up</span><p>Create an account and connect your inbox + Claude key.</p></div>
      <div class="step"><span class="n">02 / We refresh daily</span><p>A background job rebuilds your digest every day.</p></div>
      <div class="step"><span class="n">03 / Just log in</span><p>Your latest reading room is always waiting.</p></div>
    </div>
  </header>

  {% if error %}<div class="error">{{ error }}</div>{% endif %}

  <div class="tabs">
    <div class="tab {{ 'tab--on' if mode=='login' else '' }}" onclick="show('login')">Log in</div>
    <div class="tab {{ 'tab--on' if mode=='signup' else '' }}" onclick="show('signup')">Create account</div>
  </div>

  <form id="login" method="post" action="/login" style="{{ '' if mode=='login' else 'display:none' }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <div class="field"><label>Email</label><input type="email" name="email" required></div>
    <div class="field"><label>Password</label><input type="password" name="password" required></div>
    <button class="btn btn--full" type="submit">Log in</button>
  </form>

  <form id="signup" method="post" action="/signup" style="{{ '' if mode=='signup' else 'display:none' }}">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <div class="field"><label>Email</label><input type="email" name="email" required></div>
    <div class="field"><label>Password (8+ characters)</label><input type="password" name="password" required></div>
    <button class="btn btn--full" type="submit">Create account &amp; connect inbox</button>
  </form>

  <div class="privacy"><b>How your credentials are handled.</b> To refresh your
  digest while you're away, we store your inbox app-password and API key
  <b>encrypted at rest</b>; your account password is hashed, never stored in the
  clear. Want zero storage? The project is
  <a href="https://github.com/bhavyabatra19/readmynewsletter" target="_blank" rel="noopener">open source</a> —
  self-host it, or use the stateless CLI.</div>
  <footer>ReadMyNewsletter · free &amp; open source</footer>
</div>
<script>function show(w){document.getElementById('login').style.display=(w==='login')?'':'none';
document.getElementById('signup').style.display=(w==='signup')?'':'none';
document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('tab--on',(w==='login')?i===0:i===1));}</script>
</body></html>"""

DASHBOARD_PAGE = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Your digests · ReadMyNewsletter</title>""" + BASE_STYLE + """</head><body><div class="wrap">
  <div class="top">
    <a class="brand" href="/dashboard">◆ ReadMyNewsletter</a>
    <span class="meta">{{ user.email }} ·
      <form method="post" action="/logout" style="display:inline">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <button class="linkbtn" style="background:none;border:none;color:var(--accent);cursor:pointer;font-family:'IBM Plex Mono',monospace;font-size:12.5px">log out</button>
      </form>
    </span>
  </div>
""" + FLASH_BLOCK + """
  {% if not conn %}
    <div class="card">
      <div class="title" style="font-size:21px;font-weight:600">Connect your inbox to get started</div>
      <p class="muted" style="margin:8px 0 16px">You haven't connected a newsletter inbox yet. Set it up once and your digest builds automatically every day.</p>
      <a class="btn" href="/setup">Connect inbox →</a>
    </div>
  {% else %}
    <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin:20px 0 8px">
      <div>
        <h1 style="font-size:30px">Your digests</h1>
        <p class="meta">Inbox: {{ conn.imap_user }} · refreshing {{ 'daily' if conn.auto_refresh else 'manually' }} · last {{ conn.days }} days{{ ' · AI on' if conn.use_ai and conn.has_api_key else ' · no AI' }}</p>
      </div>
    </div>
    <div style="display:flex;gap:10px;margin-bottom:20px">
      <form method="post" action="/refresh" style="flex:0 0 auto">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <button class="btn" type="submit">↻ Refresh now</button>
      </form>
      <a class="btn btn--ghost" href="/setup">Settings</a>
    </div>

    {% if digests %}
      {% for d in digests %}
        <div class="card">
          <a class="title" href="/digest/{{ d.id }}">Digest · {{ d.day_range }}</a>
          <div class="meta">{{ d.created_at[:16].replace('T',' ') }} UTC · {{ d.item_count }} newsletters · {{ d.total_time }} min read</div>
        </div>
      {% endfor %}
    {% else %}
      <div class="card"><p class="muted" style="margin:0">No digest yet. Hit <b>Refresh now</b> — or wait for the next daily run.</p></div>
    {% endif %}
  {% endif %}
  <footer>ReadMyNewsletter · free &amp; open source</footer>
</div></body></html>"""

SETUP_PAGE = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Connect your inbox · ReadMyNewsletter</title>""" + BASE_STYLE + """</head><body><div class="wrap">
  <div class="top"><a class="brand" href="/dashboard">◆ ReadMyNewsletter</a><a class="meta" href="/dashboard">← back</a></div>
  <p class="eyebrow">Connect once · refreshes daily</p>
  <h1 style="font-size:32px">{{ 'Edit your connection' if conn.imap_user else 'Connect your inbox' }}</h1>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}

  <form method="post" action="/setup" style="margin-top:24px">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <fieldset>
      <legend>Your newsletter inbox</legend>
      <div class="field">
        <label for="imap_host">Provider / IMAP host</label>
        <select id="imap_host" name="imap_host" onchange="applyPreset(this)">
          {% for name, host, port in presets %}
            <option value="{{ host }}" data-port="{{ port }}" {% if conn.imap_host == host %}selected{% endif %}>{{ name }} — {{ host }}</option>
          {% endfor %}
          <option value="__custom__" {% if conn.imap_host and conn.imap_host not in preset_hosts %}selected{% endif %}>Other (type below)</option>
        </select>
        <input type="text" name="imap_host_custom" placeholder="imap.example.com" style="margin-top:8px" value="{{ conn.imap_host if conn.imap_host and conn.imap_host not in preset_hosts else '' }}">
      </div>
      <div class="row">
        <div class="field"><label>Port</label><input type="number" id="imap_port" name="imap_port" value="{{ conn.imap_port or 993 }}"></div>
        <div class="field"><label>Folder</label><input type="text" name="imap_folder" value="{{ conn.imap_folder or 'INBOX' }}"></div>
      </div>
      <div class="field"><label>Email address</label>
        <input type="email" name="imap_user" autocomplete="off" placeholder="your.newsletters@gmail.com" value="{{ conn.imap_user or '' }}" required></div>
      <div class="field"><label>App password {% if conn.imap_user %}<span style="text-transform:none">(leave blank to keep current)</span>{% endif %}</label>
        <input type="password" name="imap_password" autocomplete="off" placeholder="16-character app password" {% if not conn.imap_user %}required{% endif %}>
        <p class="hint">Not your normal password — generate an <b>app password</b> (Gmail: Account → Security → App passwords).</p></div>
    </fieldset>

    <fieldset>
      <legend>AI summaries (optional)</legend>
      <div class="field"><label>Your Anthropic API key {% if conn.has_api_key %}<span style="text-transform:none">(leave blank to keep current)</span>{% endif %}</label>
        <input type="password" name="api_key" autocomplete="off" placeholder="sk-ant-...">
        <p class="hint">From <a href="https://console.anthropic.com" target="_blank" rel="noopener">console.anthropic.com</a>. A few cents per run. Blank = group by sender, no AI.</p></div>
      <div class="field"><label>Model</label><input type="text" name="model" value="{{ conn.model or default_model }}"></div>
    </fieldset>

    <fieldset>
      <legend>Refresh</legend>
      <div class="field"><label>How many days back</label><input type="number" name="days" min="1" value="{{ conn.days or 7 }}"></div>
      <div class="check"><input type="checkbox" id="auto_refresh" name="auto_refresh" {% if conn.auto_refresh is not defined or conn.auto_refresh %}checked{% endif %}>
        <label for="auto_refresh">Rebuild my digest automatically every day</label></div>
      <div class="check"><input type="checkbox" id="use_ai" name="use_ai" {% if conn.use_ai is not defined or conn.use_ai %}checked{% endif %}>
        <label for="use_ai">Use AI to summarise &amp; categorise</label></div>
      <div class="check"><input type="checkbox" id="include_read" name="include_read" {% if conn.include_read %}checked{% endif %}>
        <label for="include_read">Include already-read mail</label></div>
    </fieldset>

    <button class="btn btn--full" type="submit">Save &amp; build my first digest →</button>
  </form>

  <div class="privacy"><b>Stored encrypted.</b> Your app-password and API key are
  encrypted at rest so the daily worker can refresh your digest while you're away.
  They're never shown back to you or logged.</div>
  <footer>ReadMyNewsletter · free &amp; open source</footer>
</div>
<script>
  const customBox=document.querySelector('input[name=imap_host_custom]');
  function applyPreset(sel){const o=sel.options[sel.selectedIndex];const p=o.getAttribute('data-port');
    if(p)document.getElementById('imap_port').value=p;customBox.style.display=(sel.value==='__custom__')?'':'none';}
  document.querySelector('form').addEventListener('submit',function(){
    const sel=document.getElementById('imap_host');
    if(sel.value==='__custom__'&&customBox.value.trim()){const h=document.createElement('input');
      h.type='hidden';h.name='imap_host';h.value=customBox.value.trim();this.appendChild(h);
      sel.disabled=true;setTimeout(()=>sel.disabled=false,0);}});
  applyPreset(document.getElementById('imap_host'));
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

# Start the daily scheduler when running as a single process (python app.py) or
# when explicitly enabled (RUN_SCHEDULER=1) — e.g. one gunicorn worker.
if os.environ.get("RUN_SCHEDULER") == "1":
    worker.start_scheduler()


if __name__ == "__main__":
    worker.start_scheduler()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"ReadMyNewsletter running at http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False)
