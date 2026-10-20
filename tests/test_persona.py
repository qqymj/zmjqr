"""Persona 开场白单测：验证时间段选择与缺省回退。"""

from __future__ import annotations

import unittest

from robot.brain.persona import Persona

GREETINGS = {"morning": "M", "afternoon": "A", "evening": "E", "night": "N"}


class TestPersonaGreeting(unittest.TestCase):
    def setUp(self) -> None:
        self.persona = Persona({"name": "小伴", "greetings": GREETINGS})

    def test_picks_by_daypart(self) -> None:
        cases = {
            5: "M",
            8: "M",
            11: "M",
            12: "A",
            17: "A",
            18: "E",
            22: "E",
            23: "N",
            2: "N",
            4: "N",
        }
        for hour, expected in cases.items():
            with self.subTest(hour=hour):
                self.assertEqual(self.persona.greeting(hour), expected)

    def test_fallback_when_greetings_missing(self) -> None:
        persona = Persona({"name": "小伴"})
        self.assertTrue(persona.greeting(9))


if __name__ == "__main__":
    unittest.main()
