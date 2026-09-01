#!/usr/bin/env python3
"""Amazon Affiliate Links Dashboard - Scraper"""

import email.utils
import hashlib
import html
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
    LISBON_TZ = ZoneInfo("Europe/Lisbon")
except Exception:
    LISBON_TZ = timezone.utc
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests
from bs4 import BeautifulSoup

# --- Configuration ---
STATE_FILE = Path(__file__).parent / "downloaded.json"

# Cloudflare R2 Configuration
CF_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")

# ===== SECURE CONFIG FROM GITHUB SECRETS =====
# All sensitive data (channel names, URLs, API keys) must be in GitHub Secrets
# to keep the public repo safe. See .github/SECURITY.md for setup.

# Affiliate tags (per-marketplace)
AMAZON_AFFILIATE_TAG = os.environ.get("AMAZON_AFFILIATE_TAG", "default-tag")
AMAZON_AFFILIATE_TAGS = {}
_tags_json = os.environ.get("AMAZON_AFFILIATE_TAGS_JSON", "{}")
try:
    AMAZON_AFFILIATE_TAGS = json.loads(_tags_json)
except:
    pass

# Telegram channels to scan for Amazon links (JSON format)
AMAZON_TELEGRAM_CHANNELS = []
_tg_json = os.environ.get("AMAZON_TELEGRAM_CHANNELS_JSON", "[]")
try:
    AMAZON_TELEGRAM_CHANNELS = json.loads(_tg_json)
except:
    pass

# Web pages to scan for Amazon links (JSON format)
AMAZON_WEB_PAGES = []
_web_json = os.environ.get("AMAZON_WEB_PAGES_JSON", "[]")
try:
    AMAZON_WEB_PAGES = json.loads(_web_json)
except:
    pass

# All source URLs/channels come from ONE secret (SOURCES_JSON) so nothing
# identifying lives in the public code. Missing keys disable that source.
SOURCES = {}
try:
    SOURCES = json.loads(os.environ.get("SOURCES_JSON", "{}"))
except Exception:
    pass

# How many recent Telegram posts to read per channel when hunting for links.
AMAZON_TELEGRAM_MAX_POSTS = 30

# Show only the latest "batch": links from posts published within this many
# hours of the most recent post that has links (the channel posts in bursts,
# so this always shows the freshest batch and never an empty list).
AMAZON_BATCH_WINDOW_HOURS = int(os.environ.get("AMAZON_BATCH_WINDOW_HOURS", "24"))

# How far back to treat a reference-source post as "current" and exclude its
# ASINs from every tab. The source re-promotes deals in the morning and keeps
# them all day, so 24h (not 12h) covers a full promotion day.
REFERENCE_WINDOW_HOURS = int(os.environ.get("REFERENCE_WINDOW_HOURS", "12"))

# Expanding short links (amzlink.to/amzn.to) costs one HTTP request each. We
# cache resolutions in R2 and only resolve up to N new links per run so the
# 10-min job never times out; the rest are picked up on the next runs.
AMAZON_MAX_RESOLVE_PER_RUN = int(os.environ.get("AMAZON_MAX_RESOLVE_PER_RUN", "120"))

# For links whose URL has no slug (the bare /dp/ASIN form), fetch the real
# product title from Amazon. It's intermittently blocked, so we retry across
# runs and cache the names we do get. Capped per run to keep the job fast.
# Amazon blocks automated IPs (GitHub + Cloudflare), so the direct title fetch
# rarely works — off by default. Names come from the URL slug, the source's own
# title, and a shared ASIN->name map built from the sources that do expose names.
AMAZON_FETCH_TITLES = os.environ.get("AMAZON_FETCH_TITLES", "false").lower() == "true"
AMAZON_MAX_TITLE_FETCH_PER_RUN = int(os.environ.get("AMAZON_MAX_TITLE_FETCH_PER_RUN", "30"))

# Keepa price-history API (optional) — used to flag DEZ deals that are at their
# all-time-low price. Needs a paid API key; without it the feature is disabled.
KEEPA_API_KEY = os.environ.get("KEEPA_API_KEY", "").strip()
KEEPA_DOMAIN = int(os.environ.get("KEEPA_DOMAIN", "9"))      # 9 = amazon.es
KEEPA_TTL_HOURS = int(os.environ.get("KEEPA_TTL_HOURS", "6"))  # re-price each ASIN every 6h (catch fresh drops)
# Token safety: cap how many NEW titles we look up per list each run (each is
# queried at most once ever, then cached). Keeps token use well under budget.
KEEPA_MAX_TITLES_PER_LIST = int(os.environ.get("KEEPA_MAX_TITLES_PER_LIST", "25"))
# All-time-low flag is transversal to every tab; cap how many ASINs we (re)price
# per run so token use stays well within budget (each ~1 token, cached 24h).
# Keepa refills 5 tokens/min (300/h) and we scan 6x/h, so 50/run spends exactly
# the refill rate — full use of the quota without ever draining the balance.
KEEPA_MAX_PRICE_PER_RUN = int(os.environ.get("KEEPA_MAX_PRICE_PER_RUN", "50"))
# Flag as all-time low when current <= lowest * (1 + margin). 0.005 = 0.5%,
# per the Keepa minimum-detection module spec.
KEEPA_LOW_MARGIN = float(os.environ.get("KEEPA_LOW_MARGIN", "0.005"))
# Keepa root categories to exclude from every tab (no books). 599364031 = Libros
# (amazon.es). Set via secret BOOK_CATS_JSON to add more (e.g. Kindle store).
BOOK_CATS = set()
try:
    BOOK_CATS = set(json.loads(os.environ.get("BOOK_CATS_JSON", "[599364031]")))
except Exception:
    BOOK_CATS = {599364031}


# Amazon dashboard lives in its OWN R2 bucket + worker. The bucket name comes
# from a secret; it is served by a dedicated Cloudflare Worker.
AMAZON_R2_BUCKET = os.environ.get("AMAZON_R2_BUCKET", "amazon-dashboard")
AMAZON_API_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/r2/buckets/{AMAZON_R2_BUCKET}"

# --- Logging setup ---

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("amz")




def sanitize_filename(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Strip trailing dots/whitespace — they cause "..mp4" which R2 rejects (403)
    cleaned = cleaned.rstrip(". ")
    if len(cleaned) > 200:
        cleaned = cleaned[:200].rstrip(". ")
    if not cleaned:
        cleaned = "video"
    return cleaned


# --- Cloudflare R2 via API ---

def r2_headers() -> dict:
    return {"Authorization": f"Bearer {CF_API_TOKEN}"}


# ==========================================================================
# Amazon affiliate link finder
# ==========================================================================

AMAZON_STATE_KEY = "data/amazon_links.json"

# Hosts that point at an Amazon product (long domains + short redirectors).
AMAZON_HOST_RE = re.compile(
    r"https?://[^\s\"'<>)]*?(?:"
    r"amazon\.[a-z.]{2,7}"               # amazon.es, amazon.com, amazon.co.uk ...
    r"|amzn\.to|amzn\.eu|a\.co|amzlink\.to"  # short redirectors
    r"|link\.amazon(?=/)"                # .amazon-gTLD shortener (path = code)
    r")[^\s\"'<>)]*",
    re.IGNORECASE,
)
AMAZON_SHORT_HOSTS = ("amzn.to", "amzn.eu", "a.co", "amzlink.to", "link.amazon")
# ASIN inside a product path: /dp/ASIN, /gp/product/ASIN, /gp/aw/d/ASIN, /product/ASIN
ASIN_RE = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|product|gp/aw/d|gp/offer-listing)/([A-Z0-9]{10})",
    re.IGNORECASE,
)


def _affiliate_tag_for(marketplace: str) -> str:
    return AMAZON_AFFILIATE_TAGS.get(marketplace, AMAZON_AFFILIATE_TAG)


# Some channels post products through a custom shortener whose path carries the
# ASIN directly (e.g. <host>/amz/<ASIN>). Treat those as amazon.es products.
CUSTOM_ASIN_RE = re.compile(r"https?://[^\s\"'<>)]+?/amz/([A-Z0-9]{10})", re.IGNORECASE)


def extract_amazon_urls(text: str) -> list[str]:
    """Find all raw Amazon URLs (long or short) inside a blob of text/HTML,
    plus custom /amz/<ASIN> shortener links (converted to amazon.es)."""
    if not text:
        return []
    t = html.unescape(text)
    found = []
    for m in AMAZON_HOST_RE.finditer(t):
        url = m.group(0).rstrip(".,);]​")
        if url not in found:
            found.append(url)
    for m in CUSTOM_ASIN_RE.finditer(t):
        url = f"https://www.amazon.es/dp/{m.group(1).upper()}"
        if url not in found:
            found.append(url)
    return found


def _amazon_marketplace(host: str) -> str:
    """Normalise an Amazon host to a marketplace id like 'amazon.es'."""
    host = host.lower().replace("www.", "")
    m = re.search(r"(amazon\.[a-z.]{2,7})", host)
    return m.group(1) if m else "amazon.com"


_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-PT,pt;q=0.9,es;q=0.8,en;q=0.7",
}


def _slug_name(path: str) -> str:
    """Derive a readable product name from the slug before /dp/ASIN in the URL."""
    m = re.search(r"/([^/]+)/(?:dp|gp/product|gp/aw/d|product)/[A-Z0-9]{10}", path, re.IGNORECASE)
    if not m:
        return ""
    name = unquote(m.group(1)).replace("-", " ").replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200] if len(name) > 3 else ""


def resolve_amazon_link(raw_url: str) -> dict | None:
    """Expand short links, extract ASIN, and build clean + affiliate URLs.

    Returns a dict (without status/source) or None if it isn't a product link.
    Does NOT fetch the live title — the name comes from the URL slug (callers may
    upgrade it via fetch_amazon_title when AMAZON_FETCH_TITLES is on).
    """
    final_url = raw_url

    # Follow redirects for short links to reach the real product URL.
    parsed = urlparse(raw_url)
    if any(parsed.netloc.lower().endswith(h) for h in AMAZON_SHORT_HOSTS):
        try:
            resp = requests.get(raw_url, headers=_BROWSER_HEADERS, timeout=30, allow_redirects=True)
            final_url = resp.url
        except requests.RequestException as e:
            log.warning("Could not expand Amazon short link %s: %s", raw_url, e)
            return None

    parsed = urlparse(final_url)
    marketplace = _amazon_marketplace(parsed.netloc)

    asin_match = ASIN_RE.search(parsed.path)
    if not asin_match:
        # Some links carry the ASIN in the query (e.g. ?asin=XXXX)
        qs = parse_qs(parsed.query)
        asin = (qs.get("asin") or qs.get("ASIN") or [""])[0]
        asin = asin.upper() if asin else ""
    else:
        asin = asin_match.group(1).upper()

    if not asin:
        log.info("No ASIN found in Amazon link: %s", final_url)
        return None

    # Clean product URL — no affiliate tag, no tracking params.
    clean_url = f"https://www.{marketplace}/dp/{asin}"

    return {
        "id": f"{asin}-{marketplace}",
        "asin": asin,
        "marketplace": marketplace,
        "clean_url": clean_url,
        "affiliate_url": clean_url,   # kept for downstream compatibility
        "name": _slug_name(parsed.path) or f"Produto {asin}",
    }


def fetch_amazon_title(product_url: str, headers: dict | None = None) -> str:
    """Best-effort direct fetch of the product name. Amazon often serves a robot
    page (title just "Amazon.es") to automated IPs — treated as a miss."""
    try:
        resp = requests.get(product_url, headers=headers or _BROWSER_HEADERS, timeout=12)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        node = soup.select_one("#productTitle")
        if node and node.get_text(strip=True):
            return node.get_text(strip=True)[:200]
        if soup.title and soup.title.get_text(strip=True):
            title = soup.title.get_text(strip=True)
            # Strip trailing "... : Amazon.es" / leading "Amazon.com: ..." noise
            title = re.sub(r"\s*[:|-]\s*Amazon\.[a-z.]+\s*$", "", title, flags=re.IGNORECASE)
            title = re.sub(r"^\s*Amazon\.[a-z.]+\s*[:|-]\s*", "", title, flags=re.IGNORECASE)
            title = title.strip()
            # Reject the generic blocked/robot page title ("Amazon.es", "Amazon").
            if re.fullmatch(r"Amazon(\.[a-z.]+)?", title, flags=re.IGNORECASE) or len(title) < 4:
                return ""
            return title[:200]
    except requests.RequestException:
        pass
    return ""


