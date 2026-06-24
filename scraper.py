#!/usr/bin/env python3
"""Amazon Affiliate Links Dashboard - Scraper"""

import hashlib
import html
import json
import logging
import os
import re
import sys
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
REFERENCE_WINDOW_HOURS = int(os.environ.get("REFERENCE_WINDOW_HOURS", "24"))

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
KEEPA_TTL_HOURS = int(os.environ.get("KEEPA_TTL_HOURS", "24"))  # re-check each ASIN once a day
# Token safety: cap how many NEW titles we look up per list each run (each is
# queried at most once ever, then cached). Keeps token use well under budget.
KEEPA_MAX_TITLES_PER_LIST = int(os.environ.get("KEEPA_MAX_TITLES_PER_LIST", "25"))
# All-time-low flag is transversal to every tab; cap how many ASINs we (re)price
# per run so token use stays well within budget (each ~1 token, cached 24h).
KEEPA_MAX_PRICE_PER_RUN = int(os.environ.get("KEEPA_MAX_PRICE_PER_RUN", "40"))


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
    r")[^\s\"'<>)]*",
    re.IGNORECASE,
)
AMAZON_SHORT_HOSTS = ("amzn.to", "amzn.eu", "a.co", "amzlink.to")
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

    tag = _affiliate_tag_for(marketplace)
    clean_url = f"https://www.{marketplace}/dp/{asin}"
    affiliate_url = f"{clean_url}?tag={tag}" if tag else clean_url

    return {
        "id": f"{asin}-{marketplace}",
        "asin": asin,
        "marketplace": marketplace,
        "clean_url": clean_url,
        "affiliate_url": affiliate_url,
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
    """Scrape a public Telegram channel for recent posts' text (for link mining)."""
    out = []
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to fetch Telegram channel %s: %s", channel, e)
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


def get_chollo_items() -> list[tuple[str, str, str, bool, str]]:
    """Scan a deals site's home + /populares. Merchant links hide behind /visit/
    redirects, so follow them and keep only the ones landing on amazon.es. Use
    each deal's publishedAt as the date, capture its coupon and title.
    Returns (amazon_url, iso_date, coupon, low, name) tuples."""
    out: list[tuple[str, str, str, bool, str]] = []
    seen = set()
    sess = requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    if not SOURCES.get("chollo_pages"):
        return out
    for page in SOURCES.get("chollo_pages", []):
        try:
            h = sess.get(page, timeout=30).text
        except requests.RequestException as e:
            log.error("Failed to fetch %s: %s", page, e)
            continue
        for section, tid in re.findall(r"/visit/([a-z0-9]+)/(\d+)", h):
            if tid in seen:
                continue
            si = h.find(f"share-deal/{tid}")
            if si == -1:
                continue
            merchant = re.search(r'"merchantUrlName":"([^"]+)"', h[si:si + 1000])
            if not merchant or merchant.group(1) != "amazon.es":  # only amazon.es
                continue
            seen.add(tid)
            before = h[max(0, si - 1500):si]
            pm = re.findall(r'"publishedAt":(\d+)', before)
            vm = re.findall(r'"voucherCode":"([^"]*)"', before)
            tm = re.findall(r'"title":"([^"]+)"', before)
            date = datetime.fromtimestamp(int(pm[-1]), timezone.utc).isoformat() if pm else ""
            coupon = vm[-1] if vm else ""
            name = ""
            if tm:
                try:  # the title is a JSON-escaped string (e.g. í)
                    name = _clean_name(json.loads('"' + tm[-1] + '"'))
                except Exception:
                    name = _clean_name(tm[-1])
            try:
                final = sess.get(f"{SOURCES.get('chollo_visit_base','')}/visit/{section}/{tid}",
                                 timeout=30, allow_redirects=True).url
            except requests.RequestException:
                continue
            urls = extract_amazon_urls(final)
            if urls and "amazon.es" in final:
                out.append((urls[0], date, coupon, False, name))
    log.info("chollo: %d amazon.es deals (%d with coupon)",
             len(out), sum(1 for t in out if t[2]))
    return out


# Keepa csv price series we treat as the real buy-from-Amazon price: Amazon,
# New, New shipped by Amazon (FBA), and New Prime-exclusive (often the lowest).
KEEPA_PRICE_IDX = (0, 1, 10, 33)


def _keepa_min_cents(product) -> int | None:
    """All-time minimum price (cents) across Amazon / New / New-FBA / Prime-excl."""
    if not product:
        return None
    csv = product.get("csv") or []
    vals = []
    for idx in KEEPA_PRICE_IDX:
        if idx < len(csv) and csv[idx]:
            arr = csv[idx]
            for j in range(1, len(arr), 2):  # [time, price, time, price, ...]
                v = arr[j]
                if isinstance(v, int) and v > 0:
                    vals.append(v)
    return min(vals) if vals else None


def _keepa_current_cents(product) -> int | None:
    """Best current price (cents) — lowest latest value across the same series."""
    if not product:
        return None
    csv = product.get("csv") or []
    cur = []
    for idx in KEEPA_PRICE_IDX:
        if idx < len(csv) and csv[idx]:
            arr = csv[idx]
            for j in range(len(arr) - 1, 0, -2):  # latest [time, price] pair
                v = arr[j]
                if isinstance(v, int) and v > 0:
                    cur.append(v)
                    break
    return min(cur) if cur else None


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
                                     "asin": ",".join(batch)}, timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.error("keepa low refresh failed: %s", e)
            break
        if "products" not in data:   # out of tokens / error -> stop, retry later
            log.warning("keepa low: no products (tokensLeft %s)", data.get("tokensLeft"))
            break
        prods = {p.get("asin"): p for p in (data.get("products") or [])}
        for a in batch:
            mn = _keepa_min_cents(prods.get(a))
            cur = _keepa_current_cents(prods.get(a))
            low = bool(mn and cur and mn > 0 and cur > 0 and cur <= mn + 1)
            low_cache[a] = {"low": low, "checked": now.isoformat()}
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


