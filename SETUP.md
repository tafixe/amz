# Amazon Affiliate Links Dashboard

Scrapes configured sources for Amazon links, applies an affiliate tag, and
serves them on a password-gated dashboard. Runs free on GitHub Actions (public
repo) + Cloudflare Workers/R2.

> **All identifying data — source sites, channels, domains, keys — lives in
> GitHub/Cloudflare Secrets, never in this code.** See [`.github/SECURITY.md`](.github/SECURITY.md).

## Architecture

- `scraper.py --amazon` — GitHub Action (`amazon.yml`), every 10 min. Reads the
  source config from secrets, builds the link lists, writes them to R2.
- `worker-amazon/` — Cloudflare Worker that serves the dashboard from R2, gated
  by a password (Cloudflare secret), with edge proxies for blocked upstreams.

## Quick start

1. Add the GitHub Secrets listed in [`.github/SECURITY.md`](.github/SECURITY.md)
   (**Settings → Secrets and variables → Actions**).
2. Push to `main` → `deploy-amazon.yml` deploys the worker and sets its secrets.
3. `amazon.yml` runs on its schedule (or trigger it manually under **Actions**).

## Local development

Copy `.env.example` to `.env`, fill it, then:

```bash
pip install -r requirements.txt
set -a; source .env; set +a
python scraper.py --amazon
```
