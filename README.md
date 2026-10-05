# Listing Generator

A web app that connects to an Etsy shop, keeps a live copy of its listings, and uses Claude to write new
listings and improve existing ones in the shop's own voice. Drafts go back to Etsy in one click.

## What it does

- **Connect a shop** with Etsy's official OAuth (PKCE). Tokens refresh automatically.
- **Stays in sync**: pulls active, draft, inactive, sold-out and expired listings, shipping profiles,
  return policies and processing times every few minutes, and logs what changed (new listings, price or
  title edits, removals).
- **New listing from notes and photos**: type a few lines about the item, add photos, and get a title,
  description, 13 tags, materials and a category, written in the style of the shop's best sellers.
- **Make similar**: start from an existing listing and only describe what's different. Price, category,
  shipping and return settings are copied over.
- **Improve a listing**: every listing gets a 0-100 score for how well it uses Etsy's search fields.
  One click drafts a rewrite you can review side by side and apply to the live listing.
- **Auto-improve (optional)**: when a listing changes on Etsy and scores low, an improvement draft is
  written for you to review.
- **Bulk create** from a CSV (`notes, price, quantity`), up to 50 drafts at a time.
- **Etsy-safe output**: titles, tags and materials are cleaned to Etsy's limits before anything is sent,
  and missing required fields are flagged before you push.
- **Push to Etsy** as a free draft, or publish live. Photos are uploaded with the listing.

## Run it

You need Python 3.10+, an Etsy developer app, and a Claude API key.

1. Create an app at https://www.etsy.com/developers/your-apps. Add `http://localhost:8000/oauth/callback`
   as a callback URL. Copy the keystring and shared secret.
2. Get a Claude API key at https://platform.claude.com.
3. Install and configure:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # then fill in the keys
   uvicorn app.main:get_app --factory --reload
   ```

4. Open http://localhost:8000 and click **Connect an Etsy shop**.

Or with Docker:

```bash
docker build -t listing-generator .
docker run -p 8000:8000 --env-file .env -v listing-data:/data listing-generator
```

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run the whole flow (connect, sync, generate, push, improve, change detection) against a fake
Etsy API and a fake generator, so they need no keys.

## Configuration

All settings live in `.env`; see `.env.example`. The main ones:

| Setting | Default | What it does |
| --- | --- | --- |
| `ETSY_API_KEY`, `ETSY_SHARED_SECRET` | | Your Etsy app credentials. Sent as `x-api-key: keystring:secret`. |
| `ETSY_REDIRECT_URI` | `http://localhost:8000/oauth/callback` | Must match the callback URL on your Etsy app. |
| `ANTHROPIC_API_KEY` | | Claude API key. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Model used for writing. |
| `CLAUDE_EFFORT` | `medium` | `low` is faster and cheaper, `high` writes more carefully. |
| `SYNC_INTERVAL_MINUTES` | `15` | Background sync interval; `0` turns it off. |
| `AUTO_OPTIMIZE` | `false` | Draft improvements for changed listings that score below `AUTO_OPTIMIZE_BELOW_SCORE`. |
| `APP_PASSWORD` | | Requires a password to use the app. Set this whenever it's on the internet. |

## How it's built

- `app/etsy.py`: Etsy Open API v3 client (OAuth, listings, images, shipping, taxonomy), with token refresh
  and rate-limit retries.
- `app/services.py`: sync and change detection, draft creation, pushing drafts to Etsy.
- `app/generator.py`: Claude prompt and structured output. The model sees the shop's top listings, its
  most-used tags, and a shortlist of matching Etsy categories.
- `app/rules.py`: Etsy field limits, cleanup and the listing score.
- `app/main.py` and `app/templates/`: the FastAPI web app.
- Storage is a single SQLite file (`data/app.db`).

## Selling it

Things to sort out before charging for it:

- **Etsy API access.** New Etsy apps start with personal access, which only works for the developer's own
  shop. To let other sellers connect their shops you need to request commercial access for your app from
  Etsy and follow the [Etsy API Terms of Use](https://www.etsy.com/legal/api). Etsy requires the trademark
  notice shown in the app footer and doesn't allow "Etsy" in your product's name.
- **Hosting model.** As built, each customer can self-host with their own Etsy and Claude keys (simplest to
  sell as a one-time license). To sell it as a hosted subscription, add user accounts, billing (e.g.
  Stripe), per-customer data separation, and encrypted token storage.
- **AI costs.** On a hosted plan you pay for Claude usage, so price plans around generations per month.
