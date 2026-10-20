"""人格（角色卡）渲染：把 persona 配置组装成 System Prompt。

组装的稳定性顺序见技术方案 §6.4，便于后续做 KV Cache 预热。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

OUTPUT_PROTOCOL = (
    "你必须以 JSON 输出，格式："
    '{"text":"给用户的话","emotion":"happy|sad|angry|anxious|tired|neutral|excited",'
    '"intensity":0.0~1.0,"gesture":"nod|shake|tilt_head|still"}。'
    "只输出 JSON，不要多余解释。text 中不要出现动作描述。"
)


class Persona:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.get("name", "小伴")

    def greeting(self, hour: int | None = None) -> str:
        """按时间段返回开场白（hour 可显式传入，便于测试）。"""
        h = datetime.now().hour if hour is None else hour
        if 5 <= h < 12:
            key = "morning"
        elif 12 <= h < 18:
            key = "afternoon"
        elif 18 <= h < 23:
            key = "evening"
        else:
            key = "night"
        greetings = self.cfg.get("greetings", {})
        return greetings.get(key, "我来啦，今天想聊点什么？")

    def system_prompt(self) -> str:
        """[1] 角色卡定义（固定段）。"""
        style = self.cfg.get("speaking_style", {})
        traits = "、".join(self.cfg.get("traits", []))
        avoid = "、".join(style.get("avoid", []))
        return (
            f"你是「{self.name}」，{self.cfg.get('role', '一个温暖的伙伴')}。"
            f"性格：{traits}。"
            f"说话风格：{style.get('sentence_length', '简短自然')}，"
            f"避免{avoid}。"
        )

    def build(
        self,
        user_text: str,
        history: list[dict],
        memory: list[str],
        mood: str,
        recent: list[str] | None = None,
    ) -> list[dict]:
        """组装完整 messages：稳定段在前，易变段在后。"""
        system_parts = [self.system_prompt(), OUTPUT_PROTOCOL]
        if memory:
            system_parts.append("关于用户你知道的事：" + "；".join(memory))
        if recent:
            system_parts.append("最近几天你们聊过：" + "；".join(recent))
        system_parts.append(f"当前用户情绪倾向：{mood}")

        return [
            {"role": "system", "content": "\n".join(system_parts)},
            *history,
            {"role": "user", "content": user_text},
        ]
