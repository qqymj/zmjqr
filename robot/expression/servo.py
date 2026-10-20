"""舵机动作通道（M5）。

技术方案 §4.3：SG90 舵机 x1~2 负责点头/摇头/歪头；§5.8：动作要与 TTS 播放
时间轴对齐（说话时轻微起伏）。

gesture 名 → 角度轨迹（``(角度°, 保持毫秒)`` 序列，0° 为水平位）。无硬件时
``ConsoleServo`` 打印轨迹预览；接舵机后换成驱动 GPIO/PWM 的实现即可。
"""

from __future__ import annotations

from typing import Protocol

# gesture -> 轨迹。角度为相对水平位的偏移，正为抬头、负为低头。
TRACKS: dict[str, list[tuple[int, int]]] = {
    "still": [],
    "nod": [(0, 90), (20, 130), (0, 90)],
    "slow_nod": [(0, 260), (12, 360), (0, 260)],
    "shake": [(0, 70), (-16, 140), (16, 140), (0, 70)],
    "tilt_head": [(0, 120), (16, 320), (0, 120)],
    "tilt_down": [(0, 160), (-18, 520), (0, 160)],
    "bounce": [(0, 80), (-10, 90), (0, 80), (-10, 90), (0, 80)],
}

GESTURE_CN = {
    "still": "静止",
    "nod": "点头",
    "slow_nod": "缓慢点头",
    "shake": "摇头",
    "tilt_head": "歪头",
    "tilt_down": "低头",
    "bounce": "轻跳",
}


def gesture_label(name: str) -> str:
    """gesture 名 → 中文标签，用于调试预览（/expr）。"""
    return GESTURE_CN.get(name, name)


def plan_gesture(name: str, intensity: float = 0.5) -> list[tuple[int, int]]:
    """按强度缩放摆幅（0.4~1.0 倍），避免情绪强时动作夸张到抖动。"""
    track = TRACKS.get(name, [])
    scale = 0.4 + 0.6 * min(1.0, max(0.0, float(intensity)))
    return [(round(angle * scale), hold) for angle, hold in track]


def track_duration_ms(track: list[tuple[int, int]]) -> int:
    return sum(hold for _angle, hold in track)


class ServoDriver(Protocol):
    async def move(self, name: str, track: list[tuple[int, int]]) -> None: ...

    async def set_speaking(self, speaking: bool) -> None: ...

    async def aclose(self) -> None: ...


class ConsoleServo:
    """终端预览：一次性动作打印轨迹；说话起伏只在开关切换时打印一次。"""

    def __init__(self) -> None:
        self.speaking = False

    async def move(self, name: str, track: list[tuple[int, int]]) -> None:
        if not track:  # 静止/未知动作没有可展示的轨迹，静默处理
            return
        path = "→".join(f"{angle}°" for angle, _hold in track)
        print(
            f"        [动作] {GESTURE_CN.get(name, name)} · "
            f"{path}（约 {track_duration_ms(track)}ms）"
        )

    async def set_speaking(self, speaking: bool) -> None:
        if speaking == self.speaking:
            return
        self.speaking = speaking
        print(f"        [动作] 说话起伏 {'开启' if speaking else '结束'}")

    async def aclose(self) -> None:
        pass


class NullServo:
    async def move(self, name: str, track: list[tuple[int, int]]) -> None:
        pass

    async def set_speaking(self, speaking: bool) -> None:
        pass

    async def aclose(self) -> None:
        pass
