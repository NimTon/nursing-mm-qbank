from __future__ import annotations

import pytest

from mm_qbank.llm.client import OpenAICompatClient


def test_openai_client_requires_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAICompatClient(api_key=None, base_url="https://example.com/v1")
