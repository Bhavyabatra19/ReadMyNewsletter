#!/usr/bin/env python3
"""
Newsletter Digest
=================
Point this at a dedicated ("dummy") email inbox you use only for newsletters.
It will:
  1. Connect to that inbox over IMAP
  2. Pull recent newsletters (auto-detected via the List-Unsubscribe header)
  3. Summarise + categorise each one with the Claude API
  4. Build one clean, mobile-friendly HTML digest you can read anywhere

Run it on a schedule (cron / Task Scheduler / GitHub Actions) and you get a
fresh "reading room" digest every morning or every Sunday.

Usage:
    python newsletter_digest.py                 # last 7 days, unread only
    python newsletter_digest.py --days 3        # last 3 days
    python newsletter_digest.py --all           # include already-read mail
    python newsletter_digest.py --no-ai         # skip Claude, group by sender only

All configuration is read from environment variables (see config.example.env).
"""

import argparse
import email
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr
from html import escape

try:
    import requests
except ImportError:
    requests = None  # only needed when AI summarisation is on

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER = os.environ.get("IMAP_USER", "")
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
IMAP_FOLDER = os.environ.get("IMAP_FOLDER", "INBOX")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Haiku is cheap and plenty good for summaries; bump to a Sonnet model for
# richer summaries. Check docs.claude.com for the current model names.
MODEL = os.environ.get("DIGEST_MODEL", "claude-haiku-4-5-20251001")

OUTPUT_DIR = os.environ.get("DIGEST_OUTPUT_DIR", "digests")

# Fixed taxonomy keeps grouping stable run to run.
CATEGORIES = [
    "Tech & AI",
    "Business & Finance",
    "News & Politics",
    "Science & Health",
    "Design & Product",
    "Marketing & Growth",
    "Culture & Lifestyle",
    "Personal Growth",
    "Other",
]

CATEGORY_COLOR = {
    "Tech & AI": "#4f6df5",
    "Business & Finance": "#1f9d6b",
    "News & Politics": "#c2472f",
    "Science & Health": "#0f9bb0",
    "Design & Product": "#9b5de5",
    "Marketing & Growth": "#e08a1e",
    "Culture & Lifestyle": "#d6567f",
    "Personal Growth": "#5a8f3d",
    "Other": "#6b7280",
}

# --------------------------------------------------------------------------- #
# IMAP fetching
# --------------------------------------------------------------------------- #

def connect(host, port, user, password):
    if not user or not password:
        sys.exit("Missing IMAP_USER / IMAP_PASSWORD. See config.example.env.")
    mail = imaplib.IMAP4_SSL(host, port)
    mail.login(user, password)
    return mail


def _decode(value):
    """Decode a possibly RFC2047-encoded header into a plain string."""
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _get_body(msg):
    """Prefer text/plain; fall back to HTML with tags stripped."""
    plain, html_body = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, TypeError):
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html_body:
                html_body = text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_body = text
            else:
                plain = text

    body = plain or _strip_html(html_body)
    return _clean_text(body)


def _strip_html(raw):
    if not raw:
        return ""
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</p>", "\n\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"&nbsp;", " ", raw)
    raw = re.sub(r"&amp;", "&", raw)
    return raw


def _clean_text(text):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_newsletter(msg):
    """Almost every legitimate newsletter carries a List-Unsubscribe header."""
    if msg.get("List-Unsubscribe"):
        return True
    precedence = (msg.get("Precedence") or "").lower()
    if precedence in ("bulk", "list"):
        return True
    if msg.get("List-Id") or msg.get("List-Post"):
        return True
    return False


def estimate_read_time(text):
    words = len(text.split())
    return max(1, round(words / 200))  # ~200 wpm


