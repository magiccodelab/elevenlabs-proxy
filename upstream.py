"""上游 ElevenLabs 调用层。

所有出站请求统一走：
- SOCKS5 代理（启动期已校验出口 IP）
- curl_cffi `impersonate` 复刻 Chrome 的 TLS / HTTP-2 / UA / sec-ch-ua 指纹
- 复刻 elevenlabs.io 的 Origin / Referer

token 由 auth.TokenManager 异步提供（自动续期）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from curl_cffi.requests import AsyncSession

from auth import token_manager
from config import settings

log = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "accept": "*/*",
    "accept-language": settings.accept_language,
    "origin": settings.elevenlabs_origin,
    "referer": settings.elevenlabs_origin + "/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}


async def _auth_headers() -> dict[str, str]:
    token = await token_manager.get_id_token()
    return {
        **_BROWSER_HEADERS,
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
    }


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    async with AsyncSession(
        impersonate=settings.impersonate,
        proxy=settings.proxy_url,
        timeout=60,
    ) as s:
        yield s


async def fetch_egress_ip() -> str:
    """通过代理访问 ipify，拿到出口 IP。"""
    async with _session() as s:
        r = await s.get("https://api.ipify.org?format=json", timeout=15)
        r.raise_for_status()
        return r.json().get("ip", "")


async def fetch_auth_account() -> dict:
    """调 /v1/auth-account 验证当前 token 在上游可用。"""
    headers = await _auth_headers()
    async with _session() as s:
        r = await s.get(
            f"{settings.elevenlabs_base_url}/v1/auth-account",
            headers=headers,
            timeout=20,
        )
        if r.status_code == 401:
            raise RuntimeError("ElevenLabs 拒绝当前 token (401)")
        r.raise_for_status()
        return r.json()


async def text_to_dialogue_stream(
    *,
    text: str,
    voice_id: str,
    model_id: str,
    stability: float,
) -> AsyncIterator[bytes]:
    """流式合成：POST /v1/text-to-dialogue/stream，逐 chunk 产出 mp3 字节。"""
    body = {
        "inputs": [{"text": text, "voice_id": voice_id}],
        "model_id": model_id,
        "settings": {"stability": stability},
    }
    headers = await _auth_headers()
    async with _session() as s:
        async with s.stream(
            "POST",
            f"{settings.elevenlabs_base_url}/v1/text-to-dialogue/stream",
            headers=headers,
            json=body,
            timeout=120,
        ) as r:
            if r.status_code != 200:
                err = b""
                async for c in r.aiter_content():
                    err += c
                    if len(err) > 4096:
                        break
                raise RuntimeError(
                    f"上游返回 {r.status_code}: {err.decode('utf-8', 'replace')[:500]}"
                )
            async for chunk in r.aiter_content():
                if chunk:
                    yield chunk
