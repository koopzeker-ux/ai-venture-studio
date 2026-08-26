from app.services.normalize import normalize_raw_signal


def test_whitespace_is_collapsed():
    raw = {
        "source": "  fictitious_source_alpha  ",
        "source_url": "https://example.com/1",
        "title": "  Too   many\n\n  spaces  ",
        "content": "Line one\n\nLine   two",
        "metadata": {},
    }
    result = normalize_raw_signal(raw)
    assert result["source"] == "fictitious_source_alpha"
    assert result["title"] == "Too many spaces"
    assert result["content"] == "Line one Line two"


def test_html_noise_is_removed():
    raw = {
        "source": "fictitious_source_alpha",
        "source_url": "https://example.com/2",
        "title": "<b>Bold title</b>",
        "content": "<p>Some <a href='x'>content</a> here</p>",
        "metadata": {},
    }
    result = normalize_raw_signal(raw)
    assert "<" not in result["title"]
    assert "<" not in result["content"]
    assert "Bold title" in result["title"]
    assert "content" in result["content"]


def test_markdown_noise_is_removed():
    raw = {
        "source": "fictitious_source_beta",
        "source_url": "https://example.com/3",
        "title": "# A **bold** _title_",
        "content": "See [this tool](https://tool.example) for more `code` info",
        "metadata": {},
    }
    result = normalize_raw_signal(raw)
    assert "#" not in result["title"]
    assert "*" not in result["title"]
    assert "_" not in result["title"]
    assert "[" not in result["content"]
    assert "`" not in result["content"]
    assert "this tool" in result["content"]


def test_metadata_engagement_score_and_published_at_are_coerced():
    raw = {
        "source": "fictitious_source_alpha",
        "source_url": "https://example.com/4",
        "title": "t",
        "content": "c",
        "metadata": {"engagement_score": "42", "published_at": "1700000000.5"},
    }
    result = normalize_raw_signal(raw)
    assert result["metadata"]["engagement_score"] == 42
    assert result["metadata"]["published_at"] == 1700000000.5


def test_metadata_invalid_values_become_none():
    raw = {
        "source": "fictitious_source_alpha",
        "source_url": "https://example.com/5",
        "title": "t",
        "content": "c",
        "metadata": {"engagement_score": "not-a-number", "published_at": "also-not"},
    }
    result = normalize_raw_signal(raw)
    assert result["metadata"]["engagement_score"] is None
    assert result["metadata"]["published_at"] is None


def test_missing_fields_default_gracefully():
    raw = {"source": None, "source_url": None, "title": None, "content": None, "metadata": None}
    result = normalize_raw_signal(raw)
    assert result == {
        "source": "",
        "source_url": "",
        "title": "",
        "content": "",
        "metadata": {
            "engagement_score": None,
            "published_at": None,
            "is_launch": False,
            "subreddit": None,
            "external_id": None,
        },
    }


def test_subreddit_and_external_id_pass_through_when_present():
    raw = {
        "source": "reddit",
        "source_url": "https://example.com/6",
        "title": "t",
        "content": "c",
        "metadata": {"subreddit": "  smallbusiness  ", "external_id": "  t3_abc123  "},
    }
    result = normalize_raw_signal(raw)
    assert result["metadata"]["subreddit"] == "smallbusiness"
    assert result["metadata"]["external_id"] == "t3_abc123"


def test_subreddit_and_external_id_missing_or_invalid_stays_none():
    for value in [None, "", "   ", 123, [], {}]:
        raw = {
            "source": "fictitious_source_alpha",
            "source_url": "https://example.com/7",
            "title": "t",
            "content": "c",
            "metadata": {"subreddit": value, "external_id": value},
        }
        result = normalize_raw_signal(raw)
        assert result["metadata"]["subreddit"] is None, f"expected None for {value!r}"
        assert result["metadata"]["external_id"] is None, f"expected None for {value!r}"


def test_is_launch_true_values_are_coerced_to_bool_true():
    for value in [True, "true", "True", "1", "yes", 1]:
        raw = {
            "source": "fictitious_source_alpha",
            "source_url": "https://example.com/launch",
            "title": "t",
            "content": "c",
            "metadata": {"is_launch": value},
        }
        result = normalize_raw_signal(raw)
        assert result["metadata"]["is_launch"] is True, f"expected True for {value!r}"


def test_is_launch_false_missing_or_invalid_defaults_to_false():
    for value in [False, "false", "0", "", 0, None, "garbage", [], {}]:
        raw = {
            "source": "fictitious_source_alpha",
            "source_url": "https://example.com/no-launch",
            "title": "t",
            "content": "c",
            "metadata": {"is_launch": value},
        }
        result = normalize_raw_signal(raw)
        assert result["metadata"]["is_launch"] is False, f"expected False for {value!r}"


def test_is_launch_missing_key_defaults_to_false():
    raw = {
        "source": "fictitious_source_alpha",
        "source_url": "https://example.com/no-key",
        "title": "t",
        "content": "c",
        "metadata": {},
    }
    result = normalize_raw_signal(raw)
    assert result["metadata"]["is_launch"] is False
