from barq_ai_support.ingestion.chunker import (
    parse_html_sections,
    chunk_sections,
)


def test_parse_html_sections():
    html = """
    <h2>Cause</h2>
    <p>Password is not working.</p>

    <h2>Resolution</h2>
    <p>Reset the password.</p>
    """

    sections = parse_html_sections(html)

    assert len(sections) == 2

    assert sections[0]["heading_path"] == ["Cause"]
    assert sections[0]["text"] == "Password is not working."

    assert sections[1]["heading_path"] == ["Resolution"]
    assert sections[1]["text"] == "Reset the password."


def test_heading_hierarchy():
    html = """
    <h2>Cause</h2>
    <h3>Password Issues</h3>
    <p>Password has expired.</p>

    <h3>Authentication Issues</h3>
    <p>Authentication failed.</p>
    """

    sections = parse_html_sections(html)

    assert sections[0]["heading_path"] == [
        "Cause",
        "Password Issues",
    ]

    assert sections[1]["heading_path"] == [
        "Cause",
        "Authentication Issues",
    ]


def test_large_section_is_split():
    sections = [
        {
            "heading_path": ["Resolution"],
            "text": "A" * 1000,
        }
    ]

    chunks = chunk_sections(
        sections,
        chunk_size=500,
        overlap=50,
    )

    assert len(chunks) == 3
    assert len(chunks[0]["text"]) == 500
    assert len(chunks[1]["text"]) == 500


def test_invalid_chunk_size():
    sections = [
        {
            "heading_path": ["Test"],
            "text": "Hello",
        }
    ]

    try:
        chunk_sections(
            sections,
            chunk_size=0,
            overlap=0,
        )
        assert False
    except ValueError:
        assert True