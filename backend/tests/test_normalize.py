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
        "metadata": {"engagement_score": None, "published_at": None},
    }
