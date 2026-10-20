"""端侧 Embedding：ONNX Runtime 跑量化后的 bge-small-zh（技术方案 §6.1 / §6.5）。

技术方案主张 **Embedding 端侧优先**：模型 int8 量化后仅 ~23MB，CPU 单条推理毫秒级，
不依赖网络、不消耗额度，桌面场景事实量小、完全够用。

启用方式：``retrieval = "embedding"`` 且 ``embedding.provider = "local"``。
依赖可选，缺依赖或模型文件时上层会退回词面检索（§7.5 不哑原则）：

    pip install onnxruntime tokenizers
    python -m robot.memory.onnx_embedder --fetch --dir data/models/bge-small-zh-v1.5

检索时向量与事实向量都在此端侧计算，原始文本不出设备。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REPO = "Xenova/bge-small-zh-v1.5"
MODEL_FILES = ("tokenizer.json", "config.json", "onnx/model_quantized.onnx")


def _cls_pool_normalize(row: list[float]) -> list[float]:
    """bge 用首 token（CLS）作句向量，再做 L2 归一化以便余弦相似度。"""
    norm = math.sqrt(sum(x * x for x in row))
    if not norm:
        return [0.0] * len(row)
    return [x / norm for x in row]


def fetch_model(model_dir: str, repo: str = DEFAULT_REPO, endpoint: str | None = None) -> None:
    """从 HF 镜像下载模型文件到 model_dir（仅需在有网环境执行一次）。"""
    base = (endpoint or os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com").rstrip("/")
    root = Path(model_dir)
    for rel in MODEL_FILES:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        url = f"{base}/{repo}/resolve/main/{rel}"
        print(f"下载 {url}")
        with urllib.request.urlopen(url, timeout=120) as resp, open(dst, "wb") as out:
            out.write(resp.read())
        print(f"  -> {dst} ({dst.stat().st_size / 1e6:.1f}MB)")


class OnnxEmbedder:
    """端侧句向量编码器。与 OpenAICompatEmbedder 同接口：``await embed(text)``。"""

    def __init__(
        self,
        model_dir: str,
        max_length: int = 512,
        session: Any = None,
        tokenizer: Any = None,
    ) -> None:
        self.max_length = max_length
        if session is not None and tokenizer is not None:
            # 便于单测注入桩件，跳过可选依赖的加载
            self.session = session
            self.tokenizer = tokenizer
            return
        try:
            import onnxruntime
            from tokenizers import Tokenizer
        except ImportError as exc:  # 缺可选依赖时由上层降级到词面检索
            raise RuntimeError(
                "端侧向量检索需要安装 onnxruntime 与 tokenizers："
                "pip install onnxruntime tokenizers"
            ) from exc

        tokenizer_path = Path(model_dir) / "tokenizer.json"
        model_path = Path(model_dir) / "onnx" / "model_quantized.onnx"
        if not tokenizer_path.exists() or not model_path.exists():
            raise RuntimeError(
                f"未找到端侧模型文件，请先执行："
                f"python -m robot.memory.onnx_embedder --fetch --dir {model_dir}"
            )

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.session = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )

    def embed_sync(self, text: str) -> list[float]:
        """同步编码，供 asyncio.to_thread 调用，避免阻塞事件循环。"""
        import numpy as np

        encoding = self.tokenizer.encode(text)
        feeds = {
            "input_ids": np.array([encoding.ids], dtype=np.int64),
            "attention_mask": np.array([encoding.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([encoding.type_ids], dtype=np.int64),
        }
        hidden = self.session.run(None, feeds)[0]
        return _cls_pool_normalize([float(x) for x in hidden[0][0]])

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self.embed_sync, text)

    async def aclose(self) -> None:
        """释放推理会话（CPU 会话无异步资源，仅置空引用）。"""
        self.session = None


def _main() -> None:
    parser = argparse.ArgumentParser(description="端侧 embedding 模型下载工具")
    parser.add_argument("--fetch", action="store_true", help="从 HF 镜像下载模型")
    parser.add_argument("--dir", default="data/models/bge-small-zh-v1.5", help="模型目录")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="HF 仓库名")
    parser.add_argument("--endpoint", default=None, help="HF 镜像地址")
    args = parser.parse_args()
    if not args.fetch:
        parser.print_help()
        return
    fetch_model(args.dir, args.repo, args.endpoint)


if __name__ == "__main__":
    _main()
