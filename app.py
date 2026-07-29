#!/usr/bin/env python3
"""
ReadMyNewsletter — web app
==========================
Turns a cluttered newsletter inbox into one clean, categorised reading digest.

Everyone uses their OWN credentials:

  * their dedicated newsletter inbox (IMAP user + app password), and
  * optionally their OWN Anthropic API key for AI summaries.

Those credentials are used for a single request to build the digest and then
discarded — nothing is written to disk, nothing is logged, and no keys of any
kind live in this repository. Non-secret preferences (provider, days, etc.) can
optionally be remembered in the visitor's own browser (localStorage); secrets
never are.

Run locally:
    pip install -r requirements.txt
    python app.py            # then open http://127.0.0.1:5000

Deploy to Vercel:
    see api/index.py + vercel.json (this same `app` is served as a function).

The email + summarisation engine lives in newsletter_digest.py; this file is
only the web layer around it.
"""

import os
from datetime import datetime, timezone

from flask import Flask, Response, render_template_string, request

import newsletter_digest as nd

app = Flask(__name__)

# Common IMAP hosts so people don't have to remember them.
IMAP_PRESETS = [
    ("Gmail", "imap.gmail.com", 993),
    ("Outlook / Office 365", "outlook.office365.com", 993),
    ("Yahoo", "imap.mail.yahoo.com", 993),
    ("iCloud", "imap.mail.me.com", 993),
    ("Proton Mail (Bridge)", "127.0.0.1", 1143),
]

# The engine's default model. Not a secret — just an editable default string.
DEFAULT_MODEL = nd.MODEL


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return render_template_string(
        FORM_PAGE,
        presets=IMAP_PRESETS,
        default_model=DEFAULT_MODEL,
        error=None,
        form={},
    )