def fetch_newsletters(mail, days, folder, only_unread):
    mail.select(folder)
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    criteria = ["SINCE", since]
    if only_unread:
        criteria = ["UNSEEN"] + criteria
    status, data = mail.search(None, *criteria)
    if status != "OK":
        return []

    ids = data[0].split()
    items = []
    for msg_id in ids:
        # BODY.PEEK avoids marking the message as read.
        status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
        if status != "OK":
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        if not is_newsletter(msg):
            continue

        sender_name, sender_email = parseaddr(_decode(msg.get("From")))
        subject = _decode(msg.get("Subject"))
        try:
            dt = parsedate_to_datetime(msg.get("Date"))
        except Exception:
            dt = None
        body = _get_body(msg)

        items.append({
            "sender": sender_name or sender_email,
            "sender_email": sender_email,
            "subject": subject or "(no subject)",
            "date": dt,
            "date_str": dt.strftime("%b %d") if dt else "",
            "body": body,
            "read_time": estimate_read_time(body),
        })
    return items


# --------------------------------------------------------------------------- #
# Claude summarisation + categorisation
# --------------------------------------------------------------------------- #

def summarise_batch(items, api_key, model):
    if requests is None:
        sys.exit("The 'requests' package is required for AI summaries. "
                 "pip install requests, or run with --no-ai.")

    payload_items = []
    for i, it in enumerate(items):
        payload_items.append({
            "id": i,
            "from": it["sender"],
            "subject": it["subject"],
            "body": it["body"][:3500],  # keep prompt lean
        })

    system = (
        "You are a newsletter digest assistant. For each newsletter, write a "
        "2-3 sentence plain-language summary of what it actually says, list up "
        "to 3 key takeaways as short phrases, and assign exactly one category "
        "from this list: " + ", ".join(CATEGORIES) + ". "
        "Reply with ONLY a JSON array, no prose, no code fences. Each element: "
        '{"id": int, "summary": str, "key_points": [str], "category": str}.'
    )
    user = json.dumps(payload_items, ensure_ascii=False)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        return json.loads(match.group(0)) if match else []


def enrich_with_ai(items, api_key, model, batch_size=5):
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        try:
            results = summarise_batch(batch, api_key, model)
        except Exception as exc:
            print(f"  ! AI summary failed for batch {start}: {exc}")
            results = []
        by_id = {r.get("id"): r for r in results}
        for i, it in enumerate(batch):
            r = by_id.get(i, {})
            it["summary"] = r.get("summary") or _fallback_summary(it["body"])
            it["key_points"] = r.get("key_points") or []
            cat = r.get("category")
            it["category"] = cat if cat in CATEGORIES else "Other"
        print(f"  summarised {min(start + batch_size, len(items))}/{len(items)}")
    return items


def _fallback_summary(body, sentences=2):
    parts = re.split(r"(?<=[.!?])\s+", body.strip())
    return " ".join(parts[:sentences])[:300]


def group_only(items):
    """No-AI mode: no summaries, category = sender."""
    for it in items:
        it["summary"] = _fallback_summary(it["body"])
        it["key_points"] = []
        it["category"] = it["sender"]
    return items


# --------------------------------------------------------------------------- #
# HTML digest
# --------------------------------------------------------------------------- #

