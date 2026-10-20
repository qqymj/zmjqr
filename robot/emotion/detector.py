"""M0 情感判定桩：关键词规则；后续替换为文本分类模型 + 语音韵律融合。"""

from __future__ import annotations

# 判定顺序即优先级：负面情绪优先识别，避免被"开心"等词误吞
MOOD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sad": ("累", "难过", "烦", "压力", "emo", "不想", "难受", "委屈"),
    "anxious": ("焦虑", "紧张", "害怕", "担心", "慌"),
    "angry": ("生气", "气死", "讨厌", "烦死", "火大"),
    "tired": ("困", "想睡", "熬夜", "加班", "好累"),
    "excited": ("激动", "太棒", "期待", "冲了"),
    "happy": ("开心", "高兴", "棒", "太好了", "哈哈", "爽"),
}


class EmotionDetector:
    def detect(self, text: str) -> str:
        for mood, keywords in MOOD_KEYWORDS.items():
            if any(k in text for k in keywords):
                return mood
        return "neutral"