@app.post("/generate")
def generate():
    f = request.form

    host = (f.get("imap_host") or "").strip()
    port_raw = (f.get("imap_port") or "993").strip()
    user = (f.get("imap_user") or "").strip()
    password = f.get("imap_password") or ""
    folder = (f.get("imap_folder") or "INBOX").strip() or "INBOX"
    api_key = (f.get("api_key") or "").strip()
    model = (f.get("model") or "").strip() or DEFAULT_MODEL
    days_raw = (f.get("days") or "7").strip()
    include_read = f.get("include_read") == "on"
    use_ai = f.get("use_ai") == "on"

    # Validate here so we never hit newsletter_digest's sys.exit paths, which
    # would otherwise take down the web worker.
    def fail(message):
        return render_template_string(
            FORM_PAGE,
            presets=IMAP_PRESETS,
            default_model=DEFAULT_MODEL,
            error=message,
            # Echo back everything EXCEPT the two secrets.
            form={
                "imap_host": host,
                "imap_port": port_raw,
                "imap_user": user,
                "imap_folder": folder,
                "model": model,
                "days": days_raw,
                "include_read": include_read,
                "use_ai": use_ai,
            },
        )

    if not host or not user or not password:
        return fail("Inbox host, email address and app password are all required.")
    try:
        port = int(port_raw)
    except ValueError:
        return fail("IMAP port must be a number (usually 993).")
    try:
        days = max(1, int(days_raw))
    except ValueError:
        return fail("Days must be a whole number.")

    # 1. Pull newsletters over IMAP.
    try:
        mail = nd.connect(host, port, user, password)
    except Exception as exc:  # noqa: BLE001 — surface any login/connection error
        return fail(f"Could not sign in to the inbox: {exc}. "
                    "Check the host and remember IMAP needs an app password, "
                    "not your normal login password.")
    try:
        items = nd.fetch_newsletters(mail, days, folder, only_unread=not include_read)
    except Exception as exc:  # noqa: BLE001
        _safe_logout(mail)
        return fail(f"Could not read the '{folder}' folder: {exc}")
    else:
        _safe_logout(mail)

    if not items:
        return fail(f"No newsletters found in the last {days} day(s). "
                    "Try more days, or tick 'include already-read mail'.")

    # 2. Summarise + categorise (or just group by sender).
    if use_ai and api_key:
        try:
            items = nd.enrich_with_ai(items, api_key, model)
        except Exception as exc:  # noqa: BLE001
            return fail(f"AI summarisation failed: {exc}. "
                        "Check the API key and model name, or untick 'Use AI'.")
    else:
        items = nd.group_only(items)

    items.sort(
        key=lambda it: it["date"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    # 3. Build the digest page, add a small reader toolbar, hand it to the browser.
    html = nd.build_html(items, f"last {days} days")
    html = _with_reader_toolbar(html)
    return Response(html, mimetype="text/html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _safe_logout(mail):
    try:
        mail.logout()
    except Exception:  # noqa: BLE001
        pass


def _with_reader_toolbar(html):
    """Inject a tiny fixed toolbar (New digest / Save PDF / Download) into the
    generated digest so it's easy to act on from a phone. Hidden when printing."""
    toolbar = """<div id="rmn-bar">
  <a href="/">← New digest</a>
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
    # Insert right after the opening <body> tag.
    marker = "<body>"
    idx = html.find(marker)
    if idx == -1:
        return html
    cut = idx + len(marker)
    return html[:cut] + "\n" + toolbar + html[cut:]


# --------------------------------------------------------------------------- #
# The onboarding / setup page
# --------------------------------------------------------------------------- #

FORM_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>ReadMyNewsletter — build your digest</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --ink:#1c1a17; --ink-soft:#5c574f; --line:#e6e1d7;
    --paper:#f7f4ee; --card:#fffdf8; --accent:#4f6df5;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--paper); color:var(--ink);
    font-family:"Newsreader",Georgia,serif; font-size:18px; line-height:1.55;
  }
  .wrap { max-width:660px; margin:0 auto; padding:0 20px 80px; }

  /* Hero / onboarding */
  .masthead { padding:44px 0 22px; border-bottom:2px solid var(--ink); }
  .eyebrow {
    font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:.18em;
    text-transform:uppercase; color:var(--ink-soft); margin:0 0 10px;
  }
  h1 { font-size:clamp(34px,8vw,52px); font-weight:600; line-height:1.02; margin:0; letter-spacing:-.01em; }
  .lede { color:var(--ink-soft); margin:16px 0 0; font-size:19px; }
  .steps {
    display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:26px 0 4px;
  }
  .step {
    background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 15px;
  }
  .step .n {
    font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--accent);
    font-weight:500; letter-spacing:.05em;
  }
  .step p { margin:6px 0 0; font-size:15.5px; line-height:1.35; }
  @media (max-width:560px){ .steps{grid-template-columns:1fr} }

  /* Form */
  form { margin-top:30px; }
  .field { margin-bottom:20px; }
  label {
    display:block; font-family:"IBM Plex Mono",monospace; font-size:12.5px;
    letter-spacing:.02em; text-transform:uppercase; color:var(--ink-soft); margin-bottom:7px;
  }
  .hint { font-size:14.5px; color:var(--ink-soft); margin:5px 0 0; }
  input[type=text], input[type=email], input[type=password], input[type=number], select {
    width:100%; font-family:"IBM Plex Mono",monospace; font-size:15px;
    padding:11px 13px; border:1px solid var(--line); border-radius:11px;
    background:var(--card); color:var(--ink);
  }
  input:focus, select:focus { outline:none; border-color:var(--accent); }
  .row { display:flex; gap:14px; }
  .row > .field { flex:1; }
  fieldset {
    border:1px solid var(--line); border-radius:14px; padding:20px 20px 4px; margin:0 0 22px;
  }
  legend {
    font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:.08em;
    text-transform:uppercase; color:var(--ink); padding:0 8px;
  }
  .check { display:flex; align-items:flex-start; gap:10px; margin-bottom:14px; }
  .check input { margin-top:5px; width:auto; }
  .check label { text-transform:none; letter-spacing:0; font-family:"Newsreader",serif;
    font-size:16.5px; color:var(--ink); margin:0; }
  button.primary {
    width:100%; cursor:pointer; border:none; border-radius:12px;
    background:var(--ink); color:var(--paper);
    font-family:"IBM Plex Mono",monospace; font-size:15px; letter-spacing:.03em; padding:15px; margin-top:6px;
  }
  button.primary:hover { background:var(--accent); }
  .manage {
    display:flex; align-items:center; justify-content:space-between; gap:12px;
    margin:0 0 22px; padding:12px 15px; border:1px solid var(--line);
    border-radius:12px; background:var(--card);
  }
  .manage .check { margin:0; }
  .linkbtn {
    background:none; border:none; color:var(--accent); cursor:pointer;
    font-family:"IBM Plex Mono",monospace; font-size:13px; padding:0;
  }
  .privacy {
    margin-top:22px; padding:16px 18px; border:1px dashed var(--line);
    border-radius:12px; background:var(--card); font-size:15px; color:var(--ink-soft);
  }
  .privacy b { color:var(--ink); font-weight:600; }
  .error {
    margin-top:24px; padding:14px 16px; border:1px solid #c2472f; border-left-width:4px;
    border-radius:10px; background:#fbeae6; color:#8f2f1c; font-size:15.5px;
  }
  footer {
    margin-top:40px; padding-top:18px; border-top:1px solid var(--line);
    font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--ink-soft); text-align:center;
  }
  a { color:var(--accent); }
</style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <p class="eyebrow">Open source · your keys are never stored</p>
      <h1>ReadMyNewsletter</h1>
      <p class="lede">One dedicated inbox in, one clean categorised digest out —
      skim a week of newsletters in ten minutes, without the inbox clutter.</p>
      <div class="steps">
        <div class="step"><span class="n">01 / Connect</span>
          <p>Point it at a dedicated newsletter inbox with an app password.</p></div>
        <div class="step"><span class="n">02 / Summarise</span>
          <p>Your Claude key turns each newsletter into a short, sorted summary.</p></div>
        <div class="step"><span class="n">03 / Read</span>
          <p>Get a mobile-friendly digest to save, print, or read on the go.</p></div>
      </div>
    </header>

    {% if error %}<div class="error">{{ error }}</div>{% endif %}

    <form method="post" action="/generate" target="_blank">
      <fieldset>
        <legend>Your newsletter inbox</legend>

        <div class="field">
          <label for="imap_host">Provider / IMAP host</label>
          <select id="imap_host" name="imap_host" onchange="applyPreset(this)">
            {% for name, host, port in presets %}
              <option value="{{ host }}" data-port="{{ port }}"
                {% if form.get('imap_host') == host %}selected{% endif %}>{{ name }} — {{ host }}</option>
            {% endfor %}
            <option value="__custom__">Other (type it below)</option>
          </select>
          <input type="text" name="imap_host_custom" placeholder="imap.example.com" style="margin-top:8px">
          <p class="hint">Pick your provider, or choose "Other" and type the host in the box.</p>
        </div>

        <div class="row">
          <div class="field">
            <label for="imap_port">Port</label>
            <input type="number" id="imap_port" name="imap_port" value="{{ form.get('imap_port', '993') }}">
          </div>
          <div class="field">
            <label for="imap_folder">Folder</label>
            <input type="text" id="imap_folder" name="imap_folder" value="{{ form.get('imap_folder', 'INBOX') }}">
          </div>
        </div>

        <div class="field">
          <label for="imap_user">Email address</label>
          <input type="email" id="imap_user" name="imap_user" autocomplete="off"
                 placeholder="your.newsletters@gmail.com" value="{{ form.get('imap_user', '') }}" required>
        </div>

        <div class="field">
          <label for="imap_password">App password</label>
          <input type="password" id="imap_password" name="imap_password"
                 autocomplete="off" placeholder="16-character app password" required>
          <p class="hint">Not your normal password. With 2-factor auth on, generate an
          <b>app password</b> (Gmail: Account → Security → App passwords).</p>
        </div>
      </fieldset>

      <fieldset>
        <legend>AI summaries (optional)</legend>
        <div class="field">
          <label for="api_key">Your Anthropic API key</label>
          <input type="password" id="api_key" name="api_key" autocomplete="off" placeholder="sk-ant-...">
          <p class="hint">Get one at <a href="https://console.anthropic.com" target="_blank" rel="noopener">console.anthropic.com</a>.
          Summaries cost a few cents per run. Leave blank to just group by sender.</p>
        </div>
        <div class="field">
          <label for="model">Model</label>
          <input type="text" id="model" name="model" value="{{ form.get('model', default_model) }}">
        </div>
      </fieldset>

      <fieldset>
        <legend>Options</legend>
        <div class="field">
          <label for="days">How many days back</label>
          <input type="number" id="days" name="days" min="1" value="{{ form.get('days', '7') }}">
        </div>
        <div class="check">
          <input type="checkbox" id="use_ai" name="use_ai" {% if form.get('use_ai', True) %}checked{% endif %}>
          <label for="use_ai">Use AI to summarise &amp; categorise (needs a key above)</label>
        </div>
        <div class="check">
          <input type="checkbox" id="include_read" name="include_read" {% if form.get('include_read') %}checked{% endif %}>
          <label for="include_read">Include already-read mail (default: unread only)</label>
        </div>
      </fieldset>

      <div class="manage">
        <div class="check">
          <input type="checkbox" id="remember">
          <label for="remember">Remember my settings on this device<br>
            <span style="font-size:13px;color:var(--ink-soft)">Saves everything except your password &amp; API key, in this browser only.</span>
          </label>
        </div>
        <button type="button" class="linkbtn" onclick="clearProfile()">Clear saved data</button>
      </div>

      <button type="submit" class="primary">Build my digest →</button>
    </form>

    <div class="privacy">
      <b>Where do my credentials go?</b> Straight to the digest builder and nowhere
      else — used once to fetch and summarise your mail, then dropped. Nothing is
      saved to a disk, database, or log, and no keys live in this repository. The
      "remember my settings" option stores only non-secret preferences in your own
      browser. For maximum privacy you can run your own copy locally — it's
      <a href="https://github.com/bhavyabatra19/readmynewsletter" target="_blank" rel="noopener">open source</a>.
    </div>

    <footer>ReadMyNewsletter · free &amp; open source · read the code before you trust it</footer>
  </div>

<script>
  const customBox = document.querySelector('input[name=imap_host_custom]');

  function applyPreset(sel) {
    const opt = sel.options[sel.selectedIndex];
    const port = opt.getAttribute('data-port');
    if (port) document.getElementById('imap_port').value = port;
    customBox.style.display = (sel.value === '__custom__') ? '' : 'none';
  }

  // When "Other" is chosen, copy the free-text host into the real field on submit.
  const form = document.querySelector('form');
  form.addEventListener('submit', function () {
    const sel = document.getElementById('imap_host');
    if (sel.value === '__custom__' && customBox.value.trim()) {
      const hidden = document.createElement('input');
      hidden.type = 'hidden'; hidden.name = 'imap_host'; hidden.value = customBox.value.trim();
      this.appendChild(hidden);
      sel.disabled = true;
      setTimeout(() => { sel.disabled = false; }, 0);  // keep it usable after submit
    }
    saveProfile();
  });

  // ----- On-device settings management (localStorage; never secrets) -----
  const KEY = 'rmn_profile_v1';
  const TEXT_IDS = ['imap_host','imap_port','imap_folder','imap_user','model','days'];
  const CHECK_IDS = ['use_ai','include_read'];

  function saveProfile() {
    if (!document.getElementById('remember').checked) { localStorage.removeItem(KEY); return; }
    const p = {};
    TEXT_IDS.forEach(id => p[id] = document.getElementById(id).value);
    CHECK_IDS.forEach(id => p[id] = document.getElementById(id).checked);
    p.imap_host_custom = customBox.value;
    localStorage.setItem(KEY, JSON.stringify(p));
  }

  function loadProfile() {
    const raw = localStorage.getItem(KEY);
    if (!raw) { applyPreset(document.getElementById('imap_host')); return; }
    document.getElementById('remember').checked = true;
    try {
      const p = JSON.parse(raw);
      TEXT_IDS.forEach(id => { if (p[id] != null) document.getElementById(id).value = p[id]; });
      CHECK_IDS.forEach(id => { if (p[id] != null) document.getElementById(id).checked = p[id]; });
      if (p.imap_host_custom) customBox.value = p.imap_host_custom;
    } catch (e) { /* ignore corrupt state */ }
    applyPreset(document.getElementById('imap_host'));
  }

  function clearProfile() {
    localStorage.removeItem(KEY);
    document.getElementById('remember').checked = false;
    alert('Saved settings cleared from this browser.');
  }

  loadProfile();
</script>
</body>
</html>"""


if __name__ == "__main__":
    # Bind to localhost by default so the app is private to your machine.
    # Override with HOST=0.0.0.0 / PORT=... only if you know you want it exposed.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"ReadMyNewsletter running at http://{host}:{port}  (Ctrl+C to stop)")
    app.run(host=host, port=port, debug=False)