def build_html(items, day_range_label):
    total = len(items)
    total_time = sum(it["read_time"] for it in items)

    # order items by our taxonomy, keep unknown categories after
    order = {c: i for i, c in enumerate(CATEGORIES)}
    grouped = {}
    for it in items:
        grouped.setdefault(it["category"], []).append(it)
    ordered_cats = sorted(grouped.keys(),
                          key=lambda c: order.get(c, len(CATEGORIES)))

    # filter chips
    chips = ['<button class="chip chip--active" data-cat="all">All '
             f'<span>{total}</span></button>']
    for cat in ordered_cats:
        color = CATEGORY_COLOR.get(cat, "#6b7280")
        chips.append(
            f'<button class="chip" data-cat="{escape(cat)}" '
            f'style="--chip:{color}">{escape(cat)} '
            f'<span>{len(grouped[cat])}</span></button>')

    sections = []
    for cat in ordered_cats:
        color = CATEGORY_COLOR.get(cat, "#6b7280")
        cards = []
        for it in grouped[cat]:
            points = ""
            if it["key_points"]:
                lis = "".join(f"<li>{escape(p)}</li>" for p in it["key_points"])
                points = f'<ul class="points">{lis}</ul>'
            cards.append(f"""
            <article class="card" style="--accent:{color}">
              <header class="card__head">
                <span class="card__from">{escape(it['sender'])}</span>
                <span class="card__meta">{escape(it['date_str'])} · {it['read_time']} min</span>
              </header>
              <h3 class="card__title">{escape(it['subject'])}</h3>
              <p class="card__summary">{escape(it['summary'])}</p>
              {points}
            </article>""")
        sections.append(f"""
        <section class="group" data-cat="{escape(cat)}">
          <h2 class="group__title"><span class="dot" style="background:{color}"></span>{escape(cat)}</h2>
          <div class="cards">{''.join(cards)}</div>
        </section>""")

    generated = datetime.now().strftime("%A, %B %d · %I:%M %p")

    return PAGE_TEMPLATE.format(
        day_range=escape(day_range_label),
        total=total,
        total_time=total_time,
        generated=escape(generated),
        chips="".join(chips),
        sections="".join(sections),
    )


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Newsletter Digest · {day_range}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #1c1a17;
    --ink-soft: #5c574f;
    --line: #e6e1d7;
    --paper: #f7f4ee;
    --card: #fffdf8;
    --accent: #4f6df5;
  }}
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: "Newsreader", Georgia, serif;
    font-size: 18px;
    line-height: 1.55;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; padding: 0 20px 80px; }}

  /* Masthead */
  .masthead {{
    padding: 40px 0 24px;
    border-bottom: 2px solid var(--ink);
    margin-bottom: 8px;
  }}
  .eyebrow {{
    font-family: "IBM Plex Mono", monospace;
    font-size: 12px; letter-spacing: .18em; text-transform: uppercase;
    color: var(--ink-soft); margin: 0 0 10px;
  }}
  .masthead h1 {{
    font-size: clamp(34px, 8vw, 52px); font-weight: 600;
    line-height: 1.02; margin: 0; letter-spacing: -0.01em;
  }}
  .stats {{
    display: flex; gap: 22px; flex-wrap: wrap;
    margin-top: 18px;
    font-family: "IBM Plex Mono", monospace; font-size: 13px;
    color: var(--ink-soft);
  }}
  .stats b {{ color: var(--ink); font-weight: 500; }}

  /* Filter chips */
  .filters {{
    position: sticky; top: 0; z-index: 5;
    display: flex; gap: 8px; overflow-x: auto;
    padding: 14px 0; margin: 0 -20px 8px; padding-left: 20px; padding-right: 20px;
    background: linear-gradient(var(--paper) 70%, transparent);
    -webkit-overflow-scrolling: touch;
  }}
  .filters::-webkit-scrollbar {{ display: none; }}
  .chip {{
    flex: 0 0 auto; cursor: pointer;
    font-family: "IBM Plex Mono", monospace; font-size: 12.5px;
    letter-spacing: .02em;
    border: 1px solid var(--line); background: var(--card);
    color: var(--ink-soft); border-radius: 999px;
    padding: 7px 14px; white-space: nowrap;
    transition: all .15s ease;
  }}
  .chip span {{ opacity: .55; margin-left: 3px; }}
  .chip:hover {{ border-color: var(--chip, var(--ink)); color: var(--ink); }}
  .chip--active {{ background: var(--ink); color: var(--paper); border-color: var(--ink); }}
  .chip--active span {{ opacity: .7; }}

  /* Category groups */
  .group {{ margin-top: 34px; }}
  .group__title {{
    display: flex; align-items: center; gap: 10px;
    font-size: 15px; font-weight: 600; letter-spacing: .02em;
    text-transform: uppercase; font-family: "IBM Plex Mono", monospace;
    color: var(--ink); margin: 0 0 14px;
  }}
  .dot {{ width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }}
  .cards {{ display: flex; flex-direction: column; gap: 14px; }}

  /* Newsletter card */
  .card {{
    position: relative; background: var(--card);
    border: 1px solid var(--line); border-radius: 14px;
    padding: 18px 20px 20px 22px; overflow: hidden;
  }}
  .card::before {{
    content: ""; position: absolute; left: 0; top: 0; bottom: 0;
    width: 4px; background: var(--accent);
  }}
  .card__head {{
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 12px; margin-bottom: 6px;
  }}
  .card__from {{
    font-family: "IBM Plex Mono", monospace; font-size: 12.5px;
    font-weight: 500; color: var(--accent); letter-spacing: .01em;
  }}
  .card__meta {{
    font-family: "IBM Plex Mono", monospace; font-size: 11.5px;
    color: var(--ink-soft); white-space: nowrap;
  }}
  .card__title {{
    font-size: 22px; font-weight: 600; line-height: 1.2;
    margin: 2px 0 8px; letter-spacing: -0.01em;
  }}
  .card__summary {{ margin: 0; color: var(--ink); }}
  .points {{
    margin: 12px 0 0; padding: 0; list-style: none;
    border-top: 1px solid var(--line); padding-top: 12px;
  }}
  .points li {{
    position: relative; padding-left: 18px; margin-bottom: 5px;
    font-size: 15.5px; color: var(--ink-soft); line-height: 1.4;
  }}
  .points li::before {{
    content: "→"; position: absolute; left: 0;
    color: var(--accent); font-family: "IBM Plex Mono", monospace;
  }}

  .empty {{ text-align: center; color: var(--ink-soft); padding: 60px 0; }}
  footer {{
    margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--line);
    font-family: "IBM Plex Mono", monospace; font-size: 12px;
    color: var(--ink-soft); text-align: center;
  }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <p class="eyebrow">Your reading room · {day_range}</p>
      <h1>The Digest</h1>
      <div class="stats">
        <span><b>{total}</b> newsletters</span>
        <span><b>{total_time}</b> min total read</span>
        <span>Generated {generated}</span>
      </div>
    </header>

    <nav class="filters">{chips}</nav>

    <main id="feed">{sections}</main>

    <footer>Auto-collected from your newsletter inbox · tap a category to filter</footer>
  </div>

