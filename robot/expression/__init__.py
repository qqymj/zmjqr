"""表达层：屏幕表情、LED 灯效、舵机动作（M5）。

- ``led`` / ``face`` / ``servo``：三个通道的驱动协议 + 终端实现；
- ``policy``（在 ``robot.emotion``）：情绪/状态 → 通道指令的纯映射；
- ``controller``：订阅事件、分发指令的编排层（换硬件只换驱动）。
"""

from .controller import ExpressionController, build_expression
from .face import ConsoleFace, FaceDriver, NullFace, frame_for
from .led import ConsoleLed, LedDriver, NullLed, describe, parse_spec
from .servo import ConsoleServo, NullServo, ServoDriver, gesture_label, plan_gesture

__all__ = [
    "ConsoleFace",
    "ConsoleLed",
    "ConsoleServo",
    "ExpressionController",
    "FaceDriver",
    "LedDriver",
    "NullFace",
    "NullLed",
    "NullServo",
    "ServoDriver",
    "build_expression",
    "describe",
    "frame_for",
    "gesture_label",
    "parse_spec",
    "plan_gesture",
]
