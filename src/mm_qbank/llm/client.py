from __future__ import annotations

import base64
from typing import Any

from openai import OpenAI


class OpenAICompatClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float = 180.0,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("缺少 API 密钥：多模态使用 VLM_API_KEY，纯文本使用 LLM_API_KEY（见 .env.example）")
        kw: dict[str, Any] = {"api_key": api_key, "base_url": base_url, "timeout": timeout}
        if default_headers:
            kw["default_headers"] = default_headers
        self._client = OpenAI(**kw)

    def chat_text_json(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        timeout: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            kwargs2 = {**kwargs, "response_format": {"type": "json_object"}}
            resp = self._client.chat.completions.create(**kwargs2)
        except (TypeError, Exception):
            resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0].message
        return (choice.content or "").strip()

    def chat_text_plain(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int = 64,
        timeout: float | None = 45.0,
    ) -> str:
        """纯文本补全，不要求 JSON；用于配置自检等。"""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0].message
        return (choice.content or "").strip()

    def chat_vision(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        image_bytes: bytes,
        content_type: str = "image/png",
        temperature: float = 0.1,
        timeout: float = 300.0,
        response_format_json: bool = False,
    ) -> str:
        """
        多模态：单张图片 + 文本。使用 ``data:`` URL 编码，便于 OpenAI 兼容类网关。

        注意：模型须支持视觉；费用与图尺寸、调用量相关，建议配合预处理限制长边。
        ``response_format_json=True`` 时尽量请求单 JSON 对象；若网关不支持会退回无该参数的请求。
        """
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        data_url = f"data:{content_type};base64,{b64}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": timeout,
        }
        if response_format_json:
            try:
                kwargs2 = {**kwargs, "response_format": {"type": "json_object"}}
                resp = self._client.chat.completions.create(**kwargs2)
            except (TypeError, Exception):
                resp = self._client.chat.completions.create(**kwargs)
        else:
            resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0].message
        return (choice.content or "").strip()
