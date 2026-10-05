"""Glue between Etsy, the database and the generator: syncing shops and pushing drafts."""

import asyncio
import json
import logging
import time
from collections import Counter
from pathlib import Path

from . import rules
from .config import Settings
from .db import DB
from .etsy import EtsyClient, EtsyError
from .generator import Generator, GenerationError, ImageInput, ShopContext, taxonomy_candidates

log = logging.getLogger(__name__)

SYNC_STATES = ["active", "draft", "inactive", "sold_out", "expired"]
WATCHED_FIELDS = ["title", "state", "quantity", "price", "tags", "description", "taxonomy_id"]


def client_for_shop(settings: Settings, db: DB, shop_id: int) -> EtsyClient:
    shop = db.shop(shop_id)
    if not shop:
        raise ValueError(f"Shop {shop_id} is not connected")
    return EtsyClient(
        settings,
        shop["access_token"],
        shop["refresh_token"],
        shop["expires_at"] or 0,
        on_refresh=lambda tokens: db.update_tokens(shop_id, tokens),
    )


def price_value(price) -> float | None:
    if isinstance(price, dict) and price.get("divisor"):
        return price["amount"] / price["divisor"]
    if isinstance(price, (int, float)):
        return float(price)
    return None


def flatten_taxonomy(nodes: list[dict], prefix: str = "") -> list[dict]:
    out = []
    for node in nodes:
        path = f"{prefix} > {node['name']}" if prefix else node["name"]
        out.append({"id": node["id"], "name": node["name"], "path": path, "level": node.get("level", 0)})
        out.extend(flatten_taxonomy(node.get("children") or [], path))
    return out


async def sync_taxonomy(client: EtsyClient, db: DB) -> int:
    flat = flatten_taxonomy(await client.get_taxonomy_nodes())
    db.conn.execute("DELETE FROM taxonomy")
    db.conn.executemany(
        "INSERT INTO taxonomy (id, name, path, level) VALUES (?, ?, ?, ?)",
        [(t["id"], t["name"], t["path"], t["level"]) for t in flat],
    )
    db.conn.commit()
    return len(flat)


def _describe_change(old: dict, new: dict) -> str:
    parts = []
    for key in WATCHED_FIELDS:
        a, b = old.get(key), new.get(key)
        if key == "price":
            a, b = price_value(a), price_value(b)
        if a == b:
            continue
        if key in ("description", "tags"):
            parts.append(f"{key} edited")
        else:
            parts.append(f"{key}: {a} → {b}")
    return "; ".join(parts) or "updated"


