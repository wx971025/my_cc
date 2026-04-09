from typing import Any


def _block_to_dict(block: Any) -> dict | None:
    if isinstance(block, dict):
        return {k: v for k, v in block.items() if not str(k).startswith("_")}

    block_type = getattr(block, "type", None)
    if not block_type:
        return None

    if block_type == "text":
        return {"type": "text", "text": getattr(block, "text", "")}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "input": getattr(block, "input", {}) or {},
        }
    if block_type == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", None),
            "content": getattr(block, "content", ""),
        }

    # Fallback: keep only public attrs the SDK may expose.
    result = {"type": block_type}
    for key in ("id", "name", "input", "text", "tool_use_id", "content"):
        value = getattr(block, key, None)
        if value is not None:
            result[key] = value
    return result


def normalize_messages(messages: list) -> list:
    """Normalize message content into API-safe primitive structures."""
    cleaned = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        clean = {"role": role}

        if isinstance(content, str):
            clean["content"] = content
        elif isinstance(content, list):
            blocks = []
            for block in content:
                normalized = _block_to_dict(block)
                if normalized is not None:
                    blocks.append(normalized)
            clean["content"] = blocks
        else:
            clean["content"] = str(content)

        cleaned.append(clean)

    return cleaned


def extract_text(content: list) -> str:
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" and block.get("text"):
                texts.append(block["text"])
            continue
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts).strip()