def get_telegram_link_posts(channel: str) -> list[dict]:
    """Scrape a public Telegram channel for recent posts' text (for link mining).
    Try both official preview hosts — one can serve an empty page for a given
    channel/region while the other returns the feed."""
    out = []
    resp = None
    # telegram.me first — t.me has been unreliable from the CI region; kept only
    # as a last-resort fallback.
    for host in ("telegram.me", "t.me"):
        try:
            r = requests.get(f"https://{host}/s/{channel}", timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("Telegram %s/%s: %s", host, channel, e)
            continue
        if "data-post" in r.text:   # this host has the feed
            resp = r
            break
        resp = r                    # keep as fallback (may still be empty)
    if resp is None:
        log.error("Failed to fetch Telegram channel %s (both hosts)", channel)
        return out

    soup = BeautifulSoup(resp.text, "html.parser")

    for div in soup.select("[data-post]")[-AMAZON_TELEGRAM_MAX_POSTS:]:
        dt = None
        date_link = div.select_one(".tgme_widget_message_date time[datetime]")
        if date_link:
            try:
                dt = datetime.fromisoformat(date_link["datetime"])
            except (ValueError, KeyError):
                pass
        text_div = div.select_one(".tgme_widget_message_text") or div.select_one(".message_text")
        if not text_div:
            continue
        # Use the raw HTML so href="" attributes (hidden behind anchor text) are seen too.
        out.append({"channel": channel, "html": str(text_div), "dt": dt})
    return out


AMAZON_CLEARED_KEY = "data/cleared.json"


def r2_get_amazon_cleared() -> set:
    """URLs the admin cleared — excluded so they never come back."""
    if not CF_API_TOKEN:
        return set()
    url = f"{AMAZON_API_BASE}/objects/{quote(AMAZON_CLEARED_KEY, safe='')}"
    try:
        resp = requests.get(url, headers=r2_headers(), timeout=30)
        if resp.status_code == 200:
            return set(resp.json())
    except Exception as e:
        log.warning("Failed to load cleared list: %s", e)
    return set()


def r2_get_amazon_links(key: str = AMAZON_STATE_KEY) -> dict:
    """Load the stored Amazon links state from R2 (or local fallback)."""
    if not CF_API_TOKEN:
        local = Path(__file__).parent / Path(key).name
        if local.exists():
            with open(local, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"updated": "", "links": []}

    url = f"{AMAZON_API_BASE}/objects/{quote(key, safe='')}"
    try:
        resp = requests.get(url, headers=r2_headers(), timeout=30)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.warning("Failed to load amazon links from R2: %s", e)
    return {"updated": "", "links": []}


def r2_put_amazon_links(data: dict, key: str = AMAZON_STATE_KEY):
    """Persist the Amazon links state to R2 (and locally)."""
    with open(Path(__file__).parent / Path(key).name, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if not CF_API_TOKEN:
        return
    url = f"{AMAZON_API_BASE}/objects/{quote(key, safe='')}"
    headers = {**r2_headers(), "Content-Type": "application/json"}
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        resp = requests.put(url, headers=headers, data=body, timeout=30)
        if resp.status_code != 200:
            log.warning("Failed to upload amazon links: %s", resp.text[:200])
    except Exception as e:
        log.warning("Failed to upload amazon links: %s", e)


def _clean_name(s: str, limit: int = 90) -> str:
    """Turn a source-provided title (may contain HTML/entities) into a short,
    plain product name suitable for a list row."""
    s = re.sub(r"<[^>]+>", "", s or "")     # strip any HTML tags
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rstrip() + "…"   # …
    return s


# A redeemable coupon/promo CODE the user types at checkout, e.g.
# "código: BANG20" / "con el cupón ABC1234". Anchored to a keyword and limited to
# UPPERCASE alphanumerics so plain words don't match. Clip-on-page coupons (no
# code) are not captured here — there is nothing to type. Returns "" if none.
_COUPON_CODE_RE = re.compile(
    r"(?i:c[oó]digo|c[oó]d|cup[oó]n|cupon|cup[aã]o|cupao|cupom|coupon|code|promo)"
    r"[^A-Za-z0-9]{0,15}([A-Z0-9]{5,15})\b")
_COUPON_STOPWORDS = {"AMAZON", "PRIME", "DESCUENTO", "OFERTA", "PRODUCTO", "CODIGO", "CUPON"}

def extract_coupon_code(text: str) -> str:
    """Pull a checkout coupon code out of free text/HTML, or '' if none found."""
    if not text:
        return ""
    plain = re.sub(r"<[^>]+>", " ", text)        # drop HTML tags so adjacency holds
    for m in _COUPON_CODE_RE.finditer(plain):
        code = m.group(1)
        if any(ch.isdigit() for ch in code) and code not in _COUPON_STOPWORDS:
            return code                          # require a digit -> real code, not a word
    return ""


# ---------------------------------------------------------------------------
# Non-Amazon store tabs (AliExpress / PCComponentes): a transversal side
# collector. Every source we already fetch also reports any direct product
# link for these stores; everything lands together in one tab per store.
# ---------------------------------------------------------------------------
STORE_TAB_KEYS = {"aliexpress": "data/aliexpress.json", "pcc": "data/pccomponentes.json"}
_STORE_ITEMS: dict = {"aliexpress": [], "pcc": []}   # reset at the start of each run
_STORE_PATTERNS = (
    ("aliexpress", re.compile(r"https?://(?:s\.click\.|[a-z]{2}\.|www\.)?aliexpress\.[a-z.]{2,6}/[^\s\"'<>\\]+", re.I)),
    ("pcc", re.compile(r"https?://(?:www\.)?pccomponentes\.(?:com|pt)/[^\s\"'<>\\]+", re.I)),
)


def _store_link_ok(store: str, url: str) -> bool:
    """Keep product-looking links only (not category/banner/host-only pages)."""
    if re.match(r"https?://[^/]+/*$", url):           # bare domain -> junk
        return False
    if store == "aliexpress":
        return "/item/" in url or "s.click." in url   # product page or affiliate short link
    path = url.split("/", 3)[-1] if url.count("/") >= 3 else ""
    return path.count("-") >= 3                       # pcc product slugs are long


def _store_slug_name(url: str) -> str:
    """Fallback store-product name from a URL slug ('' when it is just an id)."""
    seg = [s for s in re.sub(r"[?#].*$", "", url).split("/") if s]
    if not seg:
        return ""
    name = re.sub(r"[-_]+", " ", re.sub(r"\.html?$", "", seg[-1])).strip()
    return "" if re.fullmatch(r"[0-9 ]*", name) else _clean_name(name)


def store_add(store: str, url: str, date: str = "", coupon: str = "", name: str = ""):
    _STORE_ITEMS[store].append({"url": url, "date": date or "",
                                "coupon": coupon or "", "name": name or ""})


def store_scan_text(text: str, date: str = "", name: str = ""):
    """Side collector: pull direct store product links out of any source text."""
    if not text:
        return
    cpn = extract_coupon_code(text)
    for store, pat in _STORE_PATTERNS:
        for u in pat.findall(text):
            u = html.unescape(u).rstrip(").,;")
            if _store_link_ok(store, u):
                store_add(store, u, date, cpn, name or _store_slug_name(u))


def _post_text_name(html_text: str) -> str:
    """Product name from a Telegram post body (tags and links stripped)."""
    t = re.sub(r"<[^>]+>", " ", html_text or "")
    t = re.sub(r"https?://\S+", " ", t)
    return _clean_name(re.sub(r"\s+", " ", t).strip()[:90])


# aliexpress.us item ids are the global (.com/.es) id plus this fixed offset,
# so the same product gets one canonical key whichever domain a source used.
_ALI_US_ID_OFFSET = 2251799813685248


def _store_dedupe_key(url: str) -> str:
    m = re.search(r"aliexpress\.[a-z.]+/item/(\d+)", url)
    if m:
        iid = int(m.group(1))
        if iid >= _ALI_US_ID_OFFSET:
            iid -= _ALI_US_ID_OFFSET
        return f"ali:{iid}"
    return re.sub(r"[?#].*$", "", url).rstrip("/")


def _name_tokens(name: str) -> list:
    """Accent-folded lowercase word tokens of a product name (for fuzzy dedupe).
    Metadata tails after '|' (price, condition) are not part of the name."""
    s = (name or "").split("|", 1)[0]
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", s)


def _same_product(a: list, b: list) -> bool:
    """Two names are the same product when they agree on the first two words and
    share >=70% of their vocabulary. Conservative on purpose: 'chollo pack 4x
    aceite...' vs 'chollo aceite...' (different deals) stays separate."""
    if len(a) < 2 or len(b) < 2 or a[:2] != b[:2]:
        return False
    inter = len(set(a) & set(b))
    union = len(set(a) | set(b))
    return union > 0 and inter / union >= 0.7


def _store_row_score(store: str, l: dict) -> int:
    """Which duplicate to keep: direct store product link beats a deal-page
    link, then having a coupon, then having a real name."""
    s = 0
    url = l.get("url", "")
    if (store == "aliexpress" and re.search(r"aliexpress\.[a-z.]+/item/", url)) or \
       (store == "pcc" and "pccomponentes." in url):
        s += 4
    if l.get("coupon"):
        s += 2
    if l.get("name") and not l["name"].startswith("Produto "):
        s += 1
    return s


def write_store_tabs(cleared: set):
    """Merge this run's collected store links into each tab's state in R2:
    dedupe by product, keep first-seen dates, drop opened/cleared, newest first."""
    now_iso = datetime.now(timezone.utc).isoformat()
    for store, key in STORE_TAB_KEYS.items():
        state = r2_get_amazon_links(key)
        merged = {_store_dedupe_key(l.get("url", "")): l for l in state.get("links", [])}
        fresh = 0
        for it in _STORE_ITEMS.get(store) or []:
            k = _store_dedupe_key(it["url"])
            prev = merged.get(k)
            if prev:   # upgrade what we now know; keep the original date
                if it.get("coupon") and not prev.get("coupon"):
                    prev["coupon"] = it["coupon"]
                pn = prev.get("name") or ""
                if it.get("name") and (not pn or pn.startswith("Produto ")
                                       or re.fullmatch(r"[0-9 ]+", pn)):
                    prev["name"] = it["name"]
            else:
                it["date"] = it.get("date") or now_iso
                merged[k] = it
                fresh += 1
        links = [l for l in merged.values() if l.get("url") and l["url"] not in cleared]
        # Collapse cross-source duplicates of the same product (direct link vs
        # deal page vs second short link). Best row wins; coupon and freshest
        # date are inherited from the dropped copies.
        kept: list = []
        for l in sorted(links, key=lambda x: _store_row_score(store, x), reverse=True):
            toks = _name_tokens(l.get("name", ""))
            dup = None
            if not (l.get("name") or "").startswith("Produto "):   # placeholders: URL dedupe only
                dup = next((k2 for k2 in kept if _same_product(toks, k2["_t"])), None)
            if dup is not None:
                if l.get("coupon") and not dup.get("coupon"):
                    dup["coupon"] = l["coupon"]
                if (l.get("date") or "") > (dup.get("date") or ""):
                    dup["date"] = l["date"]
                continue
            l["_t"] = toks
            kept.append(l)
        for l in kept:
            l.pop("_t", None)
        links = kept
        links.sort(key=lambda l: l.get("date", ""), reverse=True)
        links = links[:300]
        label = "AliExpress" if store == "aliexpress" else "PCComponentes"
        for l in links:
            # id-only slug and no source gave a name yet -> generic placeholder
            if not l.get("name") or re.fullmatch(r"[0-9 ]+", l["name"]):
                l["name"] = f"Produto {label}"
        for l in links:                      # tidy: drop empty coupon fields
            if not l.get("coupon"):
                l.pop("coupon", None)
        r2_put_amazon_links({"updated": now_iso, "links": links}, key)
        log.info("[%s] store tab: %d links (%d new this run)", key, len(links), fresh)


# ASIN map for the community deals site: thread id -> ASIN ("" = deal page
# checked, no Amazon link found). Persisted in R2 so each page is fetched once.
CHOLLO_MAP_KEY = "data/chollo_map.json"
CHOLLO_MAX_DEAL_FETCH = int(os.environ.get("CHOLLO_MAX_DEAL_FETCH", "12"))


def get_chollo_items() -> list[tuple[str, str, str, bool, str]]:
    """Community deals site: read its 4 listing sections (front, most-voted,
    rising, new). Each listing embeds one JSON object per deal (title, dates,
    coupon, merchant). The merchant-redirect endpoint is bot-blocked (403), so
    the ASIN comes from the deal page's embedded price-comparison widget,
    cached forever by thread id (capped per run, paced).
    Returns (amazon_url, iso_date, coupon, low, name) tuples."""
    out: list[tuple[str, str, str, bool, str]] = []
    base = (SOURCES.get("chollo_visit_base") or "").rstrip("/")
    if not base:
        return out
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    threads: dict[str, dict] = {}
    for path in ("", "/mas-votados", "/populares", "/nuevos"):
        try:
            h = sess.get(f"{base}{path}", timeout=30).text
        except requests.RequestException as e:
            log.error("chollo list %s: %s", path or "/", e)
            continue
        for m in re.finditer(r"data-vue3='(\{\"name\":\"ThreadMainListItemNormalizer\".*?\})'", h):
            try:
                t = json.loads(html.unescape(m.group(1)))["props"]["thread"]
            except (ValueError, KeyError, TypeError):
                continue
            merchant = (t.get("merchant") or {}).get("merchantUrlName") or ""
            tid = str(t.get("threadId") or "")
            if not tid:
                continue
            # Other tracked stores: merchant link is unreachable (blocked
            # redirect), so the row opens the deal page itself.
            st = {"es.aliexpress.com": "aliexpress", "aliexpress.com": "aliexpress",
                  "pccomponentes.com": "pcc", "www.pccomponentes.com": "pcc"}.get(merchant)
            if st:
                ts0 = t.get("publishedAt")
                store_add(st, f"{base}/ofertas/{t.get('titleSlug', 'x')}-{tid}",
                          datetime.fromtimestamp(int(ts0), timezone.utc).isoformat() if ts0 else "",
                          extract_coupon_code("cupón " + (t.get("voucherCode") or "")),
                          _clean_name(t.get("title", "")))
                continue
            if merchant != "amazon.es":
                continue
            if tid not in threads:
                threads[tid] = t
    state = r2_get_amazon_links(CHOLLO_MAP_KEY)
    tmap: dict = state.get("t", {})
    fetched = 0
    for tid, t in threads.items():
        asin = tmap.get(tid)
        if asin is None:                      # deal page never looked at
            if fetched >= CHOLLO_MAX_DEAL_FETCH:
                continue                      # picked up on a later run
            fetched += 1
            try:
                page = sess.get(f"{base}/ofertas/{t.get('titleSlug', 'x')}-{tid}",
                                timeout=30).text
                if len(page) < 2000:          # slug drift -> meta-refresh stub
                    rm = re.search(r"url='?\"?(https?://[^'\">]+)", page)
                    if rm:
                        page = sess.get(html.unescape(rm.group(1)), timeout=30).text
            except requests.RequestException as e:
                log.warning("chollo deal %s: %s", tid, e)
                continue
            am = (re.search(r"oferta\\?/amazon_([a-zA-Z0-9]{10})\b", page)
                  or re.search(r"amazon\.es\\?/(?:[^\"'<>\s]*?\\?/)?dp\\?/([A-Z0-9]{10})", page))
            asin = am.group(1).upper() if am else ""
            tmap[tid] = asin                  # "" caches the misses too
        if not asin:
            continue
        ts = t.get("publishedAt")
        date = datetime.fromtimestamp(int(ts), timezone.utc).isoformat() if ts else ""
        # voucherCode is sometimes descriptive text ("Cupón 23% descuento"), not
        # a code — keep only a real typable code for the click-to-copy chip.
        voucher = extract_coupon_code("cupón " + (t.get("voucherCode") or ""))
        out.append((f"https://www.amazon.es/dp/{asin}", date, voucher, False,
                    _clean_name(t.get("title", ""))))
    if len(tmap) > 20000:                     # keep the map bounded
        tmap = dict(list(tmap.items())[-20000:])
    r2_put_amazon_links({"t": tmap}, CHOLLO_MAP_KEY)
    log.info("chollo: %d amazon.es deals (%d with coupon) across 4 sections, %d pages fetched",
             len(out), sum(1 for o in out if o[2]), fetched)
    return out


# Highlighted / Prime-guaranteed buyable price series — read from Keepa's
# official stats (stats.min = all-time, stats.current = now):
#   0 Amazon · 8 Lightning/flash deal · 18 Buy Box · 33 New Prime-exclusive
# Generic "New" (1) and 3rd-party FBM/FBA (7/10) are EXCLUDED — volatile marketplace
# spikes that gave false 'not at min'. stats.current is -1 for an ended Lightning,
# so it never counts a stale flash price as the current price.
KEEPA_PRICE_SERIES = (0, 8, 18, 33)
KEEPA_TYPE_NAMES = {0: "Amazon", 8: "Oferta-relâmpago", 18: "Buy Box", 33: "Prime exclusive"}


def keepa_low_refresh(asins: list, low_cache: dict, max_count: int = KEEPA_MAX_PRICE_PER_RUN) -> int:
    """Refresh the all-time-low flag for ASINs (transversal to every tab) and
    mutate low_cache: {asin: {"low": bool, "checked": iso}}. A 24h TTL plus a
    per-run cap keep token use ~1/product. Returns how many were (re)priced."""
    if not KEEPA_API_KEY or not asins or max_count <= 0:
        return 0
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=KEEPA_TTL_HOURS)
    stale = []
    for a in asins:
        if not a or a in stale:
            continue
        c = low_cache.get(a)
        fresh = False
        if c and "checked" in c:
            try:
                fresh = (now - datetime.fromisoformat(c["checked"])) < ttl
            except (ValueError, KeyError):
                fresh = False
        if not fresh:
            stale.append(a)
    stale = stale[:max_count]   # cap tokens per run (shared budget)
    done = 0
    for i in range(0, len(stale), 100):
        batch = stale[i:i + 100]
        try:
            r = requests.get("https://api.keepa.com/product",
                             params={"key": KEEPA_API_KEY, "domain": KEEPA_DOMAIN,
                                     "asin": ",".join(batch), "stats": 180, "category": 1}, timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.error("keepa low refresh failed: %s", e)
            break
        if "products" not in data:   # out of tokens / error -> stop, retry later
            log.warning("keepa low: no products (tokensLeft %s)", data.get("tokensLeft"))
            break
        prods = {p.get("asin"): p for p in (data.get("products") or [])}
        for a in batch:
            p = prods.get(a) or {}
            st = p.get("stats") or {}
            mn_arr = st.get("min") or []
            cur_arr = st.get("current") or []
            lowest = current = lowtype = None
            for i in KEEPA_PRICE_SERIES:
                m = mn_arr[i][1] if (i < len(mn_arr) and isinstance(mn_arr[i], list)
                                     and isinstance(mn_arr[i][1], int) and mn_arr[i][1] > 0) else None
                c = cur_arr[i] if (i < len(cur_arr) and isinstance(cur_arr[i], int) and cur_arr[i] > 0) else None
                if m is not None and (lowest is None or m < lowest):
                    lowest, lowtype = m, i
                if c is not None and (current is None or c < current):
                    current = c
            low = bool(lowest and current and current <= lowest * (1 + KEEPA_LOW_MARGIN))
            # Sales rank (csv 3) — popularity; lower = more popular, -1 = unranked.
            rank = cur_arr[3] if (len(cur_arr) > 3 and isinstance(cur_arr[3], int) and cur_arr[3] > 0) else None
            # First product image (medium variant) — just the CDN filename; the
            # browser loads the thumb straight from Amazon's image CDN. Free:
            # it rides in the same 1-token response.
            imgs = p.get("images") or []
            img = (imgs[0].get("m") or imgs[0].get("l") or "") if imgs and isinstance(imgs[0], dict) else ""
            low_cache[a] = {"low": low, "checked": now.isoformat(),
                            "cat": p.get("rootCategory"),
                            "min": lowest, "lbl": KEEPA_TYPE_NAMES.get(lowtype, ""),
                            "rank": rank, "i": img}
            done += 1
    log.info("keepa low: refreshed %d asins, %d now at all-time low",
             done, sum(1 for v in low_cache.values() if v.get("low")))
    return done


def keepa_titles(asins: list) -> tuple:
    """Fetch product titles from Keepa (paid) for ASINs we couldn't name any
    other way. history=0 keeps it to ~1 token per product. Returns
    (titles, queried): titles is {asin: name}; queried is the set of ASINs the
    API actually answered for (so callers can avoid ever re-querying them)."""
    titles, queried = {}, set()
    if not KEEPA_API_KEY or not asins:
        return titles, queried
    for i in range(0, len(asins), 100):
        batch = asins[i:i + 100]
        try:
            r = requests.get("https://api.keepa.com/product",
                             params={"key": KEEPA_API_KEY, "domain": KEEPA_DOMAIN,
                                     "asin": ",".join(batch), "history": 0, "stats": 0},
                             timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.error("keepa titles request failed: %s", e)
            break   # don't mark as queried — retry next run
        if "products" not in data:   # e.g. out of tokens -> don't mark queried
            log.warning("keepa titles: no products (tokens left: %s)", data.get("tokensLeft"))
            break
        queried.update(batch)
        for p in (data.get("products") or []):
            a = p.get("asin")
            t = _clean_name(p.get("title") or "")
            if a and t:
                titles[a] = t
    log.info("keepa titles: named %d, queried %d", len(titles), len(queried))
    return titles, queried


def reference_recent_asins(sess, hours: int = REFERENCE_WINDOW_HOURS) -> tuple:
    """What the reference source PUBLISHED *or* UPDATED in the last N hours.
    The source re-promotes old posts by updating them (keeping the original
    date_gmt), so we order by `modified` and treat a post as recent when EITHER
    its date_gmt (published) OR its modified_gmt (updated) is within the window.
    Posts use short links, so expand them. Returns (asins, worten_urls)."""
    base = SOURCES.get("cupo_api", "")
    if not base:
        return set(), set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cupo_links: list[str] = []
    wt_short: list[str] = []
    stop = False
    for page in (1, 2, 3, 4):
        if stop:
            break
        try:
            r = sess.get(f"{base}?per_page=100&page={page}&orderby=modified&order=desc", timeout=40)
            if r.status_code != 200:
                break
            posts = r.json()
        except (requests.RequestException, ValueError):
            break
        if not posts:
            break
        for p in posts:
            # Recent if published OR updated within the window. *_gmt is UTC
            # without a zone; take the most recent of the two timestamps.
            stamps = []
            for fld in ("modified_gmt", "date_gmt"):
                try:
                    stamps.append(datetime.fromisoformat(p.get(fld) or "").replace(tzinfo=timezone.utc))
                except ValueError:
                    pass
            if not stamps:
                continue
            if max(stamps) < cutoff:   # ordered by modified desc -> the rest are older
                stop = True
                break
            content = (p.get("content", {}) or {}).get("rendered", "")
            cupo_links += extract_amazon_urls(content)
            # Worten deals on the reference source hide behind an Awin short
            # link (tidd.ly) or a direct worten.pt link — collect both.
            wt_short += re.findall(r"https?://tidd\.ly/[A-Za-z0-9]+", content)
            wt_short += re.findall(r"https?://(?:www\.)?worten\.pt/[^\s\"'<>\\]+", content)
    # Persistent resolution cache: each short link is OPENED AT MOST ONCE, ever.
    # Following a shortener registers a (bot) affiliate click on Amazon/Awin —
    # without this cache the same ~40 links would be re-clicked every 10 min.
    REF_CACHE_KEY = "data/ref_cache.json"
    rc = r2_get_amazon_links(REF_CACHE_KEY)
    amap: dict = rc.get("a", {})   # source link -> ASIN ("" = not a product)
    wmap: dict = rc.get("w", {})   # awin short link -> worten url ("" = not worten)
    opened = 0
    recent = set()
    for u in dict.fromkeys(cupo_links):
        if u in amap:
            if amap[u]:
                recent.add(amap[u])
            continue
        short = any(h in u for h in AMAZON_SHORT_HOSTS)
        if short:
            opened += 1               # this one costs a real redirect follow
        rr = resolve_amazon_link(u)
        if rr:
            amap[u] = rr["asin"]
            recent.add(rr["asin"])
        elif not short:
            amap[u] = ""              # direct non-product link: permanent miss
        # failed short links stay uncached -> retried while inside the window
    wturls = set()
    for u in dict.fromkeys(wt_short):
        if "tidd.ly" not in u:        # direct worten link, nothing to open
            if re.search(r"worten\.pt/", u):
                wturls.add(re.sub(r"[?#].*$", "", html.unescape(u)).rstrip(").,;/"))
            continue
        if u in wmap:
            if wmap[u]:
                wturls.add(wmap[u])
            continue
        try:
            final = sess.get(u, timeout=20, allow_redirects=True).url
            opened += 1
        except requests.RequestException:
            continue                  # retried next run
        wu = (re.sub(r"[?#].*$", "", html.unescape(final)).rstrip(").,;/")
              if re.search(r"worten\.pt/", final) else "")
        wmap[u] = wu
        if wu:
            wturls.add(wu)
    if len(amap) > 20000:
        amap = dict(list(amap.items())[-20000:])
    if len(wmap) > 20000:
        wmap = dict(list(wmap.items())[-20000:])
    r2_put_amazon_links({"a": amap, "w": wmap}, REF_CACHE_KEY)
    log.info("reference source last %dh (published or updated): %d asins, %d worten "
             "(%d links opened this run)", hours, len(recent), len(wturls), opened)
    return recent, wturls


def get_dez_items(cupo_recent=None) -> list[tuple[str, str, str, bool]]:
    """Source mirrors the reference source. Read its deals API (the ASIN
    is in productId) and keep only the ones whose ASIN was NOT published on
    the reference source in the last 12 hours. Returns (amazon_url, iso_date, coupon)."""
    out: list[tuple[str, str, str]] = []
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)

    dez_api = SOURCES.get("dez_api", "")
    if not dez_api:
        return out

    if cupo_recent is None:
        cupo_recent = reference_recent_asins(sess, hours=REFERENCE_WINDOW_HOURS)[0]   # ASINs to exclude

    # Source promotions (productId is the ASIN).
    try:
        promos = sess.get(dez_api, timeout=40).json().get("data", [])
    except (requests.RequestException, ValueError) as e:
        log.error("dez: %s", e)
        return out
    seen = set()
    _dbg = 0
    for p in promos:
        if (p.get("store") or "").lower() != "amazon":
            if _dbg < 4:   # DIAG (temp): shape of non-amazon entries, sanitized
                _dbg += 1
                safe = {k: (v[:60] if isinstance(v, str) else v) for k, v in p.items()
                        if k in ("store", "productId", "title", "coupon", "updatedAt",
                                 "promoDay", "url", "link", "productUrl", "deeplink", "slug", "id")}
                log.info("dez-dbg keys=%s sample=%s", sorted(p.keys()), safe)
            continue
        asin = (p.get("productId") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
            continue
        if asin in cupo_recent:       # on reference source in last 12h -> skip
            continue
        seen.add(asin)
        # The all-time-low flag is now computed transversally (Keepa, all tabs),
        # so just pass the date/coupon/name here.
        out.append((f"https://www.amazon.es/dp/{asin}",
                    p.get("updatedAt") or p.get("promoDay") or "",
                    (p.get("coupon") or "").strip(), False, _clean_name(p.get("title", ""))))
    log.info("dez: %d promos -> %d kept (not on reference source last 12h)", len(promos), len(out))
    return out


def get_mi_items() -> list[tuple[str, str, str, bool, str]]:
    """Deals from a JS-rendered aggregator's JSON API (newest first). Keep only
    Amazon.es deals, resolve each offer link to its ASIN, and use the API's own
    product name and publish date. Returns (amazon_url, iso_date, coupon, low, name)."""
    out: list[tuple[str, str, str, bool, str]] = []
    base = SOURCES.get("mi_api", "")
    if not base:
        return out
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    sess.headers["Accept"] = "application/json"
    seen = set()
    resolved = 0
    for page in (1, 2):
        try:
            data = sess.get(f"{base}?page={page}&limit=50", timeout=40).json()
            results = (data.get("deals", {}) or {}).get("results", [])
        except (requests.RequestException, ValueError) as e:
            log.error("mi: %s", e)
            break
        if not results:
            break
        for r in results:
            stores = r.get("store", []) or []
            sdesc = " ".join(((s.get("slug") or "") + " " + (s.get("name") or "")).lower()
                             for s in stores)
            target = ("aliexpress" if "aliexpress" in sdesc
                      else "pcc" if "pccomponentes" in sdesc else None)
            if target:   # tracked non-Amazon store -> the transversal store tab
                ou = r.get("offer_url", "")
                if ou and resolved < 60:
                    try:
                        final = sess.get(ou, timeout=20, allow_redirects=True).url
                        resolved += 1
                    except requests.RequestException:
                        continue
                    want = r"aliexpress\." if target == "aliexpress" else r"pccomponentes\."
                    if re.search(want, final):
                        store_add(target, final, r.get("created_at") or "", "",
                                  _clean_name(r.get("name", "")))
                continue
            if not any(s.get("slug") == "amazon-es" or "amazon" in (s.get("name", "").lower()) for s in stores):
                continue   # Amazon.es only
            ou = r.get("offer_url", "")
            if not ou or resolved >= 60:
                continue
            try:  # the offer link is a shortener that redirects to the product
                final = sess.get(ou, timeout=20, allow_redirects=True).url
                resolved += 1
            except requests.RequestException:
                continue
            m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", final)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            out.append((f"https://www.amazon.es/dp/{m.group(1)}",
                        r.get("created_at") or "", "", False, _clean_name(r.get("name", ""))))
    log.info("mi: %d amazon.es deals", len(out))
    return out


def get_titas_items() -> list[tuple[str, str, str, bool, str]]:
    """Source is WordPress; its category pages are JS-rendered, so use the REST
    API. Each post's content has the Amazon ASIN (?asin=ASIN); title is the name.
    Returns (amazon_url, iso_date, "", low, name) — fetched via proxy, fallback direct."""
    out: list[tuple[str, str, str, bool, str]] = []
    seen = set()
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    proxy = SOURCES.get("titas_proxy", "")
    direct = SOURCES.get("titas_direct", "")
    if not proxy and not direct:
        return out
    for page in (1, 2):
        posts = None
        srcs = [s for s in (f"{proxy}?page={page}" if proxy else "",
                            f"{direct}{page}" if direct else "") if s]
        for src in srcs:
            try:
                r = sess.get(src, timeout=40)
                if r.status_code == 200:
                    posts = r.json()
                    if posts:
                        break
            except (requests.RequestException, ValueError) as e:
                log.warning("titas fetch %s: %s", src, e)
        if not posts:
            break
        for p in posts:
            content = (p.get("content", {}) or {}).get("rendered", "")
            store_scan_text(content,
                            (p.get("date_gmt") or "") + "+00:00" if p.get("date_gmt") else "",
                            _clean_name((p.get("title", {}) or {}).get("rendered", "")))
            m = re.search(r"[?&]asin=([A-Z0-9]{10})", content)
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            date = p.get("date_gmt") or p.get("date") or ""
            if date and not date.endswith(("Z", "+00:00")):
                date += "+00:00"
            name = _clean_name((p.get("title", {}) or {}).get("rendered", ""))
            out.append((f"https://www.amazon.es/dp/{m.group(1)}", date, extract_coupon_code(content), False, name))
    log.info("titas: %d amazon.es deals", len(out))
    return out


def get_terapia_items() -> list[tuple[str, str, str, bool, str]]:
    """WordPress source: each post carries an Amazon short link (amzn.to) and the
    post title is the product name. Read the REST API (newest first); the short
    link is expanded to the ASIN downstream. Returns (amazon_url, iso_date,
    coupon, low, name) tuples."""
    out: list[tuple[str, str, str, bool, str]] = []
    seen = set()
    # Endpoint lives in SOURCES_JSON["terapia_api"]; falls back to its own secret
    # so it can be added without rewriting the whole SOURCES_JSON blob.
    base = SOURCES.get("terapia_api", "") or os.environ.get("TERAPIA_API", "")
    if not base:
        return out
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    sep = "&" if "?" in base else "?"
    for page in (1, 2):
        try:
            r = sess.get(f"{base}{sep}page={page}", timeout=40)
            if r.status_code != 200:
                break
            posts = r.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("terapia fetch p%d: %s", page, e)
            break
        if not posts:
            break
        for p in posts:
            content = (p.get("content", {}) or {}).get("rendered", "")
            date = p.get("date_gmt") or p.get("date") or ""
            if date and not date.endswith(("Z", "+00:00")):
                date += "+00:00"
            name = _clean_name((p.get("title", {}) or {}).get("rendered", ""))
            store_scan_text(content, date, name)   # transversal store tabs
            urls = extract_amazon_urls(content)
            if not urls or urls[0] in seen:
                continue
            seen.add(urls[0])
            out.append((urls[0], date, extract_coupon_code(content), False, name))
    log.info("terapia: %d amazon deals", len(out))
    return out


def get_dib_items() -> list[tuple[str, str, str, bool, str]]:
    """Server-rendered deals site (Next.js). Each card on the homepage has a
    direct Amazon /dp/ASIN link, the product name in the image alt, and a
    /deal/<ms> id that is the publish timestamp. Returns
    (amazon_url, iso_date, coupon, low, name) tuples."""
    out: list[tuple[str, str, str, bool, str]] = []
    seen = set()
    url = os.environ.get("DIB_URL", "") or SOURCES.get("dib_url", "")
    if not url:
        return out
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    try:
        h = sess.get(url, timeout=40).text
    except requests.RequestException as e:
        log.error("dib: %s", e)
        return out
    store_scan_text(h)   # transversal AliExpress/PCC collector
    for m in re.finditer(r'href="https?://www\.amazon\.[a-z.]+/dp/([A-Z0-9]{10})[^"]*"', h):
        a = m.group(1)
        if a in seen:
            continue
        seen.add(a)
        card = h[max(0, m.start() - 1700):m.start()]
        alts = re.findall(r'alt="([^"]+)"', card)
        name = _clean_name(html.unescape(alts[-1])) if alts else ""
        dm = re.findall(r"/deal/(\d{13})", card)
        date = (datetime.fromtimestamp(int(dm[-1]) / 1000, timezone.utc).isoformat()
                if dm else "")
        coupon = extract_coupon_code(re.sub(r"<[^>]+>", " ", card))
        out.append((f"https://www.amazon.es/dp/{a}", date, coupon, False, name))
    log.info("dib: %d amazon.es deals", len(out))
    return out


# ---------------------------------------------------------------------------
# Bom tab: a coupon/discount site (WordPress). The REST API lists the newest
# offers (title + date; the category IS the store). The coupon code sits on
# each offer's page behind a reveal button (data-clipboard-text), and each
# store page carries the store's real website in JSON-LD — so rows can link
# STRAIGHT to the store, clean. Post/store lookups are cached in R2 so each
# page is fetched once, ever. The UI groups this tab by store, coupons first.
# ---------------------------------------------------------------------------
BOM_TAB_KEY = "data/bom.json"
BOM_MAP_KEY = "data/bom_map.json"
BOM_MAX_FETCH = int(os.environ.get("BOM_MAX_FETCH", "15"))


def write_bom_tab(cleared: set):
    base = (os.environ.get("BOM_URL", "") or SOURCES.get("bom_url", "")).rstrip("/")
    if not base:
        return
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    try:
        posts = sess.get(f"{base}/wp-json/wp/v2/posts?per_page=50", timeout=40).json()
    except (requests.RequestException, ValueError) as e:
        log.error("bom posts: %s", e)
        return
    if not isinstance(posts, list) or not posts:
        return

    state = r2_get_amazon_links(BOM_MAP_KEY)
    pmap: dict = state.get("p", {})   # post id -> {"c": code|"", "u": "dd/mm"}
    smap: dict = state.get("s", {})   # category id -> [store_name, store_site]

    # Store names for categories we haven't met yet (one batched request).
    need = sorted({str((p.get("categories") or [0])[0]) for p in posts} - set(smap))
    if need:
        try:
            cats = sess.get(f"{base}/wp-json/wp/v2/categories?include={','.join(need)}"
                            f"&per_page=100", timeout=40).json()
            for c in cats if isinstance(cats, list) else []:
                smap[str(c["id"])] = [c.get("name", ""), "", c.get("slug", "")]
        except (requests.RequestException, ValueError) as e:
            log.warning("bom categories: %s", e)

    fetched = 0
    # Coupon code + validity from each new offer page (cached forever). The
    # offer page also reveals its store-page URL (header data-url), which we
    # keep on the store entry — no site paths hardcoded here.
    for p in posts:
        pid = str(p.get("id"))
        if pid in pmap or fetched >= BOM_MAX_FETCH:
            continue
        fetched += 1
        entry = {"c": "", "u": ""}
        try:
            ph = sess.get(p.get("link", ""), timeout=30).text
            cm = re.search(r'data-code="([^"]+)"', ph)
            # "NO" is the site's sentinel for "no code needed" — not a coupon.
            if cm and html.unescape(cm.group(1)).strip().upper() not in ("NO", "N/A"):
                entry["c"] = html.unescape(cm.group(1)).strip()
            vm = re.search(r"v[aá]lido at[eé] (\d{2}/\d{2})/\d{4}", ph)
            if vm:
                entry["u"] = vm.group(1)
            um = re.search(r'class="href" data-url="(https?://[^"]+)"', ph)
            cid = str((p.get("categories") or [0])[0])
            se = smap.get(cid)
            if um and se is not None and len(se) >= 3 and not se[1]:
                if len(se) == 3:
                    se.append("")
                se[3] = um.group(1)          # store page, discovered not built
        except requests.RequestException as e:
            log.warning("bom post %s: %s", pid, e)
        pmap[pid] = entry

    # Store website (clean outbound link) from each store page's JSON-LD.
    for cid, entry in smap.items():
        if len(entry) >= 4 and entry[3] and not entry[1] and fetched < BOM_MAX_FETCH:
            fetched += 1
            try:
                sh = sess.get(entry[3], timeout=30).text
                m = re.search(r'"sameAs":\s*"(https?://[^"]+)"', sh)
                if m:
                    entry[1] = m.group(1).rstrip("/")
            except requests.RequestException as e:
                log.warning("bom store %s: %s", entry[0], e)

    # Affiliate NETWORK behind each store: the first redirect hop of the site's
    # /go/<slug> outbound reveals the platform (Awin, TradeTracker, CJ, ...).
    # One probe per store, no redirects followed, cached forever.
    NETS = ((r"awin1\.|zenaps", "Awin"), (r"tradetracker", "TradeTracker"),
            (r"anrdoezrs|dpbolvw|jdoqocy|kqzyfj|tkqlhce|dotomi|emjcd", "CJ"),
            (r"tradedoubler", "TradeDoubler"), (r"webgains", "Webgains"),
            (r"linksynergy|rakuten", "Rakuten"), (r"metaffiliation|kwanko", "Kwanko"),
            (r"effiliation", "Effiliation"), (r"daisycon", "Daisycon"),
            (r"belboon", "Belboon"), (r"\.sjv\.io|\.pxf\.io|impact\.com", "Impact"),
            (r"prf\.hn|partnerize", "Partnerize"), (r"admitad", "Admitad"),
            (r"timeone", "TimeOne"), (r"aliexpress", "AliExpress"),
            (r"amazon\.|amzn\.", "Amazon"))
    for cid, entry in smap.items():
        while len(entry) < 5:
            entry.append("")
        if entry[2] and not entry[4] and fetched < BOM_MAX_FETCH:
            fetched += 1
            try:
                r0 = sess.get(f"{base}/go/{entry[2]}", timeout=25, allow_redirects=False)
                host = re.sub(r"^https?://", "", r0.headers.get("Location", "")).split("/")[0].lower()
                entry[4] = (next((n for pat, n in NETS if re.search(pat, host)), "")
                            or ("direto" if host else ""))
            except requests.RequestException as e:
                log.warning("bom net %s: %s", entry[0], e)

    links = []
    for p in posts:
        pid = str(p.get("id"))
        cid = str((p.get("categories") or [0])[0])
        store, site = (smap.get(cid) or ["", ""])[:2]
        date = p.get("date_gmt") or p.get("date") or ""
        if date and not date.endswith(("Z", "+00:00")):
            date += "+00:00"
        # Clean link straight to the store; #fragment keeps it unique per deal
        # (so hide-on-open hides one offer, not the whole store).
        url = f"{site}#bd{pid}" if site else p.get("link", "")
        e = pmap.get(pid) or {}
        l = {"name": _clean_name(html.unescape(re.sub(r"<[^>]+>", "",
                     (p.get("title") or {}).get("rendered", "")))),
             "url": url, "date": date, "store": store or "Outras"}
        se = smap.get(cid) or []
        if len(se) >= 5 and se[4] and se[4] != "direto":
            l["net"] = se[4]       # affiliate platform, for reference
        if e.get("c"):
            l["coupon"] = e["c"]
        if e.get("u"):
            l["val"] = e["u"]      # "dd/mm" validity
        if l["name"] and url and url not in cleared:
            links.append(l)
    links.sort(key=lambda l: l.get("date", ""), reverse=True)

    # Keep the caches bounded to what still matters.
    if len(pmap) > 600:
        keep = {str(p.get("id")) for p in posts}
        pmap = {k: v for k, v in pmap.items() if k in keep or len(pmap) <= 600}
        pmap = dict(list(pmap.items())[-600:])
    r2_put_amazon_links({"p": pmap, "s": smap}, BOM_MAP_KEY)
    r2_put_amazon_links({"updated": datetime.now(timezone.utc).isoformat(),
                         "links": links}, BOM_TAB_KEY)
    log.info("[%s] bom tab: %d offers, %d with coupon (%d pages fetched)",
             BOM_TAB_KEY, len(links), sum(1 for l in links if l.get("coupon")), fetched)


def get_camel_items() -> list[tuple[str, str, str, bool, str]]:
    """Deals from a price-tracker's RSS feeds (highlights + popular + top drops;
    the feeds escape the site's bot challenge). Each item's /product/<ASIN> link
    carries the ASIN and the <title> is the product name, deduped across feeds.
    Returns (amazon_url, iso_date, "", low, name) tuples."""
    out: list[tuple[str, str, str, bool, str]] = []
    feeds = SOURCES.get("camel_feeds", [])
    if not feeds:
        return out
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    seen = set()
    for feed in feeds:
        try:
            x = sess.get(feed, timeout=40).text
        except requests.RequestException as e:
            log.warning("camel feed %s: %s", feed, e)
            continue
        for it in re.findall(r"<item>(.*?)</item>", x, re.S):
            ln = re.search(r"<link>(.*?)</link>", it, re.S)
            # Only real product ASINs (start with B); skips ISBN-format books.
            m = re.search(r"/product/(B[A-Z0-9]{9})(?:[?/<]|$)", ln.group(1)) if ln else None
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            tt = re.search(r"<title>(.*?)</title>", it, re.S)
            name = _clean_name(re.sub(r"<.*?>", "", tt.group(1))) if tt else ""
            pd = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
            date = ""
            if pd:
                try:  # RFC822 -> zoned ISO so it sorts chronologically
                    date = email.utils.parsedate_to_datetime(pd.group(1).strip()).astimezone(timezone.utc).isoformat()
                except Exception:
                    date = ""
            out.append((f"https://www.amazon.es/dp/{m.group(1)}", date, "", False, name))
    log.info("camel: %d deals from %d feeds", len(out), len(feeds))
    return out


def scrape_amazon_links():
    """Scan each configured Telegram list into its own JSON, then refresh the page.
    Telegram tab -> data/amazon_links.json ; Descontos tab -> data/descontos.json."""
    log.info("=== Amazon link scan at %s ===", datetime.now().isoformat())
    cleared = r2_get_amazon_cleared()  # admin-cleared / opened URLs, excluded from all lists
    # Links used to carry ?tag=...; hidden/cleared entries recorded back then must
    # keep matching now that URLs are clean. Additive, so store-tab URLs (which
    # legitimately have query strings) are untouched.
    for u in [u for u in cleared if "/dp/" in u and "?" in u]:
        cleared.add(u.split("?", 1)[0])
    for v in _STORE_ITEMS.values():    # fresh transversal store collector this run
        v.clear()

    # Cross-check EVERY list against the reference source on every run. Any
    # product seen there (ASIN or Worten URL) is banned while inside its 12h
    # freshness window (the stamp refreshes each scan it stays fresh). After
    # the window passes, a source re-publishing it with a newer date brings it
    # back to the lists.
    _sess = requests.Session()
    _sess.headers.update(_BROWSER_HEADERS)
    cupo_recent, cupo_worten = reference_recent_asins(_sess, hours=REFERENCE_WINDOW_HOURS)
    now_iso = datetime.now(timezone.utc).isoformat()
    BANNED_KEY = "data/cupo_banned.json"
    banned = r2_get_amazon_links(BANNED_KEY).get("b", {})   # {asin_or_worten_url: ban_iso}
    for a in cupo_recent | cupo_worten:                      # Worten deals banned by clean URL
        banned[a] = now_iso                                  # (re)stamp the ban as of now

    # Shared ASIN -> product name map. Sources that expose titles (DEZ/NAS/TITAS/
    # Chollo, and any slug) fill it; bare /dp/ASIN links on other tabs reuse it,
    # so the same deal shows a real name everywhere. Persisted across runs.
    NAMES_KEY = "data/names_by_asin.json"
    _nm = r2_get_amazon_links(NAMES_KEY)
    # Scrub poisoned entries (name == ASIN counts as no name).
    name_map = {a: n for a, n in _nm.get("n", {}).items() if n and n != a}
    keepa_tried = set(_nm.get("tried", []))   # ASINs already looked up on Keepa

    # Transversal all-time-low cache (per ASIN, 24h TTL), shared by every tab.
    # Priced via Keepa BEFORE each tab is written, so the dot is right on first
    # appearance. price_budget is the shared per-run token cap across all tabs.
    LOW_KEY = "data/keepa.json"
    low_cache = r2_get_amazon_links(LOW_KEY).get("k", {})
    all_asins = set()           # every ASIN shown this run -> to bound the cache
    price_budget = [KEEPA_MAX_PRICE_PER_RUN]

    # Deals on several lists are NOT removed anymore: every tab keeps its copy
    # and the UI colors the row, stronger the more lists carry it. `results`
    # holds each tab's written state so multiplicity is stamped after the loop.
    results = {}

    last = None
    TAB_ROWS = [
        (AMAZON_TELEGRAM_CHANNELS, AMAZON_WEB_PAGES, None, "data/amazon_links.json"),
        (SOURCES.get("descontos_channels", []), [], None, "data/descontos.json"),
        ([], SOURCES.get("deluxe_pages", []), None, "data/deluxe.json"),
        ([], [], get_chollo_items, "data/chollo.json"),
        ([], [], lambda: get_dez_items(set()), "data/dez.json"),
        (SOURCES.get("nas_channels", []), [], None, "data/nas.json"),
        ([], [], get_mi_items, "data/mi.json"),
        ([], SOURCES.get("cholloes_pages", []), None, "data/cholloes.json"),
        ([], [], get_camel_items, "data/camel.json"),
        ([], [], get_titas_items, "data/titas.json"),
        ([], [], get_terapia_items, "data/terapia.json"),
        ([], [], get_dib_items, "data/dib.json"),
    ]
    for i, (channels, web_pages, items_fn, state_key) in enumerate(TAB_ROWS):
        # The reference-source sticky-ban is applied via `banned`.
        exclude = None
        by_date = state_key in ("data/deluxe.json", "data/chollo.json",
                                "data/dez.json", "data/nas.json", "data/mi.json",
                                "data/cholloes.json", "data/camel.json", "data/titas.json",
                                "data/terapia.json", "data/dib.json")
        # TITAS: only top-1000 most-popular AND at all-time low.
        top_rank = 1000 if state_key == "data/titas.json" else None
        # Fair share of the Keepa token budget: a tab may spend at most its slice
        # of what is still left, so a hungry early tab (e.g. one scanning
        # hundreds of candidates) can't starve the last ones. Whatever a tab
        # does not use stays in the pot for the tabs after it.
        fair = None
        if price_budget:
            tabs_left = len(TAB_ROWS) - i
            fair = [max(1, price_budget[0] // tabs_left)]
            slice_cap = fair[0]
        last = scan_amazon_list(channels, web_pages, state_key, cleared, items_fn,
                                exclude, by_date, name_map, keepa_tried, low_cache, all_asins,
                                fair, banned, top_rank)
        if price_budget and fair is not None:
            price_budget[0] -= slice_cap - fair[0]   # only what this tab spent
        results[state_key] = last

    # Stamp cross-tab multiplicity: x = number of lists carrying the ASIN this
    # run. Only rows with x >= 2 carry the field; the UI tints them (stronger
    # with more lists). Tabs whose links changed are re-written.
    count: dict = {}
    for state in results.values():
        for a in {_asin_from_url(l["url"]) for l in state.get("links", [])} - {""}:
            count[a] = count.get(a, 0) + 1
    for state_key, state in results.items():
        changed = False
        for l in state.get("links", []):
            x = count.get(_asin_from_url(l["url"]), 1)
            if x >= 2 and l.get("x") != x:
                l["x"] = x
                changed = True
            elif x < 2 and l.pop("x", None) is not None:
                changed = True
        if changed:
            r2_put_amazon_links(state, state_key)

    # Persist the all-time-low cache (already refreshed per-tab before writing),
    # bounded to the ASINs still shown.
    low_cache = {a: low_cache[a] for a in all_asins if a in low_cache}
    r2_put_amazon_links({"k": low_cache}, LOW_KEY)

    # Persist the sticky reference-source ban map (bounded to most recent bans).
    if len(banned) > 100000:
        banned = dict(sorted(banned.items(), key=lambda kv: kv[1])[-100000:])
    r2_put_amazon_links({"b": banned}, BANNED_KEY)

    # Persist the shared ASIN -> name map + the Keepa "already tried" set (bounded).
    if len(name_map) > 80000:
        name_map = dict(list(name_map.items())[-80000:])
    r2_put_amazon_links({"n": name_map, "tried": sorted(keepa_tried)[-100000:]}, NAMES_KEY)

    # Non-Amazon store tabs, fed transversally by every source above.
    write_store_tabs(cleared)

    # Bom tab: coupon/discount site, grouped by store in the UI.
    write_bom_tab(cleared)

    r2_upload_amazon_html()
    return last


def _asin_from_url(u: str) -> str:
    m = re.search(r"/dp/([A-Z0-9]{10})", u or "")
    return m.group(1) if m else ""


def _parse_dt(s):
    """Parse an ISO timestamp (handles trailing Z); return aware datetime or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _dt_after(a, b) -> bool:
    """True if timestamp a is strictly newer than b (both parseable)."""
    da, db = _parse_dt(a), _parse_dt(b)
    return bool(da and db and da > db)


def scan_amazon_list(channels, web_pages, state_key, cleared, items_fn=None, exclude_asins=None, sort_by_date=False, name_map=None, keepa_tried=None, low_cache=None, all_asins=None, price_budget=None, banned=None, top_rank=None):
    """Build the freshest batch of clean affiliate links for one source list."""
    existing = r2_get_amazon_links(state_key)
    # Resolution cache: raw short/long URL -> resolved dict (avoids re-expanding).
    cache = existing.get("cache", {})
    # Persistent real-title cache: link id -> product name (filled over time).
    # A value equal to the id's ASIN is a poisoned entry, not a name.
    names = {k: v for k, v in existing.get("names", {}).items()
             if v and v != k.split("-")[0]}
    # First time we saw each URL (used as the date for web sources like Deluxe,
    # whose pages report "now" as the publish date).
    seen = existing.get("seen", {})

    # Collect posts that contain Amazon links, with their publish time, so we can
    # keep only the most recent batch (posts within N hours of the newest one).
    raw_to_coupon: dict[str, str] = {}   # raw url -> checkout coupon code (if any)
    # Worten deals from the same sources are kept IN this tab's list too,
    # marked differently in the UI (no ASIN -> no Keepa; the row opens the
    # link). Query strings are stripped so the links stay clean.
    WORTEN_RE = re.compile(r"https?://(?:www\.)?worten\.pt/[^\s\"'<>\\]+", re.I)
    wt_rows: list[dict] = []
    wt_seen = set()

    def _wt_collect(text, date="", name=""):
        for u in WORTEN_RE.findall(text or ""):
            u = re.sub(r"[?#].*$", "", html.unescape(u)).rstrip(").,;/")
            path = u.split("/", 3)[-1] if u.count("/") >= 3 else ""
            # product pages carry a numeric id in the path; skip category/home
            if len(path) < 8 or not re.search(r"\d", path) or u in wt_seen:
                continue
            wt_seen.add(u)
            seg = [s for s in path.split("/") if s]
            slug = re.sub(r"[-_]+", " ", seg[-1]).strip() if seg else ""
            wt_rows.append({"url": u, "date": date, "wt": 1,
                            "name": name or (_clean_name(slug) if len(slug) > 3 else "Produto Worten"),
                            "coupon": extract_coupon_code(text)})

    posts: list[tuple[datetime | None, list[str]]] = []
    for channel in channels:
        for post in get_telegram_link_posts(channel):
            # Tracked non-Amazon stores go to their own transversal tabs.
            store_scan_text(post["html"], post["dt"].isoformat() if post["dt"] else "",
                            _post_text_name(post["html"]))
            _wt_collect(post["html"], post["dt"].isoformat() if post["dt"] else "",
                        _post_text_name(post["html"]))
            urls = extract_amazon_urls(post["html"])
            if urls:
                posts.append((post["dt"], urls))
                cpn = extract_coupon_code(post["html"])   # code is in the post body
                if cpn:
                    for u in urls:
                        raw_to_coupon.setdefault(u, cpn)

    dated = [dt for dt, _ in posts if dt]
    window_start = (max(dated) - timedelta(hours=AMAZON_BATCH_WINDOW_HOURS)) if dated else None

    # Ordered, de-duplicated raw URLs from the latest batch only.
    candidates: list[str] = []
    seen_raw = set()
    raw_to_date: dict[str, str] = {}  # raw url -> post publish date (ISO)
    for dt, urls in posts:
        if window_start is not None and (dt is None or dt < window_start):
            continue
        for raw in urls:
            if raw not in seen_raw:
                seen_raw.add(raw)
                candidates.append(raw)
                raw_to_date[raw] = dt.isoformat() if dt else ""
    # Web pages are always included (no batching concept there).
    for page in web_pages:
        try:
            resp = requests.get(page, timeout=30, headers=_BROWSER_HEADERS)
            resp.raise_for_status()
            store_scan_text(resp.text)   # tracked stores -> their own tabs
            _wt_collect(resp.text)       # Worten -> kept in this tab, marked
            for raw in extract_amazon_urls(resp.text):
                if raw not in seen_raw:
                    seen_raw.add(raw)
                    candidates.append(raw)
                    raw_to_date[raw] = ""
        except requests.RequestException as e:
            log.error("Failed to fetch web page %s: %s", page, e)
    # Custom provider returns (url, date, coupon[, low]) tuples (chollo/dez/titas).
    raw_to_low: dict[str, bool] = {}
    raw_to_name: dict[str, str] = {}   # name supplied by the source (for /dp/ASIN links)
    if items_fn:
        try:
            for item in items_fn():
                raw, date, coupon = item[0], item[1], item[2]
                low = item[3] if len(item) > 3 else False
                iname = item[4] if len(item) > 4 else ""
                if raw not in seen_raw:
                    seen_raw.add(raw)
                    candidates.append(raw)
                    raw_to_date[raw] = date or ""
                    if coupon:
                        raw_to_coupon[raw] = coupon
                    if low:
                        raw_to_low[raw] = True
                    if iname:
                        raw_to_name[raw] = iname
        except Exception as e:
            log.error("items provider failed for %s: %s", state_key, e)

    log.info("[%s] latest batch: %d unique raw Amazon URLs (%d with coupon code)",
             state_key, len(candidates), len(raw_to_coupon))

    now_iso = datetime.now(timezone.utc).isoformat()
    resolved_this_run = 0
    deferred = 0
    titles_this_run = 0
    links: list[dict] = []
    seen_ids = set()

    for raw_url in candidates:
        # Use cached resolution if we've expanded this URL before.
        if raw_url in cache:
            resolved = cache[raw_url]
        else:
            if resolved_this_run >= AMAZON_MAX_RESOLVE_PER_RUN:
                deferred += 1
                continue  # picked up on a later run
            resolved = resolve_amazon_link(raw_url)
            resolved_this_run += 1
            cache[raw_url] = resolved  # cache hits and misses (None)
        if resolved is None:  # not a product link
            continue
        # Self-heal: a name that is just the ASIN counts as no name at all
        # (repairs cached entries poisoned by the old slug fallback too).
        if resolved.get("name", "") == resolved.get("asin"):
            resolved["name"] = f"Produto {resolved['asin']}"
        # Self-heal: strip the affiliate tag from resolutions cached before
        # links went clean.
        if "?" in (resolved.get("affiliate_url") or ""):
            resolved["affiliate_url"] = resolved["affiliate_url"].split("?", 1)[0]
        if resolved["affiliate_url"] in cleared:  # admin cleared this one
            continue
        if exclude_asins and resolved["asin"] in exclude_asins:
            continue   # already on an earlier tab this run — keep tabs unique
        # Reference-source ban with a 12h window: the stamp keeps refreshing
        # while the product stays fresh on the reference source; once that
        # window passes, a source publishing it with a NEWER date brings it
        # back to the lists.
        if banned is not None and resolved["asin"] in banned:
            src_date = raw_to_date.get(raw_url) or seen.get(resolved["affiliate_url"], "")
            if not _dt_after(src_date, banned[resolved["asin"]]):
                continue                               # still inside the ban
            banned.pop(resolved["asin"], None)         # re-published later -> back

        lid = resolved["id"]
        if lid in seen_ids:
            continue
        seen_ids.add(lid)

        name = resolved["name"]
        asin = resolved["asin"]
        # If the URL carried no slug, the name is "Produto <ASIN>". Prefer (1) a
        # real title we already cached, (2) the name the source gave us, (3) the
        # shared ASIN->name map (same deal seen on a named source), (4) a direct
        # Amazon fetch (off by default — Amazon blocks automated IPs).
        if name.startswith("Produto "):
            if names.get(lid):
                name = names[lid]
            elif raw_to_name.get(raw_url):
                name = raw_to_name[raw_url]
                names[lid] = name
            elif name_map and name_map.get(asin):
                name = name_map[asin]
            elif AMAZON_FETCH_TITLES and titles_this_run < AMAZON_MAX_TITLE_FETCH_PER_RUN:
                titles_this_run += 1
                fetched = fetch_amazon_title(resolved["clean_url"])
                if fetched:
                    names[lid] = fetched
                    name = fetched
        # Feed any real product name back into the shared map for other tabs.
        if name_map is not None and asin and name != asin and not name.startswith("Produto "):
            name_map.setdefault(asin, name)
        if all_asins is not None and asin:
            all_asins.add(asin)   # for the transversal all-time-low refresh
        # Coupon code: structured/Telegram-body first, else always scan the product
        # name itself (cupão/código/cupón/coupon + code) so no source is missed.
        # Stored as its own field -> the UI shows a click-to-copy chip.
        coupon = raw_to_coupon.get(raw_url, "") or extract_coupon_code(name)
        url = resolved["affiliate_url"]
        link_date = raw_to_date.get(raw_url, "")   # Telegram post date when available
        if not link_date:                          # web source: use first-seen date
            link_date = seen.get(url) or now_iso
            seen[url] = link_date
        link = {"name": name, "url": url, "date": link_date}
        if coupon:
            link["coupon"] = coupon
        if raw_to_low.get(raw_url):
            link["low"] = True   # provider already says it's an all-time low
        links.append(link)

    # Worten rows join this tab's list (marked; no ASIN so Keepa skips them).
    for r in wt_rows:
        if r["url"] in cleared:
            continue
        if not r["date"]:                       # web source: first-seen date
            r["date"] = seen.get(r["url"]) or now_iso
            seen[r["url"]] = r["date"]
        # Same windowed reference-source ban as ASINs, keyed by the clean URL.
        if banned is not None and r["url"] in banned:
            if not _dt_after(r["date"], banned[r["url"]]):
                continue
            banned.pop(r["url"], None)
        if not r.get("coupon"):
            r.pop("coupon", None)
        links.append(r)
    if wt_rows:
        log.info("[%s] +%d worten rows", state_key, len(wt_rows))

    # All-time-low (Keepa) BEFORE writing: price this tab's ASINs that aren't
    # fresh in the cache (shared per-run budget protects tokens), so the dot is
    # correct the moment the deal appears — no one-run delay.
    if low_cache is not None and KEEPA_API_KEY:
        cap = price_budget[0] if price_budget else KEEPA_MAX_PRICE_PER_RUN
        used = keepa_low_refresh([_asin_from_url(l["url"]) for l in links], low_cache, cap)
        if price_budget:
            price_budget[0] -= used
        for l in links:
            e = low_cache.get(_asin_from_url(l["url"])) or {}
            if e.get("i"):
                l["img"] = e["i"]   # CDN filename; the browser builds the URL
            if e.get("low"):
                l["low"] = True
                if e.get("min"):
                    l["minp"] = round(e["min"] / 100, 2)   # historical min in €
                    l["minlbl"] = e.get("lbl", "")          # which Keepa series
        # Drop books once Keepa has told us the category (root category in BOOK_CATS).
        if BOOK_CATS:
            links = [l for l in links
                     if (low_cache.get(_asin_from_url(l["url"])) or {}).get("cat") not in BOOK_CATS]
        # Curated tab: keep only all-time lows that are also in the top-N most
        # popular (Keepa sales rank <= top_rank). Needs the ASIN to be priced.
        if top_rank:
            kept = []
            for l in links:
                e = (low_cache or {}).get(_asin_from_url(l["url"])) or {}
                rk = e.get("rank")
                if e.get("low") and isinstance(rk, int) and 0 < rk <= top_rank:
                    kept.append(l)
            links = kept

    # Last resort: fill any still-nameless link from Keepa (paid). Each ASIN is
    # queried at most once ever (tracked in keepa_tried) and capped per run, so
    # tokens are never blown. Found titles go into the shared map (free reuse).
    bare = [l for l in links if l["name"].startswith("Produto ")]
    if bare and KEEPA_API_KEY:
        tried = keepa_tried if keepa_tried is not None else set()
        need = sorted({_asin_from_url(l["url"]) for l in bare} - set(name_map or {}) - tried)
        need = need[:KEEPA_MAX_TITLES_PER_LIST]   # cap new lookups per run
        if need:
            titles, queried = keepa_titles(need)
            tried |= queried                      # never re-query (found or not)
            for a, t in titles.items():
                if name_map is not None:
                    name_map[a] = t
        for l in bare:
            t = (name_map or {}).get(_asin_from_url(l["url"]))
            if t:
                l["name"] = t

    # Keep the caches from growing forever: drop entries no longer shown.
    names = {lid: n for lid, n in names.items() if lid in seen_ids}
    cur_urls = {l["url"] for l in links}
    seen = {u: d for u, d in seen.items() if u in cur_urls}

    if sort_by_date:
        # Newest first (ISO dates sort chronologically; missing dates go last).
        links.sort(key=lambda l: l.get("date", ""), reverse=True)
    else:
        # Reorder so the list doesn't mirror the channel's order. Deterministic
        # (hash of the URL) so it stays stable across the 10-min refreshes.
        links.sort(key=lambda l: hashlib.md5(l["url"].encode("utf-8")).hexdigest())

    data = {"updated": now_iso, "links": links, "cache": cache, "names": names, "seen": seen}
    r2_put_amazon_links(data, state_key)
    log.info("[%s] complete: %d links, resolved %d new, deferred %d, titles %d",
             state_key, len(links), resolved_this_run, deferred, titles_this_run)
    return data


def r2_upload_amazon_html():
    """Upload the Amazon dashboard page to R2."""
    html_content = generate_amazon_html()
    if not CF_API_TOKEN:
        with open(Path(__file__).parent / "amazon.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        return
    key = "index.html"  # root of the dedicated amazon worker/bucket
    url = f"{AMAZON_API_BASE}/objects/{quote(key, safe='')}"
    headers = {**r2_headers(), "Content-Type": "text/html; charset=utf-8"}
    try:
        resp = requests.put(url, headers=headers, data=html_content.encode("utf-8"), timeout=30)
        if resp.status_code == 200:
            log.info("Uploaded index.html to amazon bucket")
        else:
            log.warning("Failed to upload amazon.html: %s", resp.text[:200])
    except Exception as e:
        log.warning("Failed to upload amazon.html: %s", e)


def generate_amazon_html() -> str:
    """Tabbed dashboard, one live tab per source, with a universal search bar.
    Same clickable row format; tap a row to open the affiliate link (and hide it)."""
    return """<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Links Amazon</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>\U0001F6D2</text></svg>">
<style>
  :root { --bg:#121212; --row:#1a1a1a; --row-hover:#222; --border:#2a2a2a;
    --text:#ededed; --muted:#8a9099; --brand:#ff9900; --red:#ef4444; --green:#25d366; }
  * { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:var(--bg); color:var(--text); }
  header { position:sticky; top:0; z-index:5; background:var(--bg);
    border-bottom:1px solid var(--border); padding:10px 14px; }
  .top { display:flex; align-items:center; gap:8px; }
  header h1 { font-size:16px; font-weight:700; }
  header .meta { color:var(--muted); font-size:12px; margin-left:auto; }
  .clear-btn { background:transparent; border:1px solid var(--border); color:var(--muted);
    font-size:12px; padding:5px 10px; border-radius:8px; cursor:pointer; }
  .clear-btn:hover { border-color:var(--red); color:var(--red); }
  .sort-btn { background:transparent; border:1px solid var(--border); color:var(--muted);
    font-size:12px; padding:5px 10px; border-radius:8px; cursor:pointer; margin-right:6px; }
  .sort-btn:hover { border-color:var(--brand); color:var(--brand); }
  .sort-btn.active { border-color:var(--brand); color:var(--brand); font-weight:700; }
  .tabs { display:flex; gap:6px; margin-top:10px; overflow-x:auto; }
  .tab { background:var(--row); border:1px solid var(--border); color:var(--text);
    padding:7px 12px; border-radius:999px; cursor:pointer; font-size:13px; white-space:nowrap; }
  .tab.active { background:var(--brand); border-color:var(--brand); color:#1a1a1a; font-weight:700; }
  .tab.has-new::after { content:""; display:inline-block; width:8px; height:8px;
    border-radius:50%; background:var(--green); margin-left:6px; vertical-align:middle; }
  .search { width:100%; margin-top:10px; padding:9px 12px; border-radius:8px;
    border:1px solid var(--border); background:var(--row); color:var(--text); font-size:14px; }
  ul { list-style:none; max-width:760px; margin:0 auto; }
  li a { display:flex; align-items:center; gap:10px; padding:11px 16px;
    border-bottom:1px solid var(--border); color:var(--text); text-decoration:none;
    background:var(--row); font-size:14px; line-height:1.3; }
  li a:active, li a:hover { background:var(--row-hover); }
  li .name { flex:1; word-break:break-word; }
  li .tag { color:var(--muted); font-size:11px; flex-shrink:0; white-space:nowrap; }
  li .tag.disc { color:var(--brand); font-weight:700; }
  li .cpn { flex-shrink:0; margin-left:8px; border:1px solid var(--brand); color:var(--brand);
    background:transparent; border-radius:6px; padding:2px 8px; font-size:11px; font-weight:700;
    cursor:pointer; white-space:nowrap; font-family:inherit; line-height:1.6; }
  li .cpn:hover { background:rgba(255,153,0,.14); }
  li .cpn.ok { border-color:var(--green); color:var(--green); }
  li .tag.srctab { border:1px solid var(--border); border-radius:6px; padding:1px 7px;
    margin-left:8px; color:var(--muted); font-weight:600; }
  li .thumb { width:38px; height:38px; object-fit:contain; flex-shrink:0;
    margin-right:10px; border-radius:6px; background:#fff; }
  /* Grouped/compact mode (Bom): store header rows + tighter items */
  li.grp { display:flex; justify-content:space-between; align-items:baseline;
    padding:12px 16px 3px; font-size:11px; text-transform:uppercase;
    letter-spacing:.5px; color:var(--brand); font-weight:800; }
  li.grp .grp-n { color:var(--muted); font-weight:600; text-transform:none; letter-spacing:0; }
  li .stref { color:var(--brand); font-weight:700; margin-right:7px; }
  li .stref::after { content:"·"; color:var(--muted); margin-left:7px; font-weight:400; }
  /* Worten rows: red edge + badge, clearly distinct from Amazon rows */
  li a.wt { box-shadow: inset 3px 0 0 #e11d2e; }
  li .tag.wtag { border:1px solid #e11d2e; color:#ff5a68; border-radius:6px;
    padding:1px 7px; margin-left:8px; font-weight:700; }
  /* Compact rows are two lines: title on top, meta (coupon · validity ·
     publish date) below — so the date is never squeezed off-screen. */
  ul.compact li a { padding:7px 16px; font-size:13px; flex-wrap:wrap; row-gap:3px; }
  ul.compact .name { flex:1 1 100%; overflow:hidden; display:-webkit-box;
    -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  ul.compact .cpn { margin-left:0; }
  ul.compact .arrow { display:none; }
  li .arrow { color:var(--brand); font-size:13px; font-weight:700; flex-shrink:0; }
  .low-dot { display:inline-block; width:9px; height:9px; border-radius:50%;
    background:#f5b50a; margin-right:7px; vertical-align:middle; flex-shrink:0;
    box-shadow:0 0 6px rgba(245,181,10,.7); }
  li a.visited { background:rgba(37,211,102,0.10); }
  li a.visited .name { color:var(--green); }
  li a.visited .arrow { color:var(--green); }
  .more { display:block; width:100%; max-width:760px; margin:14px auto; padding:12px;
    background:var(--row); border:1px solid var(--border); color:var(--text);
    border-radius:10px; font-size:14px; cursor:pointer; }
  .empty { text-align:center; color:var(--muted); padding:50px 20px; font-size:14px; }
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>\U0001F6D2 Links Amazon</h1>
    <span class="meta" id="meta"></span>
    <button class="sort-btn" id="sortBtn" title="Ordenar por data" style="display:none">Por data</button>
    <button class="clear-btn" id="clearBtn" title="Limpar esta lista">Limpar tudo</button>
  </div>
  <div class="tabs" id="tabs"></div>
  <input class="search" id="search" placeholder="Pesquisar em todas as tabs...">
</header>
<ul id="list"></ul>
<button class="more" id="moreBtn" style="display:none">Mostrar mais</button>
<script>
const TABS = [
  { id:"tg",   label:"Telegram",     src:"/data/amazon_links.json", kind:"tg" },
  { id:"desc", label:"Descontos",    src:"/data/descontos.json",    kind:"tg" },
  { id:"deluxe", label:"Deluxe",     src:"/data/deluxe.json",       kind:"tg" },
  { id:"chollo", label:"Chollo",     src:"/data/chollo.json",       kind:"tg" },
  { id:"dez",    label:"DEZ",        src:"/data/dez.json",          kind:"tg" },
  { id:"nas",    label:"NAS",        src:"/data/nas.json",          kind:"tg" },
  { id:"mi",     label:"Mi",         src:"/data/mi.json",           kind:"tg" },
  { id:"cholloes", label:"cholloes", src:"/data/cholloes.json",     kind:"tg" },
  { id:"camel",  label:"Camel",      src:"/data/camel.json",        kind:"tg" },
  { id:"titas",  label:"TITAS",      src:"/data/titas.json",        kind:"tg" },
  { id:"terapia", label:"Terapia",   src:"/data/terapia.json",      kind:"tg" },
  { id:"dib",    label:"Dib",        src:"/data/dib.json",          kind:"tg" },
  { id:"bom",    label:"Bom",        src:"/data/bom.json",          kind:"tg", group:true },
  { id:"alix",   label:"AliExpress", src:"/data/aliexpress.json",   kind:"tg" },
  { id:"pcc",    label:"PCComponentes", src:"/data/pccomponentes.json", kind:"tg" },
];
const PAGE = 200;
let current = TABS[0];
let shown = PAGE;
let query = "";
let sortByDate = localStorage.getItem("amzSortTg") === "1";  // Telegram: sort by date
const cache = {};   // id -> normalized [{name,url,extra,disc}]
const NEW = {};     // id -> has unseen links (green dot)
let serverHidden = new Set();   // URLs hidden server-side (synced across all devices)

function esc(s){ return (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function lsGet(k){ try { return new Set(JSON.parse(localStorage.getItem(k)||"[]")); } catch(e){ return new Set(); } }
function lsAdd(k, urls){ const s=lsGet(k); urls.forEach(u=>s.add(u)); localStorage.setItem(k, JSON.stringify([...s])); }
// Static tabs: opened links stay but turn green (shared key).
function visitedSet(){ return lsGet("amzOpened"); }
function markVisited(url){ lsAdd("amzOpened", [url]); }
// Hidden links, per tab (Telegram-style hide + "Limpar tudo" on any tab).
function hiddenSet(id){ return lsGet("amzHidden_"+id); }
// Links already seen on a tab (used to decide the green "new" dot), per tab.
function knownSet(id){ return lsGet("amzKnown_"+id); }
function fmtDate(iso){ if(!iso) return ""; const d=new Date(iso); if(isNaN(d)) return ""; const p=n=>String(n).padStart(2,"0"); return p(d.getDate())+"/"+p(d.getMonth()+1)+" "+p(d.getHours())+":"+p(d.getMinutes()); }

function normalize(tab, raw){
  if (tab.kind === "tg") {
    return (raw.links||[]).map(l => ({ name:l.name, url:l.url, date:l.date||"", extra:fmtDate(l.date), disc:false, low:!!l.low, minp:l.minp, minlbl:l.minlbl, coupon:l.coupon||"", img:l.img||"", store:l.store||"", val:l.val||"", net:l.net||"", x:l.x||1, wt:!!l.wt }));
  }
  return (raw||[]).map(l => ({
    name:l.name, url:l.url,
    extra: (l.disc ? ("-"+l.disc+"%") : (l.mkt||"")),
    disc: !!l.disc, low:!!l.low,
  }));
}
function urlsOf(id){ return (cache[id]||[]).map(l => l.url); }

async function fetchTab(tab){
  try {
    const r = await fetch(tab.src + (tab.kind==="tg" ? "?t="+Date.now() : "?t="+Date.now()));
    if (r.ok) cache[tab.id] = normalize(tab, await r.json());
  } catch(e) { cache[tab.id] = cache[tab.id] || []; }
}

// Server-side hidden URLs (links opened/cleared on any device). Reuses the same
// list the scraper already excludes, so hidden deals never come back.
async function loadHidden(){
  try {
    const r = await fetch("/data/cleared.json?t="+Date.now());
    if (r.ok) serverHidden = new Set(await r.json());
  } catch(e) {}
}
function hideOnServer(urls){
  fetch("/api/hide", { method:"POST", credentials:"same-origin",
    headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ urls }) }).catch(()=>{});
}

async function loadTab(tab){
  if (!cache[tab.id] || tab.kind === "tg") await fetchTab(tab);
  // Viewing a tab marks its links as seen -> clears its green dot.
  lsAdd("amzKnown_"+tab.id, urlsOf(tab.id));
  NEW[tab.id] = false;
  render(); applyDots();
}

function visibleItems(){
  if (query) {   // transversal search: matches from EVERY tab, tagged with origin
    const q = query.toLowerCase(), seenUrl = new Set(), out = [];
    for (const t of TABS) {
      const th = hiddenSet(t.id);
      for (const l of (cache[t.id]||[])) {
        if (th.has(l.url) || serverHidden.has(l.url) || seenUrl.has(l.url)) continue;
        if (!(l.name||"").toLowerCase().includes(q)) continue;
        seenUrl.add(l.url);
        out.push(Object.assign({}, l, { srcTab: t.label }));
      }
    }
    return out;
  }
  const h = hiddenSet(current.id);
  let items = (cache[current.id]||[]).filter(l => !h.has(l.url) && (current.kind!=="tg" || !serverHidden.has(l.url)));
  if (current.id === "tg" && sortByDate) {  // newest first, by post date
    items = items.slice().sort((a, b) => (b.date||"").localeCompare(a.date||""));
  }
  return items;
}

function updateSortBtn(){
  const b = document.getElementById("sortBtn");
  b.style.display = current.id === "tg" ? "" : "none";
  b.classList.toggle("active", sortByDate);
  b.textContent = sortByDate ? "Data ↓" : "Por data";
}

function render(){
  updateSortBtn();
  const items = visibleItems();
  document.getElementById("meta").textContent = items.length + " links";
  const box = document.getElementById("list");
  if (!items.length) { box.innerHTML = '<div class="empty">Sem links.</div>';
    document.getElementById("moreBtn").style.display="none"; return; }
  const visited = visitedSet();
  const useGreen = current.kind !== "tg";
  // Grouped mode (e.g. Bom): compact rows bucketed by store — stores ordered
  // by their newest offer, coupons first inside each store.
  const grouping = !!current.group && !query;
  box.classList.toggle("compact", grouping);
  let ordered = items, counts = {};
  if (grouping) {
    const newest = {};
    items.forEach(l => { const s = l.store || "Outras";
      if (!(s in newest)) newest[s] = l.date || "";
      counts[s] = counts[s] || {n:0, c:0}; counts[s].n++; if (l.coupon) counts[s].c++; });
    ordered = items.slice().sort((a, b) => {
      const sa = a.store || "Outras", sb = b.store || "Outras";
      if (sa !== sb) return (newest[sb]||"").localeCompare(newest[sa]||"") || sa.localeCompare(sb);
      const d = (b.coupon?1:0) - (a.coupon?1:0);
      return d || (b.date||"").localeCompare(a.date||"");
    });
  }
  const slice = ordered.slice(0, shown);
  let lastStore = null;
  box.innerHTML = slice.map(l => {
    const dotTitle = l.low ? ('Mínimo de sempre'+(l.minp?': '+l.minp+'€':'')+(l.minlbl?' ('+l.minlbl+')':'')) : '';
    const dot = l.low ? '<span class="low-dot" title="'+esc(dotTitle)+'"></span>' : '';
    // For all-time-low rows show the historical min price; otherwise the usual tag.
    const tag = (l.low && l.minp)
      ? '<span class="tag disc" title="'+esc(dotTitle)+'">mín '+l.minp+'€</span>'
      : (l.extra ? '<span class="tag'+(l.disc?' disc':'')+'" title="Data de publicação">'+
          (l.store?'pub. ':'')+esc(l.extra)+'</span>' : '');
    // Validity of a coupon/offer ("até dd/mm"), when the source states it.
    const val = l.val ? '<span class="tag">até '+esc(l.val)+'</span>' : '';
    // Click-to-copy coupon chip (stops the row link from opening).
    const cpn = l.coupon ? '<button type="button" class="cpn" data-code="'+esc(l.coupon)+'" title="Copiar cupão" onclick="copyCoupon(event,this)">🎟️ '+esc(l.coupon)+'</button>' : '';
    // During transversal search, show which tab the result came from.
    const src = l.srcTab ? '<span class="tag srctab">'+esc(l.srcTab)+'</span>' : '';
    // Tiny product thumb, loaded by the browser straight from Amazon's CDN
    // (._SL96_ = small variant). Hidden automatically if it fails to load.
    const th = l.img ? '<img class="thumb" loading="lazy" alt="" src="https://m.media-amazon.com/images/I/'+
      esc(l.img.replace(/\\.([A-Za-z]+)$/, '._SL96_.$1'))+'" onerror="this.remove()">' : '';
    // Affiliate platform (Awin, TradeTracker, CJ, ...) up front, for reference.
    const stref = l.net ? '<span class="stref" title="Plataforma de afiliação">'+esc(l.net)+'</span>' : '';
    // Deal on several lists: tint the row, stronger the more lists carry it.
    const dupStyle = l.x > 1
      ? ' style="background:rgba(255,153,0,'+Math.min(0.10 + (l.x-2)*0.09, 0.40).toFixed(2)+')" title="Em '+l.x+' listas"'
      : '';
    // Worten deal: marked differently (red edge + badge).
    const wbadge = l.wt ? '<span class="tag wtag">Worten</span>' : '';
    const row = '<li data-url="'+esc(l.url)+'"><a'+dupStyle+' class="'+(useGreen && visited.has(l.url)?'visited':'')+(l.wt?' wt':'')+'" href="'+esc(l.url)+'" target="_blank" rel="noopener">'+
      th + '<span class="name">'+dot+stref+esc(l.name)+'</span>'+ wbadge + cpn + val + src + tag +
      '<span class="arrow">&rsaquo;</span></a></li>';
    if (grouping) {
      const s = l.store || "Outras";
      if (s !== lastStore) {
        lastStore = s;
        const c = counts[s];
        return '<li class="grp"><span>'+esc(s)+'</span><span class="grp-n">'+c.n+
               (c.c ? ' · '+c.c+' 🎟️' : '')+'</span></li>' + row;
      }
    }
    return row;
  }).join("");
  document.getElementById("moreBtn").style.display = items.length > shown ? "" : "none";
}

function copyCoupon(ev, el){
  ev.preventDefault(); ev.stopPropagation();   // don't open the Amazon link
  const code = el.dataset.code;
  const restore = () => { el.textContent = '🎟️ ' + code; el.classList.remove('ok'); };
  const ok = () => { el.classList.add('ok'); el.textContent = 'copiado ✓'; setTimeout(restore, 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText)
    navigator.clipboard.writeText(code).then(ok, () => fallbackCopy(code, ok));
  else fallbackCopy(code, ok);
}
function fallbackCopy(text, cb){
  const t = document.createElement('textarea');
  t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
  document.body.appendChild(t); t.focus(); t.select();
  try { document.execCommand('copy'); } catch(e) {}
  document.body.removeChild(t); if (cb) cb();
}

function applyDots(){
  document.querySelectorAll("#tabs .tab").forEach(b => {
    b.classList.toggle("has-new", !!NEW[b.dataset.id] && b.dataset.id !== current.id);
  });
}

// Check every live (tg) tab for links the user hasn't seen yet -> green dot.
async function refreshDots(){
  await Promise.all(TABS.filter(t => t.kind === "tg").map(async t => {
    await fetchTab(t);
    const known = knownSet(t.id);
    NEW[t.id] = (cache[t.id]||[]).some(l => !known.has(l.url) && !serverHidden.has(l.url));
  }));
  applyDots();
}

function buildTabs(){
  const box = document.getElementById("tabs");
  box.innerHTML = TABS.map(t => '<button class="tab'+(t.id===current.id?' active':'')+'" data-id="'+t.id+'">'+t.label+'</button>').join("");
  box.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => {
    current = TABS.find(t => t.id === b.dataset.id);
    shown = PAGE; query = ""; document.getElementById("search").value = "";
    buildTabs(); loadTab(current);
  }));
  applyDots();
}

document.getElementById("list").addEventListener("click", function(e){
  const a = e.target.closest("a"); if(!a) return;
  const li = a.closest("li");
  if (!li || !li.dataset.url) return;
  if (current.kind === "tg") {                                       // hide everywhere
    const u = li.dataset.url;
    lsAdd("amzHidden_"+current.id, [u]); serverHidden.add(u);
    hideOnServer([u]); li.remove();
  } else { markVisited(li.dataset.url); a.classList.add("visited"); }  // static: green
});
document.getElementById("sortBtn").addEventListener("click", function(){
  sortByDate = !sortByDate;
  localStorage.setItem("amzSortTg", sortByDate ? "1" : "0");
  shown = PAGE; render();
});
document.getElementById("moreBtn").addEventListener("click", () => { shown += PAGE; render(); });
document.getElementById("search").addEventListener("input", function(){ query = this.value.trim(); shown = PAGE; render(); });

// "Limpar tudo" on every tab: hide all links currently in this tab (this browser).
document.getElementById("clearBtn").addEventListener("click", function(){
  if (!confirm("Limpar todos os links desta lista?")) return;
  const urls = urlsOf(current.id);
  lsAdd("amzHidden_"+current.id, urls);
  lsAdd("amzKnown_"+current.id, urls);
  if (current.kind === "tg" && urls.length) {                      // hide everywhere
    urls.forEach(u => serverHidden.add(u));
    hideOnServer(urls);
  }
  render();
});

buildTabs();
(async () => { await loadHidden(); await loadTab(current); })();
refreshDots();
setInterval(async () => { await loadHidden(); if (current.kind === "tg") loadTab(current); refreshDots(); }, 30000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    if "--amazon" in sys.argv:
        scrape_amazon_links()
    else:
        log.info("Usage: python scraper.py --amazon")
