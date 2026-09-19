from bs4 import BeautifulSoup


def sanitize_html(html: str) -> str:
    """Strip active content (scripts, styles) before text extraction.

    Implemented with BeautifulSoup instead of regexes so unquoted event
    handlers (e.g. <div onerror=alert(1)>) and nested markup can't slip
    through. Returns sanitized HTML safe for downstream parsing.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return str(soup)


def parse_html_sections(html: str) -> list[dict]:
    soup = BeautifulSoup(sanitize_html(html), "html.parser")

    sections = []
    heading_stack = []

    elements = soup.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "ol", "ul"]
    )
    if not elements:
        elements = soup.find_all(["div", "span"])
    if not elements:
        text = soup.get_text(" ", strip=True)
        if text:
            return [{"heading_path": [], "text": text}]
        return []

    for element in elements:
        if element.name.startswith("h"):
            level = int(element.name[1])
            heading = element.get_text(" ", strip=True)

            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()

            heading_stack.append((level, heading))

            current_path = [h for _, h in heading_stack]

        else:
            text = element.get_text(" ", strip=True)

            if not text:
                continue

            current_heading_path = [h for _, h in heading_stack]

            if (
                sections
                and sections[-1]["heading_path"] == current_heading_path
            ):
                sections[-1]["text"] += "\n" + text
            else:
                sections.append(
                    {
                        "heading_path": current_heading_path,
                        "text": text,
                    }
                )

    if not sections:
        text = soup.get_text(" ", strip=True)
        if text:
            return [{"heading_path": [], "text": text}]

    return sections


def find_split_position(
    text: str,
    start: int,
    target_end: int,
) -> int:
    end = min(target_end, len(text))

    newline_position = text.rfind("\n", start, end)

    if newline_position > start:
        return newline_position

    sentence_positions = [
        text.rfind(".", start, end),
        text.rfind("?", start, end),
        text.rfind("!", start, end),
    ]

    sentence_position = max(sentence_positions)

    if sentence_position > start:
        return sentence_position + 1

    whitespace_position = text.rfind(" ", start, end)

    if whitespace_position > start:
        return whitespace_position

    return end


def find_overlap_start(
    text: str,
    end: int,
    overlap: int,
) -> int:
    start = max(0, end - overlap)

    original_start = start

    while start < len(text) and not text[start].isspace():
        start += 1

    if start >= len(text):
        return original_start

    return start


def chunk_sections(
    sections: list[dict],
    chunk_size: int,
    overlap: int,
) -> list[dict]:

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")

    if overlap < 0:
        raise ValueError("overlap must not be negative")

    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    chunks = []

    for section in sections:
        text = section["text"]
        heading_path = section["heading_path"]

        if len(text) <= chunk_size:
            chunks.append(
                {
                    "text": text,
                    "heading_path": heading_path.copy(),
                }
            )
            continue

        start = 0

        while start < len(text):
            target_end = start + chunk_size

            end = find_split_position(
                text,
                start,
                target_end,
            )

            chunk_text = text[start:end].strip()

            if chunk_text:
                chunks.append(
                    {
                        "text": chunk_text,
                        "heading_path": heading_path.copy(),
                    }
                )

            if end >= len(text):
                break

            next_start = find_overlap_start(
                text,
                end,
                overlap,
            )

            if next_start <= start:
                next_start = end

            start = next_start

    return chunks


def chunk_article(
    article: dict,
    chunk_size: int,
    overlap: int,
) -> list[dict]:

    sections = parse_html_sections(article["text"])

    chunks = chunk_sections(
        sections,
        chunk_size=chunk_size,
        overlap=overlap,
    )

    result = []

    for index, chunk in enumerate(chunks):
        heading = " > ".join(chunk["heading_path"])

        metadata = {
            key: value
            for key, value in article.items()
            if key != "text"
        }

        chunk_data = {
            "text": (
                f"{heading}\n{chunk['text']}"
                if heading
                else chunk["text"]
            ),
            "metadata": {
                **metadata,
                "heading_path": chunk["heading_path"].copy(),
                "chunk_index": index,
            },
        }

        result.append(chunk_data)

    return result


def chunk_articles(
    articles: list[dict],
    chunk_size: int,
    overlap: int,
) -> list[dict]:

    all_chunks = []

    for article in articles:
        article_chunks = chunk_article(
            article,
            chunk_size=chunk_size,
            overlap=overlap,
        )

        all_chunks.extend(article_chunks)

    return all_chunks