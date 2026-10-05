"""Etsy Open API v3 client: OAuth 2.0 (PKCE) plus the listing endpoints this app uses.

Reference: https://developers.etsy.com/documentation/
"""

import asyncio
import base64
import hashlib
import secrets
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx

from .config import Settings

AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

# Values Etsy accepts for these enum fields on createDraftListing.
WHO_MADE = ["i_did", "someone_else", "collective"]
WHEN_MADE = [
    "made_to_order", "2020_2026", "2010_2019", "2007_2009", "before_2007", "2000_2006",
    "1990s", "1980s", "1970s", "1960s", "1950s", "1940s", "1930s", "1920s", "1910s",
    "1900s", "1800s", "1700s", "before_1700",
]
LISTING_TYPES = ["physical", "download", "both"]


class EtsyError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        message = body.get("error") if isinstance(body, dict) else str(body)
        super().__init__(f"Etsy API {status}: {message}")


def make_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(settings: Settings, state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.etsy_api_key,
        "redirect_uri": settings.etsy_redirect_uri,
        "scope": settings.etsy_scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def _token_request(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(TOKEN_URL, data=data)
    if resp.status_code != 200:
        raise EtsyError(resp.status_code, _body(resp))
    return resp.json()


async def exchange_code(settings: Settings, code: str, verifier: str) -> dict:
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": settings.etsy_api_key,
            "redirect_uri": settings.etsy_redirect_uri,
            "code": code,
            "code_verifier": verifier,
        }
    )


async def refresh_tokens(settings: Settings, refresh_token: str) -> dict:
    return await _token_request(
        {"grant_type": "refresh_token", "client_id": settings.etsy_api_key, "refresh_token": refresh_token}
    )


def _body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _form(fields: dict) -> dict:
    """Etsy's listing endpoints take form fields; lists go comma-separated, bools as strings."""
    out = {}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = ",".join(str(v) for v in value)
        else:
            out[key] = str(value)
    return out


class EtsyClient:
    def __init__(
        self,
        settings: Settings,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: float = 0,
        on_refresh: Callable[[dict], Awaitable[None] | None] | None = None,
    ):
        self.settings = settings
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.on_refresh = on_refresh

    async def _refresh(self) -> None:
        if not self.refresh_token:
            return
        tokens = await refresh_tokens(self.settings, self.refresh_token)
        self.access_token = tokens["access_token"]
        self.refresh_token = tokens.get("refresh_token", self.refresh_token)
        self.expires_at = time.time() + int(tokens.get("expires_in", 3600))
        if self.on_refresh:
            result = self.on_refresh(tokens)
            if asyncio.iscoroutine(result):
                await result

    async def request(self, method: str, path: str, *, params=None, data=None, files=None, auth=True) -> Any:
        if auth and self.refresh_token and self.expires_at and time.time() > self.expires_at - 60:
            await self._refresh()
        url = f"{self.settings.etsy_api_base}/application{path}"
        refreshed = False
        for attempt in range(4):
            headers = {"x-api-key": self.settings.api_key_header}
            if auth:
                headers["Authorization"] = f"Bearer {self.access_token}"
            async with httpx.AsyncClient(timeout=60) as http:
                resp = await http.request(method, url, params=params, data=data, files=files, headers=headers)
            if resp.status_code == 401 and auth and not refreshed and self.refresh_token:
                refreshed = True
                await self._refresh()
                continue
            if resp.status_code == 429 and attempt < 3:
                await asyncio.sleep(float(resp.headers.get("retry-after", 1 + attempt)))
                continue
            if resp.status_code >= 400:
                raise EtsyError(resp.status_code, _body(resp))
            return _body(resp) if resp.content else None
        raise EtsyError(429, "rate limited")

    # Reads -------------------------------------------------------------------
    async def get_me(self) -> dict:
        return await self.request("GET", "/users/me")

    async def get_shop(self, shop_id: int) -> dict:
        return await self.request("GET", f"/shops/{shop_id}")

    async def list_listings(self, shop_id: int, state: str = "active") -> list[dict]:
        results: list[dict] = []
        offset = 0
        while True:
            page = await self.request(
                "GET",
                f"/shops/{shop_id}/listings",
                params={"state": state, "limit": 100, "offset": offset, "includes": "Images"},
            )
            batch = page.get("results", [])
            results.extend(batch)
            offset += len(batch)
            if not batch or offset >= page.get("count", 0):
                return results

    async def get_shipping_profiles(self, shop_id: int) -> list[dict]:
        return (await self.request("GET", f"/shops/{shop_id}/shipping-profiles")).get("results", [])

    async def get_return_policies(self, shop_id: int) -> list[dict]:
        return (await self.request("GET", f"/shops/{shop_id}/policies/return")).get("results", [])

    async def get_readiness_states(self, shop_id: int) -> list[dict]:
        return (await self.request("GET", f"/shops/{shop_id}/readiness-state-definitions")).get("results", [])

    async def get_taxonomy_nodes(self) -> list[dict]:
        return (await self.request("GET", "/seller-taxonomy/nodes", auth=False)).get("results", [])

    # Writes ------------------------------------------------------------------
    async def create_draft_listing(self, shop_id: int, fields: dict) -> dict:
        return await self.request("POST", f"/shops/{shop_id}/listings", data=_form(fields))

    async def update_listing(self, shop_id: int, listing_id: int, fields: dict) -> dict:
        return await self.request("PATCH", f"/shops/{shop_id}/listings/{listing_id}", data=_form(fields))

    async def upload_listing_image(
        self, shop_id: int, listing_id: int, filename: str, content: bytes, media_type: str, rank: int = 1
    ) -> dict:
        return await self.request(
            "POST",
            f"/shops/{shop_id}/listings/{listing_id}/images",
            data={"rank": str(rank)},
            files={"image": (filename, content, media_type)},
        )