<script>
  const chips = document.querySelectorAll('.chip');
  const groups = document.querySelectorAll('.group');
  chips.forEach(chip => chip.addEventListener('click', () => {{
    chips.forEach(c => c.classList.remove('chip--active'));
    chip.classList.add('chip--active');
    const cat = chip.dataset.cat;
    groups.forEach(g => {{
      g.style.display = (cat === 'all' || g.dataset.cat === cat) ? '' : 'none';
    }});
  }}));
</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Build a newsletter reading digest.")
    ap.add_argument("--days", type=int, default=7, help="How many days back to pull.")
    ap.add_argument("--all", action="store_true", help="Include already-read mail.")
    ap.add_argument("--no-ai", action="store_true", help="Skip Claude summaries.")
    args = ap.parse_args()

    print(f"Connecting to {IMAP_HOST} as {IMAP_USER or '(unset)'} ...")
    mail = connect(IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASSWORD)
    try:
        items = fetch_newsletters(mail, args.days, IMAP_FOLDER, only_unread=not args.all)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    print(f"Found {len(items)} newsletters in the last {args.days} day(s).")
    if not items:
        print("Nothing to digest. Try --days 14 or --all.")
        return

    if args.no_ai or not ANTHROPIC_API_KEY:
        if not args.no_ai:
            print("No ANTHROPIC_API_KEY set — grouping by sender without summaries.")
        items = group_only(items)
    else:
        print("Summarising with Claude ...")
        items = enrich_with_ai(items, ANTHROPIC_API_KEY, MODEL)

    items.sort(key=lambda it: it["date"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)

    label = f"last {args.days} days"
    html_out = build_html(items, label)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fname = os.path.join(OUTPUT_DIR,
                         f"digest_{datetime.now():%Y-%m-%d}.html")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"\nDigest ready → {fname}")
    print("Open it in a browser, or sync the folder to your phone.")


if __name__ == "__main__":
    main()
