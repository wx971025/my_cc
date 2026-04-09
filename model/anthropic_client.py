import os
from anthropic import Anthropic
from typing import cast


class AnthropicClient(Anthropic):
    def __init__(self, *args, **kwargs):
        api_key = os.environ.get("DMX_API_KEY")
        base_url = os.environ.get("DMX_BASE_URL")
        if not api_key or not base_url:
            raise ValueError("DMX_API_KEY or DMX_BASE_URL is not set")
        if "api_key" in kwargs:
            del kwargs["api_key"]
        if "base_url" in kwargs:
            del kwargs["base_url"]
        super().__init__(api_key=api_key, base_url=base_url, *args, **kwargs)

anthropic_client = cast(Anthropic, AnthropicClient())
