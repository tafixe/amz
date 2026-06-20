# Security & Secrets

This is a **public repository**. The code is intentionally generic — every
identifying value (source sites, Telegram channels, domains, affiliate tags,
API keys, passwords) lives in **Secrets**, never in git. A fork is useless
without them.

## GitHub Secrets (Settings → Secrets and variables → Actions)

| Secret | Purpose |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token (R2 + Workers). **Rotate if ever exposed.** |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account id (also used as wrangler's account at deploy). |
| `AMAZON_AFFILIATE_TAG` | Default affiliate tag. |
| `AMAZON_AFFILIATE_TAGS_JSON` | Per-marketplace tags, e.g. `{"amazon.es":"xxx-21"}`. |
| `AMAZON_TELEGRAM_CHANNELS_JSON` | Main Telegram channels, e.g. `["channel_a"]`. |
| `AMAZON_WEB_PAGES_JSON` | Extra web pages to scan, e.g. `[]`. |
| `SOURCES_JSON` | All remaining source URLs/channels (keys below). |
| `KEEPA_API_KEY` | *(optional)* Keepa price-history API key. |
| `AMAZON_ADMIN_PASSWORD` | Dashboard login password (pushed to the worker on deploy). |
| `PROXY_A_URL` / `PROXY_B_URL` | Upstream URL templates (with `{page}`) for the worker edge proxies. |
| `PROMO_URL` | Link behind the dashboard's promo button. |

### `SOURCES_JSON` keys

```json
{
  "chollo_pages": ["https://.../", "https://.../populares"],
  "chollo_visit_base": "https://...",
  "titas_proxy": "https://<worker>/api/src-a?",
  "titas_direct": "https://.../wp-json/wp/v2/posts?per_page=100&page=",
  "cupo_api": "https://.../wp-json/wp/v2/posts",
  "dez_api": "https://.../api/promotions?limit=100",
  "nas_proxy": "https://<worker>/api/src-b?",
  "nas_direct": "https://.../wp-json/wp/v2/posts?per_page=100&orderby=date&order=desc&page=",
  "descontos_channels": ["channel_b"],
  "deluxe_pages": ["https://.../"]
}
```

Any omitted key simply disables that source.

## Rules

- **Never** hardcode a domain, channel, tag, key, or password in code.
- Keep history clean — if a secret is ever committed, **rotate it** (assume
  it's compromised) and scrub history.
- Worker secrets (`ADMIN_PASSWORD`, `PROXY_*`, `PROMO_URL`) are set by
  `deploy-amazon.yml` from the matching GitHub Secrets.
