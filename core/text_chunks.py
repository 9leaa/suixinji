"""Split long text for delivery without truncating any task or changing numbers."""


def text_chunks(text: str, max_bytes: int = 6000) -> list[str]:
    chunks, pending, size = [], [], 0
    for char in text:
        width = len(char.encode("utf-8"))
        if pending and size + width > max_bytes:
            chunks.append("".join(pending))
            pending, size = [], 0
        pending.append(char)
        size += width
    if pending or not chunks:
        chunks.append("".join(pending))
    return chunks
