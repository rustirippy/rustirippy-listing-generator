"""Listing copy generation with Claude.

The generator sees the shop's own best listings (so new copy matches the shop's voice), a shortlist of
Etsy categories, and the seller's notes and photos, and returns structured fields that are then cleaned
to Etsy's limits in rules.py.
"""

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

import anthropic
from pydantic import BaseModel

from . import rules
from .config import Settings


class ListingCopy(BaseModel):
    title: str
    description: str
    tags: list[str]
    materials: list[str]
    taxonomy_id: int | None
    seller_notes: str


@dataclass
class ShopContext:
    shop_name: str = ""
    shop_title: str = ""
    example_listings: list[dict] = field(default_factory=list)
    taxonomy_options: list[dict] = field(default_factory=list)
    popular_tags: list[str] = field(default_factory=list)


@dataclass
class ImageInput:
    data: bytes
    media_type: str


class Generator(Protocol):
    def generate_new(self, ctx: ShopContext, notes: str, images: list[ImageInput]) -> dict: ...
    def optimize(self, ctx: ShopContext, listing: dict, notes: str) -> dict: ...


SYSTEM_PROMPT = """You write Etsy listings for independent sellers. Your goal is listings that buyers find \
in Etsy search and that convert, written in the seller's own voice.

What good looks like on Etsy:
- Title: clear and readable, leading with what the item is, then the one or two details buyers search \
for (material, style, recipient, occasion). Under 140 characters, no keyword stuffing, no repeated \
words, and the characters %, : and & at most once each.
- Description: the first two sentences restate what it is and why someone would want it, since they \
show in search previews. Then concrete details: size, materials, colors, how it's made, personalization \
options, care, shipping or gifting notes. Short paragraphs or simple lists. Never invent facts the \
seller didn't give or the photos don't show; if something important is missing (size, materials), \
write around it and mention it in seller_notes.
- Tags: exactly 13, each 20 characters or fewer, mostly multi-word phrases real buyers type. Don't \
just repeat the title word for word; cover synonyms, styles, recipients and occasions.
- Materials: what it's made of, up to 13 short entries, letters and numbers only.
- taxonomy_id: pick the best match from the category options given, or null if none fit.
- seller_notes: one to three short sentences to the seller about missing info or quick wins. Not shown \
to buyers."""


def _example_block(ctx: ShopContext) -> str:
    lines = [f"Shop: {ctx.shop_name}" + (f" ({ctx.shop_title})" if ctx.shop_title else "")]
    if ctx.example_listings:
        lines.append("\nThe shop's best listings, to match its voice and formatting:")
        for ex in ctx.example_listings[:4]:
            desc = (ex.get("description") or "")[:1200]
            lines.append(
                f"<listing>\nTitle: {ex.get('title')}\nTags: {', '.join(ex.get('tags') or [])}\n"
                f"Description:\n{desc}\n</listing>"
            )
    if ctx.popular_tags:
        lines.append("\nTags this shop uses most: " + ", ".join(ctx.popular_tags[:30]))
    if ctx.taxonomy_options:
        lines.append("\nCategory options (taxonomy_id: path):")
        lines.extend(f"{t['id']}: {t['path']}" for t in ctx.taxonomy_options[:60])
    return "\n".join(lines)


def finalize(copy: dict, ctx: ShopContext) -> dict:
    """Clean model output to Etsy's limits and drop category ids that weren't offered."""
    allowed = {t["id"] for t in ctx.taxonomy_options}
    taxonomy_id = copy.get("taxonomy_id")
    if allowed and taxonomy_id not in allowed:
        taxonomy_id = None
    return {
        "title": rules.clean_title(copy.get("title", "")),
        "description": (copy.get("description") or "").strip(),
        "tags": rules.clean_tags(copy.get("tags", [])),
        "materials": rules.clean_materials(copy.get("materials", [])),
        "taxonomy_id": taxonomy_id,
        "seller_notes": copy.get("seller_notes", ""),
    }


class GenerationError(Exception):
    pass


class ClaudeGenerator:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self.client = client or anthropic.Anthropic()

    def _call(self, content: list[dict]) -> dict:
        try:
            response = self.client.beta.messages.parse(
                model=self.settings.claude_model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=ListingCopy,
                output_config={"effort": self.settings.claude_effort},
                # If a request is ever declined, retry it on a fallback model inside the same call.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise GenerationError("Claude API key is missing or invalid (set ANTHROPIC_API_KEY).") from e
        except anthropic.RateLimitError as e:
            raise GenerationError("Claude API rate limit reached; try again in a minute.") from e
        except anthropic.APIStatusError as e:
            raise GenerationError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise GenerationError("Couldn't reach the Claude API.") from e
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise GenerationError("Claude couldn't write this listing. Try rewording the notes.")
        return response.parsed_output.model_dump()

    def generate_new(self, ctx: ShopContext, notes: str, images: list[ImageInput]) -> dict:
        content: list[dict] = []
        for img in images[:5]:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": base64.standard_b64encode(img.data).decode(),
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": f"{_example_block(ctx)}\n\nWrite a new listing for this item. Seller's notes:\n"
                f"<notes>\n{notes}\n</notes>" + ("\nThe photos above show the item." if images else ""),
            }
        )
        return finalize(self._call(content), ctx)

    def optimize(self, ctx: ShopContext, listing: dict, notes: str = "") -> dict:
        current = {
            "title": listing.get("title"),
            "description": listing.get("description"),
            "tags": listing.get("tags"),
            "materials": listing.get("materials"),
            "taxonomy_id": listing.get("taxonomy_id"),
        }
        text = (
            f"{_example_block(ctx)}\n\nImprove this existing listing for Etsy search and conversion. Keep "
            "every fact, keep the seller's voice, and keep the current taxonomy_id unless it is clearly "
            f"wrong.\n<current_listing>\n{json.dumps(current, indent=2)}\n</current_listing>"
        )
        if notes:
            text += f"\nSeller's extra notes:\n<notes>\n{notes}\n</notes>"
        return finalize(self._call([{"type": "text", "text": text}]), ctx)


# Context helpers --------------------------------------------------------------

_WORD = re.compile(r"[a-z]{3,}")


def taxonomy_candidates(taxonomy: list[dict], text: str, prefer_ids: set[int], limit: int = 60) -> list[dict]:
    """Shortlist categories by word overlap with the notes, always keeping ones the shop already uses."""
    words = set(_WORD.findall(text.lower()))
    words |= {w[:-1] for w in words if w.endswith("s")}
    scored = []
    for node in taxonomy:
        path_words = set(_WORD.findall(node["path"].lower()))
        path_words |= {w[:-1] for w in path_words if w.endswith("s")}
        overlap = len(words & path_words)
        bonus = 3 if node["id"] in prefer_ids else 0
        if overlap or bonus:
            scored.append((overlap + bonus, node["level"], node))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    return [s[2] for s in scored[:limit]]
