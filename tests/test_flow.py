"""End-to-end: connect a shop, sync, generate a listing, push it, improve one, and catch Etsy changes."""

import io
import json
from urllib.parse import parse_qs

import httpx
import respx

from app import etsy
from app.config import Settings
from tests.conftest import API, SHOP_ID, connect_shop, make_listing


def test_connect_uses_pkce_and_imports_shop(client, etsy_api):
    resp, auth_url = connect_shop(client)
    assert resp.status_code == 303 and resp.headers["location"] == f"/shops/{SHOP_ID}"
    params = parse_qs(auth_url.query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["client_id"] == ["key123"]
    assert "listings_w" in params["scope"][0]
    assert etsy_api.last_token_form["code_verifier"][0]

    page = client.get(f"/shops/{SHOP_ID}")
    assert "ClayCo" in page.text and "Speckled Mug" in page.text
    assert "First sync: 1 listings imported" in page.text


def test_api_key_header_includes_shared_secret(client, etsy_api):
    connect_shop(client)
    me_call = [c for c in etsy_api.router.calls if c.request.url.path.endswith("/users/me")][-1]
    assert me_call.request.headers["x-api-key"] == "key123:secret456"
    assert me_call.request.headers["authorization"] == "Bearer 777.access"


def test_generate_and_publish_new_listing(client, etsy_api, generator):
    connect_shop(client)
    resp = client.post(
        f"/shops/{SHOP_ID}/new",
        data={"notes": "speckled stoneware mug, 12 oz", "price": "28", "quantity": "4"},
        files=[("photos", ("mug.jpg", io.BytesIO(b"\xff\xd8fakejpeg"), "image/jpeg"))],
        follow_redirects=False,
    )
    assert resp.status_code == 303
    draft_url = resp.headers["location"]

    kind, ctx, notes, n_images = generator.calls[-1]
    assert kind == "new" and n_images == 1
    assert ctx.example_listings[0]["title"] == "Speckled Mug"  # shop voice examples
    assert any(t["id"] == 1002 for t in ctx.taxonomy_options)  # "mug" matched the Mugs category

    page = client.get(draft_url)
    assert "Speckled Stoneware Mug" in page.text and "Add the mug&#39;s height." in page.text

    resp = client.post(f"{draft_url}/push", data={"action": "publish"}, follow_redirects=False)
    assert resp.status_code == 303
    created = etsy_api.created[-1]
    assert created["title"].startswith("Speckled Stoneware Mug")
    assert created["price"] == "28" and created["quantity"] == "4"
    assert created["shipping_profile_id"] == "11" and created["taxonomy_id"] == "1002"
    assert created["tags"] == "handmade mug,ceramic coffee cup,pottery gift"
    assert len(etsy_api.images) == 1
    assert etsy_api.updates[-1][1] == {"state": "active"}

    draft_id = int(draft_url.rsplit("/", 1)[1])
    assert client.app_db.draft(draft_id)["status"] == "published"


def test_make_similar_copies_template_settings(client, etsy_api, generator):
    connect_shop(client)
    resp = client.post(f"/shops/{SHOP_ID}/new", data={"notes": "same mug in blue", "template_id": "101"}, follow_redirects=False)
    draft = client.app_db.draft(int(resp.headers["location"].rsplit("/", 1)[1]))
    assert draft["fields"]["price"] == 24.0
    assert draft["fields"]["shipping_profile_id"] == 11
    assert draft["source_listing_id"] == 101
    assert "variation of an existing listing" in generator.calls[-1][2]


def test_push_blocks_invalid_draft(client, etsy_api):
    connect_shop(client)
    resp = client.post(f"/shops/{SHOP_ID}/new", data={"notes": "mug"}, follow_redirects=False)  # no price
    draft_url = resp.headers["location"]
    client.post(f"{draft_url}/push", data={"action": "draft"})
    assert etsy_api.created == []
    assert "Price must be at least" in client.get(draft_url).text


def test_optimize_and_apply_then_sync_sees_change(client, etsy_api):
    connect_shop(client)
    resp = client.post("/listings/101/optimize", follow_redirects=False)
    draft_url = resp.headers["location"]
    page = client.get(draft_url)
    assert "Was: Speckled Mug" in page.text

    client.post(f"{draft_url}/push", data={"action": "apply"})
    lid, form = etsy_api.updates[-1]
    assert lid == 101 and form["title"].startswith("Speckled Stoneware Mug")
    assert len(form["tags"].split(",")) == 13

    changes = client.app_db.q("SELECT kind, summary FROM changes WHERE listing_id=101")
    assert any(c["kind"] == "updated" and "title" in c["summary"] for c in changes)


def test_sync_detects_new_updated_removed(client, etsy_api):
    connect_shop(client)
    etsy_api.listings[102] = make_listing(102, "Tea Bowl", modified=5)
    etsy_api.listings[101]["price"] = {"amount": 3000, "divisor": 100}
    etsy_api.listings[101]["last_modified_timestamp"] = 1001
    client.post(f"/shops/{SHOP_ID}/sync")
    kinds = {c["kind"]: c["summary"] for c in client.app_db.q("SELECT * FROM changes")}
    assert "New active listing: Tea Bowl" in kinds["new"]
    assert "price: 24.0 → 30.0" in kinds["updated"]

    del etsy_api.listings[102]
    client.post(f"/shops/{SHOP_ID}/sync")
    assert client.app_db.one("SELECT * FROM changes WHERE kind='removed'")["listing_id"] == 102


def test_auto_optimize_drafts_suggestions_for_weak_changed_listings(settings, generator, etsy_api):
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings.auto_optimize = True
    app = create_app(settings, generator=generator)
    with TestClient(app) as client:
        connect_shop(client)
        etsy_api.listings[103] = make_listing(103, "Bowl", modified=1, tags=[], description="short")
        client.post(f"/shops/{SHOP_ID}/sync")
        drafts = app.state.db.q("SELECT * FROM drafts WHERE kind='optimize'")
        assert [d["source_listing_id"] for d in drafts] == [103]
        client.post(f"/shops/{SHOP_ID}/sync")  # no duplicate suggestion
        assert len(app.state.db.q("SELECT * FROM drafts WHERE kind='optimize'")) == 1


def test_bulk_csv(client, etsy_api, generator):
    connect_shop(client)
    csv_data = "notes,price,quantity\nblue mug,20,2\ngreen mug,22,1\n,,\n"
    client.post(f"/shops/{SHOP_ID}/bulk", files={"csv_file": ("items.csv", csv_data, "text/csv")})
    drafts = client.app_db.q("SELECT * FROM drafts WHERE kind='new'")
    assert len(drafts) == 2
    assert json.loads(drafts[0]["fields"])["price"] == "20"


def test_password_gate(settings, generator, etsy_api):
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings.app_password = "pw"
    with TestClient(create_app(settings, generator=generator)) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        client.post("/login", data={"password": "nope"})
        assert client.get("/", follow_redirects=False).status_code == 303
        client.post("/login", data={"password": "pw"})
        assert client.get("/").status_code == 200


async def test_client_refreshes_expired_token(tmp_path):
    settings = Settings(etsy_api_key="k", database_path=":memory:")
    refreshed = []
    with respx.mock() as router:
        router.post("https://api.etsy.com/v3/public/oauth/token").mock(
            return_value=httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
        )
        route = router.get(f"{API}/users/me").mock(
            side_effect=[httpx.Response(401, json={"error": "invalid_token"}), httpx.Response(200, json={"user_id": 1})]
        )
        client = etsy.EtsyClient(settings, "old", "r1", expires_at=0, on_refresh=refreshed.append)
        assert (await client.get_me())["user_id"] == 1
        assert route.calls[-1].request.headers["authorization"] == "Bearer new"
        assert refreshed[0]["refresh_token"] == "r2"
