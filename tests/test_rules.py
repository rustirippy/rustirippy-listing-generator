from app import rules


def test_clean_title_limits_special_chars_and_length():
    title = rules.clean_title("Mug: blue & white: big & bold 100% 50%  " + "word " * 40)
    assert title.count(":") == 1 and title.count("&") == 1 and title.count("%") == 1
    assert len(title) <= rules.TITLE_MAX
    assert not title.endswith(" ")


def test_clean_tags_dedupes_trims_and_caps():
    tags = rules.clean_tags(["Handmade Mug", "handmade mug", "a very long tag that exceeds twenty", "mug!!"] + [f"t{i}" for i in range(20)])
    assert tags[0] == "handmade mug"
    assert "mug" in tags
    assert all(len(t) <= rules.TAG_MAX for t in tags)
    assert len(tags) == rules.TAGS_MAX
    assert len(set(tags)) == len(tags)


def test_clean_tags_accepts_comma_string():
    assert rules.clean_tags("a b, c d ,, e") == ["a b", "c d", "e"]


def test_validate_flags_missing_fields():
    issues = rules.validate({"title": "", "price": "0.1", "quantity": 0})
    joined = " ".join(issues)
    assert "Title is required" in joined
    assert "Price" in joined and "Quantity" in joined and "category" in joined


def test_validate_publish_needs_shipping():
    fields = {"title": "Mug", "description": "d", "price": "10", "quantity": 1, "taxonomy_id": 1,
              "who_made": "i_did", "when_made": "made_to_order", "type": "physical"}
    assert rules.validate(fields) == []
    assert any("shipping" in i for i in rules.validate(fields, for_publish=True))


def test_score_rewards_complete_listings():
    weak, tips = rules.score_listing({"title": "Mug", "tags": ["mug"], "description": "nice"})
    strong, _ = rules.score_listing({
        "title": "Speckled Stoneware Mug Handmade Coffee Cup",
        "tags": [f"tag phrase {i}" for i in range(13)],
        "description": "x" * 400,
        "materials": ["stoneware"],
    })
    assert weak < 60 <= strong
    assert tips
