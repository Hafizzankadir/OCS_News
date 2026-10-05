#!/usr/bin/env python3
"""Fetch the latest official releases and rebuild the OCS News pages.

Runs daily from .github/workflows/update-news.yml. Standard library only.

For every organisation the script tries, in order:
  1. the WordPress REST API  (<site>/wp-json/wp/v2/posts)
  2. the WordPress RSS feed  (<site>/feed/)
  3. Google News RSS limited to the organisation's domain (fallback for
     SharePoint sites and anything the first two can't reach)

New articles (matched by URL and title) are merged into the ARTICLES data
already embedded in each page, then the cards, counts and "Compiled" date
are regenerated. If a source fails, its existing articles are kept as-is.

Usage:
  python3 scripts/update_news.py            # fetch and update pages
  python3 scripts/update_news.py --offline  # re-render only (no network)
"""

import datetime as dt
import email.utils
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_PER_ORG = 30          # newest articles kept per organisation
MAX_AGE_DAYS = 120        # ignore fetched items older than this
SNIPPET_LEN = 190
TIMEOUT = 25
USER_AGENT = ("Mozilla/5.0 (compatible; OCS-News-Updater/1.0; "
              "+https://github.com/Hafizzankadir/OCS_News)")

# orgId -> (site base URL, fallback location)
MILITARY = {
    "mindef": ("https://www.mindef.gov.bn", "Brunei Darussalam"),
    "rblf":   ("https://land.mindef.gov.bn", "Brunei Darussalam"),
    "rbairf": ("https://airforce.mindef.gov.bn", "Brunei Darussalam"),
    "rbn":    ("https://navy.mindef.gov.bn", "Brunei Darussalam"),
    "da":     ("https://da.mindef.gov.bn", "Brunei Darussalam"),
    "ti":     ("https://ilabdb.mindef.gov.bn", "Brunei Darussalam"),
    "jfhq":   ("https://jfhq.mindef.gov.bn", "Brunei Darussalam"),
    "ocs":    ("https://ocs.mindef.gov.bn", "Brunei Darussalam"),
}
GOVERNMENT = {
    "pmo":    ("https://www.pmo.gov.bn", "Brunei Darussalam"),
    "mofe":   ("https://www.mofe.gov.bn", "Brunei Darussalam"),
    "mindef": ("https://www.mindef.gov.bn", "Brunei Darussalam"),
    "mfa":    ("https://www.mfa.gov.bn", "Brunei Darussalam"),
    "moha":   ("https://www.moha.gov.bn", "Brunei Darussalam"),
    "moe":    ("https://www.moe.gov.bn", "Brunei Darussalam"),
    "mprt":   ("https://www.mprt.gov.bn", "Brunei Darussalam"),
    "mod":    ("https://www.mod.gov.bn", "Brunei Darussalam"),
    "kkbs":   ("https://www.kkbs.gov.bn", "Brunei Darussalam"),
    "moh":    ("https://moh.gov.bn", "Brunei Darussalam"),
    "mora":   ("https://www.mora.gov.bn", "Brunei Darussalam"),
    "mtic":   ("https://www.mtic.gov.bn", "Brunei Darussalam"),
}

PAGES = [
    # file, sources, how dates are displayed on that page
    ("brunei-military-news.html", MILITARY, lambda d: f"{d:%a}, {d.day} {d:%b %Y}"),
    ("brunei-government-news.html", GOVERNMENT, lambda d: f"{d.day} {d:%b %Y}"),
]

TODAY = dt.date.today()
_cache = {}


# ---------------------------------------------------------------- helpers

