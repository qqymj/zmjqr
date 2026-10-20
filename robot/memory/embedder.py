"""Embedding 客户端（OpenAI 兼容 /embeddings 接口）。

技术方案 §6.5 主张 Embedding 端侧优先，因此本项目默认不依赖外部 embedding；
只有显式配置并具备额度时才启用，用于更准的长期记忆检索。
"""

from __future__ import annotations

try:
    import httpx
except ImportError:  # 未安装 httpx 时仍可使用词面检索
    httpx = None  # type: ignore[assignment]


def _ensure_httpx() -> None:
    if httpx is None:
        raise RuntimeError("使用向量检索需要安装 httpx：uv sync")


class OpenAICompatEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 20.0,
        transport=None,
    ) -> None:
        _ensure_httpx()
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport)

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.post(
            f"{self.base_url}/embeddings", json={"model": self.model, "input": text}
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    async def aclose(self) -> None:
        await self._client.aclose()
