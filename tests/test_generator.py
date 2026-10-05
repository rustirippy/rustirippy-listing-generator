import json

import anthropic
import httpx2

from app.config import Settings
from app.generator import ClaudeGenerator, ImageInput, ShopContext, taxonomy_candidates

COPY = {
    "title": "Mug: speckled: stoneware",
    "description": "A mug.",
    "tags": ["Handmade Mug", "handmade mug", "pottery gift for coffee lovers"],
    "materials": ["stoneware!"],
    "taxonomy_id": 999,
    "seller_notes": "Add size.",
}


def test_claude_generator_request_and_cleanup():
    settings = Settings(claude_model="claude-opus-5-5", claude_effort="medium")
    ctx = ShopContext(shop_name="ClayCo", taxonomy_options=[{"id": 1002, "path": "Home > Mugs", "level": 2}])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(COPY)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    # The SDK uses httpx2, so mock at the transport level.
    sdk = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    out = ClaudeGenerator(settings, sdk).generate_new(ctx, "speckled mug", [ImageInput(b"img", "image/png")])

    body = json.loads(requests[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert body["messages"][0]["content"][0]["type"] == "image"
    assert "1002: Home > Mugs" in body["messages"][0]["content"][-1]["text"]

    assert out["title"] == "Mug: speckled stoneware"
    assert out["tags"] == ["handmade mug"]  # deduped; the over-long tag is dropped
    assert out["materials"] == ["stoneware"]
    assert out["taxonomy_id"] is None  # 999 wasn't one of the offered categories


def test_taxonomy_candidates_prefers_matches():
    tax = [
        {"id": 1, "path": "Home & Living > Kitchen > Mugs", "level": 2},
        {"id": 2, "path": "Jewelry > Rings", "level": 1},
        {"id": 3, "path": "Art > Prints", "level": 1},
    ]
    ids = [t["id"] for t in taxonomy_candidates(tax, "blue ceramic mug", prefer_ids={3})]
    assert ids[0] == 3 or ids[0] == 1
    assert 2 not in ids and set(ids) == {1, 3}