def reference_recent_asins(sess, hours: int = REFERENCE_WINDOW_HOURS) -> set:
    """ASINs that were PUBLISHED *or* UPDATED on the reference source in the last
    N hours. The source re-promotes old posts by updating them (keeping the
    original date_gmt), so we order by `modified` and treat a post as recent when
    EITHER its date_gmt (published) OR its modified_gmt (updated) is within the
    window. Its posts use short links, so expand them to get the ASIN."""
    base = SOURCES.get("cupo_api", "")
    if not base:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cupo_links: list[str] = []
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
            cupo_links += extract_amazon_urls((p.get("content", {}) or {}).get("rendered", ""))
    recent = set()
    for u in dict.fromkeys(cupo_links):
        rr = resolve_amazon_link(u)
        if rr:
            recent.add(rr["asin"])
    log.info("reference source last %dh (published or updated): %d asins", hours, len(recent))
    return recent


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
        cupo_recent = reference_recent_asins(sess, hours=REFERENCE_WINDOW_HOURS)   # ASINs to exclude

    # Source promotions (productId is the ASIN).
    try:
        promos = sess.get(dez_api, timeout=40).json().get("data", [])
    except (requests.RequestException, ValueError) as e:
        log.error("dez: %s", e)
        return out
    seen = set()
    for p in promos:
        if (p.get("store") or "").lower() != "amazon":
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


