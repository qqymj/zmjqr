"""M5 表达层单测：LED 规格解析、舵机轨迹、表情帧、策略映射、事件分发。"""

from __future__ import annotations

import asyncio
import unittest

from robot.core.eventbus import EventBus
from robot.emotion import policy
from robot.expression import (
    ExpressionController,
    NullFace,
    NullLed,
    NullServo,
    build_expression,
    describe,
    frame_for,
    parse_spec,
    plan_gesture,
)
from robot.main import EMOTIONS, _expression_table, _run_expr


class TestLedSpec(unittest.TestCase):
    def test_color_and_mode_split(self) -> None:
        self.assertEqual(parse_spec("warm_orange_pulse"), ("orange", "pulse"))
        self.assertEqual(parse_spec("soft_purple_breath"), ("purple", "breath"))

    def test_aliases_and_defaults(self) -> None:
        self.assertEqual(parse_spec("steady_cyan"), ("cyan", "solid"))
        self.assertEqual(parse_spec("breathing_cyan"), ("cyan", "breath"))
        self.assertEqual(parse_spec("warm_orange"), ("orange", "breath"))
        self.assertEqual(parse_spec("mystery"), ("purple", "breath"))


class TestServoPlan(unittest.TestCase):
    def test_intensity_scales_amplitude(self) -> None:
        weak = plan_gesture("nod", 0.0)
        strong = plan_gesture("nod", 1.0)
        self.assertLess(abs(weak[1][0]), abs(strong[1][0]))
        self.assertEqual([hold for _a, hold in weak], [hold for _a, hold in strong])

    def test_unknown_gesture_is_empty(self) -> None:
        self.assertEqual(plan_gesture("spinning", 1.0), [])


class TestFaceFrame(unittest.TestCase):
    def test_known_and_unknown_emotion(self) -> None:
        self.assertEqual(frame_for("happy"), "(^ ^ ◡)")
        self.assertEqual(frame_for("whatever"), frame_for("neutral"))

    def test_describe_spec(self) -> None:
        self.assertEqual(describe("warm_orange_pulse"), "橙 · 跳动")


class TestExpressionTable(unittest.TestCase):
    def test_lists_every_emotion_with_channels(self) -> None:
        table = _expression_table(_EXPRESSIONS)
        for mood in EMOTIONS:
            self.assertIn(mood, table)
        self.assertIn("橙 · 呼吸", table)  # happy 的灯效描述
        self.assertIn("点头", table)  # happy 的动作
        self.assertIn("(^ ^ ◡)", table)  # happy 的表情


class TestPolicy(unittest.TestCase):
    def test_model_values_override_config(self) -> None:
        cfg = {"sad": {"led": "dim_blue_breath", "gesture": "tilt_down"}}
        plan = policy.resolve_emotion("sad", cfg, gesture="nod", intensity=1.5)
        self.assertEqual(plan["gesture"], "nod")
        self.assertEqual(plan["led"], "dim_blue_breath")
        self.assertEqual(plan["intensity"], 1.0)  # 越界值被裁剪

    def test_unknown_emotion_falls_back_to_neutral(self) -> None:
        plan = policy.resolve_emotion("bogus", {"neutral": {"led": "soft_purple_breath"}})
        self.assertEqual(plan["led"], "soft_purple_breath")
        self.assertEqual(plan["gesture"], policy.DEFAULT_GESTURE)

    def test_state_policy(self) -> None:
        self.assertEqual(policy.resolve_state("LISTENING")["led"], "calm_cyan_solid")
        self.assertIsNone(policy.resolve_state("SPEAKING"))


class _RecLed:
    def __init__(self) -> None:
        self.specs: list[tuple[str, float]] = []

    async def show(self, spec: str, intensity: float = 0.5) -> None:
        self.specs.append((spec, intensity))

    async def aclose(self) -> None:
        pass


class _RecFace:
    def __init__(self) -> None:
        self.frames: list[str] = []
        self.blinks = 0

    async def render(self, emotion: str) -> None:
        self.frames.append(emotion)

    async def blink(self) -> None:
        self.blinks += 1

    async def aclose(self) -> None:
        pass


class _RecServo:
    def __init__(self) -> None:
        self.moves: list[tuple[str, list]] = []
        self.speaking: list[bool] = []

    async def move(self, name: str, track: list) -> None:
        self.moves.append((name, track))

    async def set_speaking(self, speaking: bool) -> None:
        self.speaking.append(speaking)

    async def aclose(self) -> None:
        pass


