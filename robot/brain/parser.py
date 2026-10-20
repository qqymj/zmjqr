"""LLM 结构化输出解析：从 JSON 协议流中抽取 text 字段与表达元数据。

角色卡要求模型输出 {"text":..., "emotion":..., "intensity":..., "gesture":...}
（技术方案 §6.4），但流水线需要的是"能直接朗读的文本"。本模块负责两件事：

1. 流式剥离 JSON 包装，增量产出 text 内容给 TTS —— 保住"首句一到就开口"的
   低延迟优势（技术方案 §5.4）；
2. 结束时解析 emotion / intensity / gesture，供表达层驱动表情与动作。

模型未遵循协议时（离线桩、兜底话术）原样透传，不抛异常。
"""

from __future__ import annotations

import re

_TEXT_KEY = re.compile(r'"text"\s*:\s*"')
_META = {
    "emotion": re.compile(r'"emotion"\s*:\s*"([^"]+)"'),
    "gesture": re.compile(r'"gesture"\s*:\s*"([^"]+)"'),
    "intensity": re.compile(r'"intensity"\s*:\s*([0-9]*\.?[0-9]+)'),
}
_SIMPLE_ESCAPE = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
# seek 阶段保留的尾部长度，保证被 chunk 边界切断的 "text": " 仍能被识别
_KEEP = 24


class OutputExtractor:
    """把 LLM 的流式输出转换为可朗读文本。

    用法：feed() 取增量文本，finish() 收尾，meta 属性取表达元数据。
    """

    def __init__(self) -> None:
        self.raw = ""  # 完整原始输出，结束时用于解析元数据
        self.meta: dict = {}
        self._mode = "init"  # init -> json -> text -> tail（或 init -> plain）
        self._buf = ""
        self._out: list[str] = []
        self._in_text = False

    def feed(self, chunk: str) -> str:
        """喂入一个 token，返回本次可朗读的文本增量。"""
        if not chunk:
            return ""
        self.raw += chunk
        if self._mode != "tail":
            self._buf += chunk
        if self._mode == "init":
            self._decide()
        if self._mode == "json":
            self._seek()
        if self._mode == "text":
            self._scan_text()
        if self._mode == "plain":
            self._out.append(self._buf)
            self._buf = ""
        return self._drain()

    def finish(self) -> str:
        """流结束后的收尾，返回最后一段可朗读文本。"""
        if self._mode == "init":
            self._decide()
        if self._mode == "json":
            self._seek()
        if self._mode == "text":
            self._scan_text()
        if self._mode == "json" and not self._in_text:
            # 协议未命中：整体按纯文本朗读，避免机器人"说不出话"
            self._out.append(self.raw)
        elif self._buf:
            # text 未正常收尾（模型漏了收尾引号）时补上残留内容
            self._out.append(self._buf)
        self._buf = ""
        self._parse_meta()
        return self._drain()

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    def _decide(self) -> None:
        stripped = self._buf.lstrip()
        if not stripped:
            return  # 还只有空白，继续等
        self._buf = stripped
        self._mode = "json" if stripped[0] == "{" else "plain"

    def _seek(self) -> None:
        match = _TEXT_KEY.search(self._buf)
        if match is None:
            # 丢弃已确认无用部分，只留尾部等待后续 chunk 补齐
            if len(self._buf) > _KEEP:
                self._buf = self._buf[-_KEEP:]
            return
        self._buf = self._buf[match.end() :]
        self._in_text = True
        self._mode = "text"
        self._scan_text()

    def _scan_text(self) -> None:
        buf, i, n = self._buf, 0, len(self._buf)
        while i < n:
            char = buf[i]
            if char == "\\":
                if i + 1 >= n:
                    break  # 等下一块补齐转义
                nxt = buf[i + 1]
                if nxt == "u":
                    if i + 6 > n:
                        break  # \uXXXX 尚未收全
                    try:
                        self._out.append(chr(int(buf[i + 2 : i + 6], 16)))
                    except ValueError:
                        self._out.append("u" + buf[i + 2 : i + 6])
                    i += 6
                else:
                    self._out.append(_SIMPLE_ESCAPE.get(nxt, nxt))
                    i += 2
            elif char == '"':
                self._mode = "tail"
                i += 1
                break
            else:
                self._out.append(char)
                i += 1
        self._buf = "" if self._mode == "tail" else buf[i:]

    def _parse_meta(self) -> None:
        for key, pattern in _META.items():
            match = pattern.search(self.raw)
            if match is None:
                continue
            value = match.group(1)
            if key == "intensity":
                self.meta[key] = min(1.0, max(0.0, float(value)))
            else:
                self.meta[key] = value

    def _drain(self) -> str:
        if not self._out:
            return ""
        text = "".join(self._out)
        self._out.clear()
        return text