def scrape_amazon_links():
    """Scan each configured Telegram list into its own JSON, then refresh the page.
    Telegram tab -> data/amazon_links.json ; Descontos tab -> data/descontos.json."""
    log.info("=== Amazon link scan at %s ===", datetime.now().isoformat())
    if AMAZON_AFFILIATE_TAG == "default-tag":
        log.warning("AMAZON_AFFILIATE_TAG is still the placeholder "
                    "— set the AMAZON_AFFILIATE_TAG secret to your real tag.")
    cleared = r2_get_amazon_cleared()  # admin-cleared / opened URLs, excluded from all lists

    # Cross-check EVERY list against the reference source: drop any ASIN that was
    # published (or re-promoted) there in the last 12h. Computed once and reused.
    _sess = requests.Session()
    _sess.headers.update(_BROWSER_HEADERS)
    cupo_recent = reference_recent_asins(_sess, hours=REFERENCE_WINDOW_HOURS)

    # Shared ASIN -> product name map. Sources that expose titles (DEZ/NAS/TITAS/
    # Chollo, and any slug) fill it; bare /dp/ASIN links on other tabs reuse it,
    # so the same deal shows a real name everywhere. Persisted across runs.
    NAMES_KEY = "data/names_by_asin.json"
    _nm = r2_get_amazon_links(NAMES_KEY)
    name_map = _nm.get("n", {})
    keepa_tried = set(_nm.get("tried", []))   # ASINs already looked up on Keepa

    # Transversal all-time-low cache (per ASIN, 24h TTL), shared by every tab.
    # Priced via Keepa BEFORE each tab is written, so the dot is right on first
    # appearance. price_budget is the shared per-run token cap across all tabs.
    LOW_KEY = "data/keepa.json"
    low_cache = r2_get_amazon_links(LOW_KEY).get("k", {})
    all_asins = set()           # every ASIN shown this run -> to bound the cache
    price_budget = [KEEPA_MAX_PRICE_PER_RUN]

    # Cross-tab uniqueness: an ASIN shows on the FIRST tab (in this order) that
    # has it. `claimed` holds ASINs already taken; later tabs exclude them.
    claimed = set()

    last = None
    for channels, web_pages, items_fn, state_key in [
        (AMAZON_TELEGRAM_CHANNELS, AMAZON_WEB_PAGES, None, "data/amazon_links.json"),
        (SOURCES.get("descontos_channels", []), [], None, "data/descontos.json"),
        ([], SOURCES.get("deluxe_pages", []), None, "data/deluxe.json"),
        ([], [], get_chollo_items, "data/chollo.json"),
        ([], [], lambda: get_dez_items(cupo_recent), "data/dez.json"),
        (SOURCES.get("nas_channels", []), [], None, "data/nas.json"),
        ([], [], get_mi_items, "data/mi.json"),
    ]:
        # Exclude reference-source ASINs + any ASIN already shown on an earlier
        # tab this run (cross-tab uniqueness, applied equally to every tab).
        exclude = set(cupo_recent) | claimed
        by_date = state_key in ("data/deluxe.json", "data/chollo.json",
                                "data/dez.json", "data/nas.json", "data/mi.json")
        last = scan_amazon_list(channels, web_pages, state_key, cleared, items_fn,
                                exclude, by_date, name_map, keepa_tried, low_cache, all_asins,
                                price_budget)
        for l in last.get("links", []):
            a = _asin_from_url(l["url"])
            if a:
                claimed.add(a)   # taken — no other tab shows it this run

    # Persist the all-time-low cache (already refreshed per-tab before writing),
    # bounded to the ASINs still shown.
    low_cache = {a: low_cache[a] for a in all_asins if a in low_cache}
    r2_put_amazon_links({"k": low_cache}, LOW_KEY)

    # Persist the shared ASIN -> name map + the Keepa "already tried" set (bounded).
    if len(name_map) > 80000:
        name_map = dict(list(name_map.items())[-80000:])
    r2_put_amazon_links({"n": name_map, "tried": sorted(keepa_tried)[-100000:]}, NAMES_KEY)

    r2_upload_amazon_html()
    return last


def _asin_from_url(u: str) -> str:
    m = re.search(r"/dp/([A-Z0-9]{10})", u or "")
    return m.group(1) if m else ""


