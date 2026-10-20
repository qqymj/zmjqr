"""表达层控制器（M5）：把情绪 / 状态 / 模型指令分发到三个通道。

技术方案 §5.8 的落地：

- 订阅事件驱动（``state.changed`` / ``emotion.detected`` / ``expression.chosen``
  / ``tts.playing``），订阅关系保持不变，便于单测与替换；
- 屏幕表情由独立协程驱动眨眼；
- 舵机与 TTS 时间轴对齐：进入说话时开启轻微起伏，回到待机时结束；
- LED 支持 solid / breath / pulse / wave（见 ``led.parse_spec``）。

三个通道默认用终端实现（ConsoleLed / ConsoleFace / ConsoleServo），换真实
硬件只需在 ``build_expression`` 里替换驱动，控制器逻辑不动。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..core.eventbus import EventBus
from ..emotion import policy
from .face import ConsoleFace, FaceDriver, NullFace
from .led import ConsoleLed, LedDriver, NullLed
from .servo import ConsoleServo, NullServo, ServoDriver, plan_gesture

_BACKENDS: dict[str, dict[str, Any]] = {
    "led": {"console": ConsoleLed, "none": NullLed},
    "face": {"console": ConsoleFace, "none": NullFace},
    "servo": {"console": ConsoleServo, "none": NullServo},
}


def _driver(kind: str, backend: str):
    table = _BACKENDS[kind]
    return table.get(backend, table["console"])()


def build_expression(bus: EventBus, cfg: dict, persona_cfg: dict) -> ExpressionController:
    """按 ``[expression]`` 配置组装表达层；未知后端一律回落到终端实现。"""
    exp_cfg = cfg.get("expression", {})
    return ExpressionController(
        bus,
        persona_cfg,
        led=_driver("led", exp_cfg.get("led", "console")),
        face=_driver("face", exp_cfg.get("screen", "console")),
        servo=_driver("servo", exp_cfg.get("servo", "console")),
        blink_interval_s=exp_cfg.get("blink_interval_s", 4.0),
    )


class ExpressionController:
    def __init__(
        self,
        bus: EventBus,
        persona_cfg: dict[str, Any],
        led: LedDriver | None = None,
        face: FaceDriver | None = None,
        servo: ServoDriver | None = None,
        blink_interval_s: float = 4.0,
    ) -> None:
        self.bus = bus
        self.expressions = persona_cfg.get("expressions", {})
        self.led = led or ConsoleLed()
        self.face = face or ConsoleFace()
        self.servo = servo or ConsoleServo()
        self.blink_interval_s = blink_interval_s
        self._blink_task: asyncio.Task | None = None
        bus.on("state.changed", self._on_state)
        bus.on("emotion.detected", self._on_emotion)
        bus.on("expression.chosen", self._on_chosen)
        bus.on("tts.playing", self._on_tts_playing)

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #

    async def _on_state(self, payload: dict) -> None:
        state = payload["state"]
        print(f"        [状态] {state}")
        self._ensure_blink()
        if state == "SPEAKING":
            await self.servo.set_speaking(True)
            return
        if state in ("IDLE", "PROACTIVE"):
            await self.servo.set_speaking(False)
        plan = policy.resolve_state(state)
        if plan is not None:
            await self._apply(plan)

    async def _on_emotion(self, payload: dict) -> None:
        """用户情绪 → 机器人的共情表达（灯效 + 表情，不急着做动作）。"""
        self._ensure_blink()
        plan = policy.resolve_emotion(payload["emotion"], self.expressions)
        await self._apply(plan)

    async def _on_chosen(self, payload: dict) -> None:
        """模型自选表达（§5.8）：led/gesture 优先，并演一次动作。"""
        mood = payload.get("emotion") or "neutral"
        plan = policy.resolve_emotion(
            mood,
            self.expressions,
            gesture=payload.get("gesture"),
            intensity=payload.get("intensity"),
        )
        await self._apply(plan)
        await self.servo.move(plan["gesture"], plan_gesture(plan["gesture"], plan["intensity"]))

    async def _on_tts_playing(self, payload: dict) -> None:
        """TTS 开始播报 → 舵机轻微起伏，与说话时间轴对齐（§5.8）。"""
        await self.servo.set_speaking(bool(payload.get("speaking", True)))

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #

    async def _apply(self, plan: dict) -> None:
        await self.led.show(plan["led"], plan["intensity"])
        await self.face.render(plan["emotion"])

    def _ensure_blink(self) -> None:
        """首个事件到达时启动眨眼协程（此时一定在事件循环里）。"""
        if self.blink_interval_s <= 0 or self._blink_task is not None:
            return
        self._blink_task = asyncio.create_task(self._blink_loop())

    async def _blink_loop(self) -> None:
        while True:
            await asyncio.sleep(self.blink_interval_s)
            await self.face.blink()

    async def aclose(self) -> None:
        if self._blink_task is not None:
            self._blink_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._blink_task
            self._blink_task = None
        await self.led.aclose()
        await self.face.aclose()
        await self.servo.aclose()
