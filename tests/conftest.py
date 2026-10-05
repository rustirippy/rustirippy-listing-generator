import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

API = "https://api.etsy.com/v3/application"
SHOP_ID = 555
USER_ID = 777


def make_listing(listing_id, title, modified=1000, state="active", **extra):
    base = {
        "listing_id": listing_id,
        "title": title,
        "description": "A lovely handmade thing. " * 5,
        "state": state,
        "tags": ["handmade mug", "ceramic cup"],
        "materials": ["stoneware"],
        "taxonomy_id": 1002,
        "price": {"amount": 2400, "divisor": 100, "currency_code": "USD"},
        "quantity": 3,
        "who_made": "i_did",
        "when_made": "made_to_order",
        "is_supply": False,
        "shipping_profile_id": 11,
        "return_policy_id": 22,
        "last_modified_timestamp": modified,
        "num_favorers": 5,
        "url": f"https://www.etsy.com/listing/{listing_id}",
        "images": [],
    }
    base.update(extra)
    return base


class FakeEtsy:
    """In-memory stand-in for the Etsy API, wired up with respx."""

    def __init__(self, router: respx.Router):
        self.listings = {101: make_listing(101, "Speckled Mug")}
        self.created = []
        self.updates = []
        self.images = []
        self.next_id = 900
        self.router = r = router
        r.post("https://api.etsy.com/v3/public/oauth/token").mock(side_effect=self.token)
        r.get(f"{API}/users/me").mock(return_value=httpx.Response(200, json={"user_id": USER_ID, "shop_id": SHOP_ID}))
        r.get(f"{API}/shops/{SHOP_ID}").mock(
            return_value=httpx.Response(200, json={"shop_id": SHOP_ID, "shop_name": "ClayCo", "title": "Pottery for slow mornings"})
        )
        r.get(f"{API}/shops/{SHOP_ID}/listings").mock(side_effect=self.list_listings)
        r.get(f"{API}/shops/{SHOP_ID}/shipping-profiles").mock(
            return_value=httpx.Response(200, json={"count": 1, "results": [{"shipping_profile_id": 11, "title": "Standard"}]})
        )
        r.get(f"{API}/shops/{SHOP_ID}/policies/return").mock(
            return_value=httpx.Response(200, json={"count": 1, "results": [{"return_policy_id": 22, "accepts_returns": True, "return_deadline": 30}]})
        )
        r.get(f"{API}/shops/{SHOP_ID}/readiness-state-definitions").mock(
            return_value=httpx.Response(200, json={"count": 1, "results": [{"readiness_state_id": 33, "readiness_state": "made_to_order", "min_processing_time": 1, "max_processing_time": 3, "processing_time_unit": "days"}]})
        )
        r.get(f"{API}/seller-taxonomy/nodes").mock(
            return_value=httpx.Response(200, json={"count": 1, "results": [
                {"id": 1000, "level": 0, "name": "Home & Living", "children": [
                    {"id": 1001, "level": 1, "name": "Kitchen & Dining", "children": [
                        {"id": 1002, "level": 2, "name": "Mugs", "children": []},
                    ]},
                ]},
            ]})
        )
        r.post(f"{API}/shops/{SHOP_ID}/listings").mock(side_effect=self.create)
        r.patch(url__regex=rf"{API}/shops/{SHOP_ID}/listings/\d+$").mock(side_effect=self.update)
        r.post(url__regex=rf"{API}/shops/{SHOP_ID}/listings/\d+/images$").mock(side_effect=self.image)

    def token(self, request):
        form = parse_qs(request.content.decode())
        self.last_token_form = form
        return httpx.Response(200, json={"access_token": f"{USER_ID}.access", "refresh_token": f"{USER_ID}.refresh", "expires_in": 3600})

    def list_listings(self, request):
        state = request.url.params.get("state")
        rows = [l for l in self.listings.values() if l["state"] == state]
        return httpx.Response(200, json={"count": len(rows), "results": rows})

    def create(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.created.append(form)
        lid = self.next_id
        self.next_id += 1
        self.listings[lid] = make_listing(lid, form["title"], state="draft", modified=2000, tags=form.get("tags", "").split(","))
        return httpx.Response(201, json={"listing_id": lid, "state": "draft"})

    def update(self, request):
        lid = int(request.url.path.rsplit("/", 1)[1])
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.updates.append((lid, form))
        listing = self.listings[lid]
        for k, v in form.items():
            listing[k] = v.split(",") if k in ("tags", "materials") else v
        listing["last_modified_timestamp"] += 1
        return httpx.Response(200, json=listing)

    def image(self, request):
        self.images.append(request.url.path)
        return httpx.Response(201, json={"listing_image_id": 1})


class FakeGenerator:
    def __init__(self):
        self.calls = []

    def generate_new(self, ctx, notes, images):
        self.calls.append(("new", ctx, notes, len(images)))
        return {
            "title": "Speckled Stoneware Mug, Handmade Ceramic Coffee Cup",
            "description": "A hand-thrown mug for slow mornings.",
            "tags": ["handmade mug", "ceramic coffee cup", "pottery gift"],
            "materials": ["stoneware", "glaze"],
            "taxonomy_id": 1002,
            "seller_notes": "Add the mug's height.",
        }

    def optimize(self, ctx, listing, notes):
        self.calls.append(("optimize", ctx, listing, notes))
        return {
            "title": "Speckled Stoneware Mug, Handmade Pottery Coffee Cup",
            "description": "Better description.",
            "tags": [f"tag number {i}" for i in range(13)],
            "materials": ["stoneware"],
            "taxonomy_id": 1002,
            "seller_notes": "",
        }


@pytest.fixture
def settings(tmp_path):
    return Settings(
        etsy_api_key="key123",
        etsy_shared_secret="secret456",
        database_path=str(tmp_path / "app.db"),
        upload_dir=str(tmp_path / "uploads"),
        background_sync=False,
        app_password="",
    )


@pytest.fixture
def etsy_api():
    with respx.mock(assert_all_called=False) as router:
        yield FakeEtsy(router)


@pytest.fixture
def generator():
    return FakeGenerator()


@pytest.fixture
def client(settings, generator, etsy_api):
    app = create_app(settings, generator=generator)
    with TestClient(app) as c:
        c.app_db = app.state.db
        yield c


def connect_shop(client):
    resp = client.get("/connect", follow_redirects=False)
    url = urlparse(resp.headers["location"])
    state = parse_qs(url.query)["state"][0]
    return client.get(f"/oauth/callback?code=abc&state={state}", follow_redirects=False), url