def scan_amazon_list(channels, web_pages, state_key, cleared, items_fn=None, exclude_asins=None, sort_by_date=False, name_map=None, keepa_tried=None, low_cache=None, all_asins=None, price_budget=None):
    """Build the freshest batch of clean affiliate links for one source list."""
    existing = r2_get_amazon_links(state_key)
    # Resolution cache: raw short/long URL -> resolved dict (avoids re-expanding).
    cache = existing.get("cache", {})
    # Persistent real-title cache: link id -> product name (filled over time).
    names = existing.get("names", {})
    # First time we saw each URL (used as the date for web sources like Deluxe,
    # whose pages report "now" as the publish date).
    seen = existing.get("seen", {})

    # Collect posts that contain Amazon links, with their publish time, so we can
    # keep only the most recent batch (posts within N hours of the newest one).
    posts: list[tuple[datetime | None, list[str]]] = []
    for channel in channels:
        for post in get_telegram_link_posts(channel):
            urls = extract_amazon_urls(post["html"])
            if urls:
                posts.append((post["dt"], urls))

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
            for raw in extract_amazon_urls(resp.text):
                if raw not in seen_raw:
                    seen_raw.add(raw)
                    candidates.append(raw)
                    raw_to_date[raw] = ""
        except requests.RequestException as e:
            log.error("Failed to fetch web page %s: %s", page, e)
    # Custom provider returns (url, date, coupon[, low]) tuples (chollo/dez).
    raw_to_coupon: dict[str, str] = {}
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

    log.info("[%s] latest batch: %d unique raw Amazon URLs", state_key, len(candidates))

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
        if resolved["affiliate_url"] in cleared:  # admin cleared this one
            continue
        if exclude_asins and resolved["asin"] in exclude_asins:
            continue   # already in (or was in) another list — keep this list unique

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
        if name_map is not None and asin and not name.startswith("Produto "):
            name_map.setdefault(asin, name)
        if all_asins is not None and asin:
            all_asins.add(asin)   # for the transversal all-time-low refresh
        coupon = raw_to_coupon.get(raw_url, "")
        if coupon:
            name = f"{name}  \U0001F39F️ {coupon}"   # 🎟️ coupon code
        url = resolved["affiliate_url"]
        link_date = raw_to_date.get(raw_url, "")   # Telegram post date when available
        if not link_date:                          # web source: use first-seen date
            link_date = seen.get(url) or now_iso
            seen[url] = link_date
        link = {"name": name, "url": url, "date": link_date}
        if raw_to_low.get(raw_url):
            link["low"] = True   # provider already says it's an all-time low
        links.append(link)

    # All-time-low (Keepa) BEFORE writing: price this tab's ASINs that aren't
    # fresh in the cache (shared per-run budget protects tokens), so the dot is
    # correct the moment the deal appears — no one-run delay.
    if low_cache is not None and KEEPA_API_KEY:
        cap = price_budget[0] if price_budget else KEEPA_MAX_PRICE_PER_RUN
        used = keepa_low_refresh([_asin_from_url(l["url"]) for l in links], low_cache, cap)
        if price_budget:
            price_budget[0] -= used
        for l in links:
            if (low_cache.get(_asin_from_url(l["url"])) or {}).get("low"):
                l["low"] = True

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
    """Tabbed dashboard: Telegram batch + two static lists (PD26 ES, Top 100 EU5).
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
  <input class="search" id="search" placeholder="Pesquisar produto..." style="display:none">
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
  { id:"pd26", label:"PD26 ES",      src:"/data/pd26_es.json",      kind:"static", search:true },
  { id:"es",   label:"Top 100 ES",   src:"/data/top100_es.json",    kind:"static" },
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
    return (raw.links||[]).map(l => ({ name:l.name, url:l.url, date:l.date||"", extra:fmtDate(l.date), disc:false, low:!!l.low }));
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
  const h = hiddenSet(current.id);
  let items = (cache[current.id]||[]).filter(l => !h.has(l.url) && (current.kind!=="tg" || !serverHidden.has(l.url)));
  if (current.search && query) {
    const q = query.toLowerCase();
    items = items.filter(l => (l.name||"").toLowerCase().includes(q));
  }
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
  document.getElementById("search").style.display = current.search ? "" : "none";
  const box = document.getElementById("list");
  if (!items.length) { box.innerHTML = '<div class="empty">Sem links.</div>';
    document.getElementById("moreBtn").style.display="none"; return; }
  const visited = visitedSet();
  const useGreen = current.kind !== "tg";
  const slice = items.slice(0, shown);
  box.innerHTML = slice.map(l =>
    '<li data-url="'+esc(l.url)+'"><a class="'+(useGreen && visited.has(l.url)?'visited':'')+'" href="'+esc(l.url)+'" target="_blank" rel="noopener">'+
    '<span class="name">'+(l.low?'<span class="low-dot" title="Preço mais baixo de sempre"></span>':'')+esc(l.name)+'</span>'+
    (l.extra ? '<span class="tag'+(l.disc?' disc':'')+'">'+esc(l.extra)+'</span>' : '')+
    '<span class="arrow">&rsaquo;</span></a></li>'
  ).join("");
  document.getElementById("moreBtn").style.display = items.length > shown ? "" : "none";
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