_EXPRESSIONS = {
    "sad": {"led": "dim_blue_breath", "gesture": "tilt_down"},
    "happy": {"led": "warm_orange", "gesture": "nod"},
    "neutral": {"led": "soft_purple_breath", "gesture": "still"},
}


class TestController(unittest.IsolatedAsyncioTestCase):
    def _make(self, blink_interval_s: float = 0.0) -> ExpressionController:
        return ExpressionController(
            EventBus(),
            {"expressions": _EXPRESSIONS},
            led=_RecLed(),
            face=_RecFace(),
            servo=_RecServo(),
            blink_interval_s=blink_interval_s,
        )

    async def test_user_mood_drives_led_and_face(self) -> None:
        ctrl = self._make()
        await ctrl.bus.emit("emotion.detected", {"emotion": "sad", "text": "今天好累"})
        self.assertEqual(ctrl.led.specs[-1][0], "dim_blue_breath")
        self.assertEqual(ctrl.face.frames[-1], "sad")

    async def test_chosen_expression_plays_one_shot_gesture(self) -> None:
        ctrl = self._make()
        await ctrl.bus.emit(
            "expression.chosen", {"emotion": "happy", "intensity": 0.8, "gesture": "nod"}
        )
        self.assertEqual(ctrl.face.frames[-1], "happy")
        name, track = ctrl.servo.moves[-1]
        self.assertEqual(name, "nod")
        self.assertTrue(track)

    async def test_speaking_toggles_servo_oscillation(self) -> None:
        ctrl = self._make()
        await ctrl.bus.emit("state.changed", {"state": "SPEAKING"})
        self.assertEqual(ctrl.servo.speaking[-1], True)
        await ctrl.bus.emit("state.changed", {"state": "IDLE"})
        self.assertEqual(ctrl.servo.speaking[-1], False)
        self.assertEqual(ctrl.led.specs[-1][0], "soft_purple_breath")

    async def test_tts_playing_keeps_servo_speaking(self) -> None:
        ctrl = self._make()
        await ctrl.bus.emit("tts.playing", {"text": "你好", "speaking": True})
        self.assertEqual(ctrl.servo.speaking[-1], True)

    async def test_blink_loop_runs_and_stops(self) -> None:
        ctrl = self._make(blink_interval_s=0.01)
        self.assertIsNone(ctrl._blink_task)
        await ctrl.bus.emit("state.changed", {"state": "IDLE"})
        self.assertIsNotNone(ctrl._blink_task)  # 首个事件触发时懒启动
        await asyncio.sleep(0.03)
        self.assertGreaterEqual(ctrl.face.blinks, 1)
        await ctrl.aclose()
        self.assertIsNone(ctrl._blink_task)

    async def test_expr_command_triggers_expression(self) -> None:
        ctrl = self._make()
        await _run_expr(ctrl.bus, "/expr happy 0.9", _EXPRESSIONS)
        self.assertEqual(ctrl.face.frames[-1], "happy")
        self.assertEqual(ctrl.led.specs[-1][0], "warm_orange")
        self.assertEqual(ctrl.servo.moves[-1][0], "nod")

    async def test_expr_command_ignores_unknown_emotion(self) -> None:
        ctrl = self._make()
        await _run_expr(ctrl.bus, "/expr dancing", _EXPRESSIONS)
        self.assertEqual(ctrl.face.frames, [])


class TestBuildExpression(unittest.TestCase):
    def test_none_backend_uses_null_drivers(self) -> None:
        ctrl = build_expression(
            EventBus(),
            {"expression": {"screen": "none", "led": "none", "servo": "none"}},
            {},
        )
        self.assertIsInstance(ctrl.led, NullLed)
        self.assertIsInstance(ctrl.face, NullFace)
        self.assertIsInstance(ctrl.servo, NullServo)

    def test_default_backend_is_console(self) -> None:
        ctrl = build_expression(EventBus(), {}, {})
        self.assertIsNotNone(ctrl.led)
        self.assertIsNotNone(ctrl.face)
        self.assertIsNotNone(ctrl.servo)


class TestTtsEmitsPlaying(unittest.IsolatedAsyncioTestCase):
    async def test_play_broadcasts_tts_playing(self) -> None:
        from robot.speech.tts import ConsoleTTS

        bus = EventBus()
        seen: list[dict] = []

        async def on_playing(payload: dict) -> None:
            seen.append(payload)

        bus.on("tts.playing", on_playing)
        await ConsoleTTS(per_char_delay=0, bus=bus).play("你好")
        self.assertEqual(seen[0]["text"], "你好")


if __name__ == "__main__":
    unittest.main()
