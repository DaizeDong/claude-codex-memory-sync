"""Keep imported trigger metadata small while preserving full source templates."""
import re


_PAIRED_MARKUP = re.compile(
    r'<(?P<tag>example|examples|p|b|i|em|strong|code|span|div)'
    r'(?:\s+[\w:-]+\s*=\s*(?:"[^"]*"|\'[^\']*\'))*\s*>'
    r'(?P<text>.*?)</(?P=tag)\s*>', re.I | re.S)
_EXAMPLE_SECTION = re.compile(
    r'(?:^|\n)[ \t]*(?:#{1,6}[ \t]+)?examples?\s*:'
    r'|(?<=[.!?])[ \t]+examples?\s*:'
    r'|(?:^|\n)[ \t]*<example\s*>', re.I)


def normalize_description(value: str, *, fallback: str, limit: int = 400) -> str:
    """Shorten structural example sections while retaining literal trigger text."""
    text = _EXAMPLE_SECTION.split(value, maxsplit=1)[0]
    while _PAIRED_MARKUP.search(text):
        text = _PAIRED_MARKUP.sub(lambda match: match['text'], text)
    text = re.sub(r'<br\s*/>', ' ', text, flags=re.I)
    text = text.replace("<", " less than ").replace(">", " greater than ")
    text = " ".join(text.split()).strip() or fallback
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    sentences = list(re.finditer(r"[.!?](?:\s|$)", prefix))
    if sentences:
        return prefix[:sentences[-1].start() + 1]
    return prefix.rsplit(" ", 1)[0] if " " in prefix else prefix
