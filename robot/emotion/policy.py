"""情感 → 表达策略映射（技术方案 §5.6）。

两条输入：

- 用户情绪（``emotion.detected``）：决定机器人共情时的灯效与表情；
- 模型自选表达（``expression.chosen``）：模型给出的 gesture/intensity 优先，
  覆盖角色卡里的默认值。

输出是"通道指令"（led 规格串 / gesture 名 / intensity），具体怎么点灯、怎么
动舵机由 expression 层实现。本模块只做纯映射，方便单测。
"""

from __future__ import annotations

from typing import Any

DEFAULT_LED = "soft_purple_breath"
DEFAULT_GESTURE = "still"
DEFAULT_INTENSITY = 0.5

# 状态 → 通道指令。倾听时 LED 明确亮起（§8.2 麦克风指示），思考时呼吸，
# 待机时收敛；SPEAKING 不在这里处理（由说话起伏 + 情绪表达驱动）。
STATE_POLICY: dict[str, dict[str, Any]] = {
    "LISTENING": {"led": "calm_cyan_solid", "emotion": "neutral"},
    "THINKING": {"led": "breathing_cyan", "emotion": "neutral"},
    "IDLE": {"led": DEFAULT_LED, "emotion": "neutral"},
    "PROACTIVE": {"led": DEFAULT_LED, "emotion": "neutral"},
}


def clamp(value: float) -> float:
    """把强度裁剪到 0~1，防止模型给出越界值。"""
    return min(1.0, max(0.0, float(value)))


def resolve_emotion(
    mood: str,
    expressions: dict[str, Any],
    *,
    led: str | None = None,
    gesture: str | None = None,
    intensity: float | None = None,
) -> dict[str, Any]:
    """把情绪解析成通道指令；模型显式给出的值优先于角色卡默认值。"""
    expr = expressions.get(mood) or expressions.get("neutral") or {}
    return {
        "emotion": mood,
        "led": led or expr.get("led", DEFAULT_LED),
        "gesture": gesture or expr.get("gesture", DEFAULT_GESTURE),
        "intensity": clamp(DEFAULT_INTENSITY if intensity is None else intensity),
    }


def resolve_state(state: str) -> dict[str, Any] | None:
    """状态 → 通道指令；无对应策略时返回 None（如 SPEAKING 交给其它通道）。"""
    plan = STATE_POLICY.get(state)
    if plan is None:
        return None
    return {"gesture": DEFAULT_GESTURE, "intensity": DEFAULT_INTENSITY, **plan}
