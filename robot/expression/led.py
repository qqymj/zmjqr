"""LED 灯效通道（M5）。

技术方案 §5.8：LED 支持 4 种基础模式 ``solid / breath / pulse / wave``。
角色卡里的 led 字段是"颜色 + 模式"的拼接串（如 ``warm_orange_pulse``），
本模块负责把它拆成 (颜色, 模式)，再交给具体驱动。

无硬件时用 ``ConsoleLed`` 在终端打印预览；接灯环（WS2812 等）后只需换一个
实现 ``LedDriver`` 的类，控制器逻辑不动。
"""

from __future__ import annotations

from typing import Protocol

MODES = ("solid", "breath", "pulse", "wave")
# 角色卡里用过的同义词，统一归一到基础模式
MODE_ALIAS = {"breathing": "breath", "breathe": "breath", "steady": "solid"}

MODE_CN = {"solid": "常亮", "breath": "呼吸", "pulse": "跳动", "wave": "流动"}

# 颜色词 -> (中文名, ANSI 前景色码)；未知颜色原样输出
PALETTE: dict[str, tuple[str, str]] = {
    "red": ("红", "91"),
    "orange": ("橙", "38;5;208"),
    "amber": ("琥珀", "38;5;214"),
    "yellow": ("黄", "93"),
    "green": ("绿", "92"),
    "cyan": ("青", "96"),
    "blue": ("蓝", "94"),
    "purple": ("紫", "95"),
    "pink": ("粉", "38;5;213"),
    "white": ("白", "97"),
}


def parse_spec(spec: str) -> tuple[str, str]:
    """把 ``"warm_orange_pulse"`` 拆成 ``("orange", "pulse")``。

    修饰词（warm / soft / dim / calm …）只影响观感，这里忽略；
    颜色与模式都缺省时回退到 (purple, breath)。
    """
    tokens = [token for token in str(spec).lower().split("_") if token]
    color, mode = "purple", "breath"
    has_color = False
    for token in tokens:
        if token in MODES:
            mode = token
        elif token in MODE_ALIAS:
            mode = MODE_ALIAS[token]
        elif token in PALETTE and not has_color:
            color, has_color = token, True
    return color, mode


def describe(spec: str) -> str:
    """``"warm_orange_pulse"`` -> ``"橙 · 跳动"``，用于调试预览（/expr）。"""
    color, mode = parse_spec(spec)
    name = PALETTE.get(color, (color, ""))[0]
    return f"{name} · {MODE_CN[mode]}"


class LedDriver(Protocol):
    async def show(self, spec: str, intensity: float = 0.5) -> None: ...

    async def aclose(self) -> None: ...


class ConsoleLed:
    """终端预览：状态无变化时不重复打印，避免刷屏。"""

    def __init__(self) -> None:
        self.state: tuple[str, str] | None = None

    async def show(self, spec: str, intensity: float = 0.5) -> None:
        color, mode = parse_spec(spec)
        if (color, mode) == self.state:
            return
        self.state = (color, mode)
        name, code = PALETTE.get(color, (color, "0"))
        print(f"        [灯效] \x1b[{code}m■\x1b[0m {name} · {MODE_CN[mode]}（{intensity:.1f}）")

    async def aclose(self) -> None:
        pass


class NullLed:
    async def show(self, spec: str, intensity: float = 0.5) -> None:
        pass

    async def aclose(self) -> None:
        pass