async def sync_shop(settings: Settings, db: DB, shop_id: int, generator: Generator | None = None) -> dict:
    """Pull the shop's listings and settings from Etsy and record what changed since last time."""
    client = client_for_shop(settings, db, shop_id)
    shop_row = db.shop(shop_id)
    first_sync = not shop_row["last_synced_at"]

    shop = await client.get_shop(shop_id)
    db.run("UPDATE shops SET shop_name=?, data=? WHERE shop_id=?", (shop.get("shop_name"), json.dumps(shop), shop_id))
    for kind, fetch in (
        ("shipping_profiles", client.get_shipping_profiles),
        ("return_policies", client.get_return_policies),
        ("readiness_states", client.get_readiness_states),
    ):
        try:
            db.set_meta(shop_id, kind, await fetch(shop_id))
        except EtsyError as e:
            log.warning("Could not fetch %s for shop %s: %s", kind, shop_id, e)
    if not db.one("SELECT 1 FROM taxonomy LIMIT 1"):
        await sync_taxonomy(client, db)

    existing = {r["listing_id"]: r for r in db.q("SELECT * FROM listings WHERE shop_id=?", (shop_id,))}
    seen: set[int] = set()
    summary = {"new": 0, "updated": 0, "removed": 0, "total": 0, "suggestions": 0}
    to_optimize: list[dict] = []

    for state in SYNC_STATES:
        for listing in await client.list_listings(shop_id, state):
            lid = listing["listing_id"]
            if lid in seen:
                continue
            seen.add(lid)
            score, _ = rules.score_listing(listing)
            modified = listing.get("last_modified_timestamp") or listing.get("updated_timestamp") or 0
            old = existing.get(lid)
            if old is None:
                summary["new"] += 1
                if not first_sync:
                    db.add_change(shop_id, lid, "new", f"New {listing.get('state')} listing: {listing.get('title')}")
                    to_optimize.append(listing)
            elif (old["last_modified"] or 0) != modified:
                summary["updated"] += 1
                db.add_change(shop_id, lid, "updated", _describe_change(json.loads(old["data"]), listing))
                to_optimize.append(listing)
            db.run(
                """INSERT OR REPLACE INTO listings (listing_id, shop_id, state, title, last_modified, score, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (lid, shop_id, listing.get("state"), listing.get("title"), modified, score, json.dumps(listing)),
            )

    for lid, row in existing.items():
        if lid not in seen:
            summary["removed"] += 1
            db.add_change(shop_id, lid, "removed", f"Listing removed from Etsy: {row['title']}")
            db.run("DELETE FROM listings WHERE listing_id=?", (lid,))

    summary["total"] = len(seen)
    if first_sync:
        db.add_change(shop_id, None, "sync", f"First sync: {len(seen)} listings imported")
    db.run("UPDATE shops SET last_synced_at=? WHERE shop_id=?", (time.time(), shop_id))

    if settings.auto_optimize and generator:
        for listing in to_optimize:
            score, _ = rules.score_listing(listing)
            if listing.get("state") != "active" or score >= settings.auto_optimize_below_score:
                continue
            if db.one(
                "SELECT 1 FROM drafts WHERE source_listing_id=? AND kind='optimize' AND status='draft'",
                (listing["listing_id"],),
            ):
                continue
            try:
                await create_optimize_draft(db, generator, shop_id, listing, notes="")
                summary["suggestions"] += 1
            except GenerationError as e:
                log.warning("Auto-optimize failed for %s: %s", listing["listing_id"], e)
    return summary


def build_context(db: DB, shop_id: int, text: str) -> ShopContext:
    shop = db.shop(shop_id)
    shop_data = json.loads(shop["data"] or "{}") if shop else {}
    listings = [json.loads(r["data"]) for r in db.q("SELECT data FROM listings WHERE shop_id=? AND state='active'", (shop_id,))]
    listings.sort(key=lambda l: (l.get("num_favorers") or 0, l.get("views") or 0), reverse=True)
    tag_counts = Counter(t for l in listings for t in (l.get("tags") or []))
    taxonomy = [dict(r) for r in db.q("SELECT * FROM taxonomy")]
    used = {l.get("taxonomy_id") for l in listings if l.get("taxonomy_id")}
    return ShopContext(
        shop_name=shop["shop_name"] if shop else "",
        shop_title=shop_data.get("title") or "",
        example_listings=listings[:4],
        taxonomy_options=taxonomy_candidates(taxonomy, text, used),
        popular_tags=[t for t, _ in tag_counts.most_common(30)],
    )


def default_fields(db: DB, shop_id: int, template: dict | None = None) -> dict:
    """Listing settings for a new draft, copied from a template listing or the shop's defaults."""
    shipping = db.meta(shop_id, "shipping_profiles")
    returns = db.meta(shop_id, "return_policies")
    readiness = db.meta(shop_id, "readiness_states")
    fields = {
        "price": "",
        "quantity": 1,
        "who_made": "i_did",
        "when_made": "made_to_order",
        "is_supply": False,
        "type": "physical",
        "shipping_profile_id": shipping[0]["shipping_profile_id"] if shipping else None,
        "return_policy_id": returns[0]["return_policy_id"] if returns else None,
        "readiness_state_id": readiness[0].get("readiness_state_id") if readiness else None,
    }
    if template:
        for key in ("quantity", "who_made", "when_made", "is_supply", "type", "shipping_profile_id",
                    "return_policy_id", "readiness_state_id", "taxonomy_id"):
            if template.get(key) is not None:
                fields[key] = template[key]
        fields["price"] = price_value(template.get("price")) or ""
    return fields


async def create_new_draft(
    db: DB, generator: Generator, shop_id: int, notes: str, images: list[tuple[str, str]],
    overrides: dict, template: dict | None = None,
) -> int:
    """images: list of (saved file path, media type)."""
    text = notes + (" " + template.get("title", "") if template else "")
    ctx = build_context(db, shop_id, text)
    if template:
        notes = f"{notes}\n\nThis is a variation of an existing listing:\nTitle: {template.get('title')}\n" \
                f"Description:\n{(template.get('description') or '')[:2000]}"
    image_inputs = [ImageInput(Path(p).read_bytes(), mt) for p, mt in images]
    copy = await asyncio.to_thread(generator.generate_new, ctx, notes, image_inputs)
    fields = default_fields(db, shop_id, template)
    fields.update({k: v for k, v in copy.items() if v not in (None, "")})
    fields.update({k: v for k, v in overrides.items() if v not in (None, "")})
    draft_id = db.create_draft(shop_id, "new", notes, fields, template.get("listing_id") if template else None)
    for path, media_type in images:
        db.add_draft_image(draft_id, path, media_type)
    return draft_id


async def create_optimize_draft(db: DB, generator: Generator, shop_id: int, listing: dict, notes: str) -> int:
    ctx = build_context(db, shop_id, f"{listing.get('title', '')} {' '.join(listing.get('tags') or [])}")
    copy = await asyncio.to_thread(generator.optimize, ctx, listing, notes)
    fields = {
        "title": copy["title"] or listing.get("title"),
        "description": copy["description"] or listing.get("description"),
        "tags": copy["tags"] or listing.get("tags") or [],
        "materials": copy["materials"] or listing.get("materials") or [],
        "taxonomy_id": copy["taxonomy_id"] or listing.get("taxonomy_id"),
        "seller_notes": copy.get("seller_notes", ""),
        "original": {k: listing.get(k) for k in ("title", "description", "tags", "materials", "taxonomy_id")},
    }
    return db.create_draft(shop_id, "optimize", notes, fields, listing["listing_id"])


LISTING_KEYS = [
    "title", "description", "price", "quantity", "who_made", "when_made", "taxonomy_id", "tags",
    "materials", "shipping_profile_id", "return_policy_id", "readiness_state_id", "is_supply", "type",
]


async def push_draft(settings: Settings, db: DB, draft_id: int, publish: bool) -> dict:
    """Send a draft to Etsy: create a new listing, or apply an optimization to an existing one."""
    draft = db.draft(draft_id)
    fields = draft["fields"]
    shop_id = draft["shop_id"]
    client = client_for_shop(settings, db, shop_id)

    if draft["kind"] == "optimize":
        update = {k: fields.get(k) for k in ("title", "description", "tags", "materials", "taxonomy_id")}
        result = await client.update_listing(shop_id, draft["source_listing_id"], update)
        db.update_draft(draft_id, status="applied", etsy_listing_id=draft["source_listing_id"], error=None)
        return result

    issues = rules.validate(fields, for_publish=publish)
    if issues:
        raise ValueError(" ".join(issues))
    payload = {k: fields.get(k) for k in LISTING_KEYS}
    if payload.get("type") == "download":
        payload.pop("shipping_profile_id", None)

    listing_id = draft["etsy_listing_id"]
    if listing_id:
        result = await client.update_listing(shop_id, listing_id, payload)
    else:
        result = await client.create_draft_listing(shop_id, payload)
        listing_id = result["listing_id"]
        db.update_draft(draft_id, etsy_listing_id=listing_id)
        for rank, img in enumerate(draft["images"], start=1):
            p = Path(img["path"])
            await client.upload_listing_image(shop_id, listing_id, p.name, p.read_bytes(), img["media_type"], rank)

    if publish:
        result = await client.update_listing(shop_id, listing_id, {"state": "active"})
    db.update_draft(draft_id, status="published" if publish else "pushed", error=None)
    return result


async def sync_loop(settings: Settings, db: DB, get_generator) -> None:
    """Background task: keep every connected shop in sync on an interval."""
    while True:
        await asyncio.sleep(settings.sync_interval_minutes * 60)
        for shop in db.shops():
            try:
                await sync_shop(settings, db, shop["shop_id"], get_generator())
            except Exception:  # keep the loop alive; errors surface on the next manual sync
                log.exception("Background sync failed for shop %s", shop["shop_id"])