def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url):
    if url in _cache:
        return _cache[url]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - any failure just means "skip"
        log(f"    ! {url}: {e}")
        data = None
    _cache[url] = data
    return data


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\[\s*(…|\.\.\.|&hellip;)\s*\]", "", s)
    s = re.sub(r"(Continue reading|Read more).*$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def first_sentences(text, limit=420):
    """Body text for the modal: whole sentences up to ~limit chars."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind(".”"))
    if end > 120:
        return cut[:end + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def norm_url(u):
    p = urllib.parse.urlparse(u.strip())
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")).lower()


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def guess_location(body, fallback):
    # Releases typically open "BERAKAS GARRISON, Monday, 31 August 2026 – ..."
    m = re.match(r"\s*([A-Z][A-Z'’ .\-]{2,60}?),\s*(?:[A-Za-z]+day)?", body)
    if m and m.group(1).upper() == m.group(1):
        return m.group(1).title().replace("'S", "'s")
    return fallback


# ---------------------------------------------------------------- sources

def from_wp_rest(base):
    url = (f"{base}/wp-json/wp/v2/posts?per_page=15"
           "&_fields=date,link,title,excerpt,content")
    data = get(url)
    if not data:
        return []
    try:
        posts = json.loads(data)
    except ValueError:
        return []
    if not isinstance(posts, list):
        return []
    out = []
    for p in posts:
        try:
            d = dt.date.fromisoformat(p["date"][:10])
            title = strip_tags(p["title"]["rendered"])
            body = strip_tags((p.get("content") or {}).get("rendered", "")) \
                or strip_tags((p.get("excerpt") or {}).get("rendered", ""))
            out.append({"title": title, "iso": d, "url": p["link"], "body": body})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_rss(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    out = []
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title") or "")
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        try:
            d = email.utils.parsedate_to_datetime(pub).date()
        except (TypeError, ValueError):
            continue
        body = strip_tags(it.findtext("content:encoded", namespaces=ns) or "") \
            or strip_tags(it.findtext("description") or "")
        src = it.find("source")
        out.append({"title": title, "iso": d, "url": link, "body": body,
                    "source": src.text if src is not None else None})
    return out


def from_wp_feed(base):
    data = get(f"{base}/feed/")
    return parse_rss(data) if data and "<rss" in data[:500] else []


def from_google_news(base):
    host = urllib.parse.urlparse(base).netloc.removeprefix("www.")
    q = urllib.parse.quote(f"site:{host} when:60d")
    data = get(f"https://news.google.com/rss/search?q={q}&hl=en-BN&gl=BN&ceid=BN:en")
    if not data:
        return []
    items = parse_rss(data)
    for it in items:
        # Google appends " - Publisher" to titles and its description just
        # repeats the headline, so keep only the clean title.
        if it.get("source"):
            it["title"] = re.sub(r"\s+-\s+" + re.escape(it["source"]) + r"\s*$",
                                 "", it["title"])
        it["body"] = ""
    return items


def fetch(base):
    for fn in (from_wp_rest, from_wp_feed, from_google_news):
        items = [i for i in fn(base) if i["title"] and i["url"]]
        if items:
            log(f"    {fn.__name__}: {len(items)} items")
            return items
    return []


# ---------------------------------------------------------------- page I/O

ART_START = "const ARTICLES = "


def load_articles(page):
    i = page.index(ART_START) + len(ART_START)
    j = page.index("];\n", i) + 1
    return json.loads(page[i:j])


def esc(s):
    return html.escape(s, quote=True)


def snippet(body):
    if len(body) <= SNIPPET_LEN:
        return body
    return body[:SNIPPET_LEN].rsplit(" ", 1)[0] + "…"


def render_card(a):
    search = " ".join([a["title"], a["body"], a["orgName"], a["date"], a["loc"]]).lower()
    return (
        f'    <div class="card" data-id="{a["id"]}" data-org="{esc(a["org"])}" '
        f'data-date="{a["iso"]}" data-search="{esc(search)}">\n'
        f'      <span class="date">{esc(a["date"])} · {esc(a["loc"])}</span>\n'
        f'      <h3>{esc(a["title"])}</h3>\n'
        f'      <p>{esc(snippet(a["body"]))}</p>\n'
        f'      <button class="read-more" data-id="{a["id"]}">Read full article →</button>\n'
        f'    </div>\n'
    )


def rebuild(page, articles):
    # Cards inside each <section class="org" id="..."> ... <div class="cards">
    for org_id in dict.fromkeys(a["orgId"] for a in articles):
        cards = "".join(render_card(a) for a in articles if a["orgId"] == org_id)
        pat = re.compile(
            r'(<section class="org" id="' + re.escape(org_id) +
            r'">.*?<div class="cards">\n)(.*?)(  </div>\n  <div class="older-note">)',
            re.S)
        page, n = pat.subn(lambda m: m.group(1) + cards + m.group(3), page, count=1)
        if n != 1:
            raise SystemExit(f"could not find cards block for section '{org_id}'")

    i = page.index(ART_START) + len(ART_START)
    j = page.index("];\n", i) + 1
    page = page[:i] + json.dumps(articles, ensure_ascii=False) + page[j:]

    page = re.sub(r"const TODAY = new Date\('[^']*'\);",
                  f"const TODAY = new Date('{TODAY.isoformat()}T23:59:59');", page)
    page = re.sub(r'<div class="updated">[^<]*</div>',
                  f'<div class="updated">Compiled {TODAY.day} {TODAY:%B %Y}</div>', page)
    return page


def update_page(fname, sources, fmt_date, offline):
    path = ROOT / fname
    page = path.read_text(encoding="utf-8")
    articles = load_articles(page)
    org_meta = {a["orgId"]: (a["org"], a["orgName"]) for a in articles}
    order = list(dict.fromkeys(a["orgId"] for a in articles))

    seen_urls = {norm_url(a["url"]) for a in articles}
    seen_titles = {norm_title(a["title"]) for a in articles}
    added = 0

    if not offline:
        for org_id in order:
            if org_id not in sources:
                continue
            base, fallback_loc = sources[org_id]
            log(f"  {org_id}: {base}")
            org, org_name = org_meta[org_id]
            for it in fetch(base):
                if (TODAY - it["iso"]).days > MAX_AGE_DAYS or it["iso"] > TODAY:
                    continue
                if norm_url(it["url"]) in seen_urls or norm_title(it["title"]) in seen_titles:
                    continue
                body = first_sentences(it["body"]) or it["title"]
                articles.append({
                    "id": "", "org": org, "orgId": org_id, "orgName": org_name,
                    "title": it["title"],
                    "date": fmt_date(it["iso"]),
                    "iso": it["iso"].isoformat(),
                    "loc": guess_location(it["body"], fallback_loc),
                    "url": it["url"], "body": body,
                })
                seen_urls.add(norm_url(it["url"]))
                seen_titles.add(norm_title(it["title"]))
                added += 1

    # Group by organisation (keeping section order), newest first, capped.
    result = []
    for org_id in order:
        group = [a for a in articles if a["orgId"] == org_id]
        group.sort(key=lambda a: a["iso"], reverse=True)  # stable for ties
        result.extend(group[:MAX_PER_ORG])
    for n, a in enumerate(result):
        a["id"] = f"a{n}"

    new_page = rebuild(page, result)
    changed = new_page != page
    if changed:
        path.write_text(new_page, encoding="utf-8")
    log(f"{fname}: +{added} new, {len(result)} total, changed={changed}")
    return len(result)


def update_index(mil_count, gov_count):
    path = ROOT / "index.html"
    page = path.read_text(encoding="utf-8")
    new = re.sub(r"— \d+ articles, searchable with a timeframe filter",
                 f"— {mil_count} articles, searchable with a timeframe filter", page)
    new = re.sub(r"— \d+ articles, searchable with timeframe and ministry filters",
                 f"— {gov_count} articles, searchable with timeframe and ministry filters", new)
    if new != page:
        path.write_text(new, encoding="utf-8")


def main():
    offline = "--offline" in sys.argv
    counts = [update_page(f, s, d, offline) for f, s, d in PAGES]
    update_index(*counts)


if __name__ == "__main__":
    main()
