"""Etsy listing limits, cleanup, and a simple quality score.

Limits follow Etsy's createDraftListing rules: title up to 140 characters with %, : and & each used
at most once; up to 13 tags of 20 characters; up to 13 materials of 45 characters.
"""

import re

TITLE_MAX = 140
TAG_MAX = 20
TAGS_MAX = 13
MATERIAL_MAX = 45
MATERIALS_MAX = 13

_TAG_BAD = re.compile(r"[^\w\s\-'™©®]", re.UNICODE)
_MATERIAL_BAD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def clean_title(title: str) -> str:
    title = _SPACES.sub(" ", (title or "").replace("\n", " ")).strip()
    for ch in "%:&":
        first = title.find(ch)
        if first != -1:
            title = title[: first + 1] + title[first + 1 :].replace(ch, "")
    title = _SPACES.sub(" ", title).strip()
    if len(title) > TITLE_MAX:
        cut = title[:TITLE_MAX]
        title = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return title.strip(" ,-|")


def _clean_list(items, bad: re.Pattern, item_max: int, count_max: int) -> list[str]:
    if isinstance(items, str):
        items = items.split(",")
    seen: set[str] = set()
    out: list[str] = []
    for raw in items or []:
        item = _SPACES.sub(" ", bad.sub("", str(raw))).strip().lower()
        if not item:
            continue
        if len(item) > item_max:
            continue  # a truncated phrase ("pottery gift for") is worse than none
        if item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) == count_max:
            break
    return out


def clean_tags(tags) -> list[str]:
    return _clean_list(tags, _TAG_BAD, TAG_MAX, TAGS_MAX)


def clean_materials(materials) -> list[str]:
    return _clean_list(materials, _MATERIAL_BAD, MATERIAL_MAX, MATERIALS_MAX)


def validate(fields: dict, *, for_publish: bool = False) -> list[str]:
    """Problems that would make Etsy reject the listing (or that a seller should fix first)."""
    issues = []
    title = fields.get("title") or ""
    if not title:
        issues.append("Title is required.")
    if len(title) > TITLE_MAX:
        issues.append(f"Title is {len(title)} characters; Etsy allows {TITLE_MAX}.")
    for ch in "%:&":
        if title.count(ch) > 1:
            issues.append(f"Title can use '{ch}' only once.")
    if not (fields.get("description") or "").strip():
        issues.append("Description is required.")
    tags = fields.get("tags") or []
    if len(tags) > TAGS_MAX:
        issues.append(f"Etsy allows {TAGS_MAX} tags; this has {len(tags)}.")
    for t in tags:
        if len(t) > TAG_MAX:
            issues.append(f"Tag '{t}' is longer than {TAG_MAX} characters.")
    try:
        price = float(fields.get("price") or 0)
    except (TypeError, ValueError):
        price = 0
    if price < 0.20:
        issues.append("Price must be at least 0.20.")
    try:
        quantity = int(fields.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0
    if quantity < 1:
        issues.append("Quantity must be at least 1.")
    if not fields.get("taxonomy_id"):
        issues.append("Pick a category (taxonomy).")
    if not fields.get("who_made") or not fields.get("when_made"):
        issues.append("Who made it and when it was made are required.")
    if for_publish and fields.get("type", "physical") != "download" and not fields.get("shipping_profile_id"):
        issues.append("Physical listings need a shipping profile before they can go live.")
    return issues


def score_listing(listing: dict) -> tuple[int, list[str]]:
    """0-100 score for how well a listing uses Etsy's search fields, plus tips."""
    score = 100
    tips = []
    title = listing.get("title") or ""
    tags = listing.get("tags") or []
    description = listing.get("description") or ""
    words = len(title.split())
    if words < 4:
        score -= 15
        tips.append("Title is very short; lead with what the item is plus one or two key details.")
    elif len(title) > 120:
        score -= 5
        tips.append("Title is long; Etsy favors clear, readable titles over keyword lists.")
    if len(tags) < TAGS_MAX:
        score -= min(30, (TAGS_MAX - len(tags)) * 3)
        tips.append(f"Uses {len(tags)} of {TAGS_MAX} tags.")
    single_word = [t for t in tags if " " not in t]
    if len(single_word) > 5:
        score -= 10
        tips.append("Many one-word tags; multi-word phrases match more searches.")
    if len(description) < 300:
        score -= 20
        tips.append("Description is short; add size, materials, care and gifting details.")
    if not listing.get("materials"):
        score -= 5
        tips.append("No materials listed.")
    images = listing.get("images") or listing.get("Images") or []
    if isinstance(images, list) and 0 < len(images) < 5:
        score -= 10
        tips.append(f"Only {len(images)} photos; listings with 5 or more tend to convert better.")
    return max(0, score), tips
