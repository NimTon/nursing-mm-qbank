from __future__ import annotations

import base64
import io
import logging
from typing import Any

from openai import OpenAI

# 兼容网关（如 DashScope）对单张 image_url data-uri 的字节上限；略留余量避免等于边界仍被拒
_DATA_URI_ITEM_MAX_BYTES = 20971520
# 按 UTF-8 计量的整条 ``data:{mime};base64,...`` 长度上限（网关常见为 20MiB）
_DATA_URI_SAFE_OCTETS = _DATA_URI_ITEM_MAX_BYTES - 8192


def _estimate_data_uri_octets(image_bytes: bytes, content_type: str) -> int:
    """估算 ``data:{mime};base64,...`` 在 UTF-8 下的字节长度（与常见网关计量一致）。"""
    b64_len = ((len(image_bytes) + 2) // 3) * 4
    prefix = f"data:{content_type};base64,"
    return len(prefix.encode("utf-8")) + b64_len


def _shrink_image_for_data_uri(image_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    """
    将图片压到网关允许的 data-uri 体积（``_DATA_URI_SAFE_OCTETS``）以下；必要时转 JPEG 并缩放。
    返回 ``(新字节, MIME 类型)``。
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise ValueError(
            "单张图片超过网关 data-uri 大小上限，请安装 Pillow（PIL）以便自动压缩，或缩小原图后再试。"
        ) from e

    log = logging.getLogger(__name__)
    bio = io.BytesIO(image_bytes)
    im = Image.open(bio)
    im = im.convert("RGB")

    quality = 88
    scale = 1.0
    last_jpeg: bytes | None = None

    for _ in range(28):
        w, h = im.size
        if scale < 1.0:
            nw = max(320, int(w * scale))
            nh = max(320, int(h * scale))
            if (nw, nh) != (w, h):
                frame = im.resize((nw, nh), Image.Resampling.LANCZOS)
            else:
                frame = im
        else:
            frame = im

        out = io.BytesIO()
        frame.save(out, format="JPEG", quality=quality, optimize=True)
        last_jpeg = out.getvalue()
        mime = "image/jpeg"
        if _estimate_data_uri_octets(last_jpeg, mime) <= _DATA_URI_SAFE_OCTETS:
            if last_jpeg != image_bytes or mime != content_type:
                log.warning(
                    "VLM 输入图已压缩为 JPEG（约 %s KB）以满足网关单图 data-uri 上限",
                    len(last_jpeg) // 1024,
                )
            return last_jpeg, mime

        scale *= 0.86
        quality = max(52, quality - 7)

    raise ValueError(
        "图片无法在网关单图大小限制内压缩到可接受范围，请将原图长边缩小或降低分辨率后重试。"
    )


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
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = int(max_tokens)
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
        if _estimate_data_uri_octets(image_bytes, content_type) > _DATA_URI_SAFE_OCTETS:
            image_bytes, content_type = _shrink_image_for_data_uri(image_bytes, content_type)
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
