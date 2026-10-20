"""小伴 · 程序入口。

模拟模式（无硬件，用终端模拟唤醒与对话）：
    python3 -m robot.main --sim            # 接真实 LLM（按 config.toml 的 llm 配置）
    python3 -m robot.main --sim --fake     # 用离线桩模型，不联网
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from robot.brain.llm import EchoLLM, OllamaLLM, OpenAICompatLLM
from robot.brain.persona import Persona
from robot.brain.pipeline import DialoguePipeline
from robot.brain.router import LLMRouter
from robot.core.config import load_config
from robot.core.eventbus import EventBus
from robot.core.fsm import DialogueFSM, State
from robot.core.scheduler import Scheduler
from robot.emotion import policy
from robot.emotion.detector import EmotionDetector
from robot.expression import build_expression, describe, frame_for, gesture_label
from robot.memory import (
    FactExtractor,
    MemoryStore,
    NullMemory,
    OnnxEmbedder,
    OpenAICompatEmbedder,
    ShortTermMemory,
)
from robot.services import BriefingService, CalendarService, NewsService, WeatherService
from robot.speech.tts import ConsoleTTS


def build_llm(cfg: dict, bus: EventBus, fake: bool, model_override: str | None = None):
    """按配置组装 LLM：云端优先 + 本地降级；fake=True 时用离线桩。"""
    if fake:
        return EchoLLM(char_delay=cfg["app"]["sim_char_delay"])

    llm_cfg = cfg["llm"]
    local_cfg = llm_cfg["local"]
    cloud_cfg = llm_cfg["cloud"]

    local = OllamaLLM(local_cfg["base_url"], local_cfg["model"], local_cfg["timeout_s"])
    api_key = os.environ.get(cloud_cfg.get("api_key_env", "LLM_API_KEY"), "")
    cloud = OpenAICompatLLM(
        cloud_cfg["base_url"],
        model_override or cloud_cfg["model"],
        api_key,
        cloud_cfg["timeout_s"],
        extra_body=cloud_cfg.get("extra"),
    )
    if llm_cfg.get("provider", "cloud") == "local":
        primary, fallback = local, cloud
    else:
        primary, fallback = cloud, local

    if not api_key and primary is cloud:
        env_name = cloud_cfg.get("api_key_env", "LLM_API_KEY")
        print(f"[提示] 未检测到云端 API Key（环境变量 {env_name}），首轮将直接降级到本地。")

    return LLMRouter(bus, primary, fallback, llm_cfg.get("first_token_timeout_s", 3.0))


def _build_embedder(emb_cfg: dict):
    """按 provider 组装句向量编码器；不可用时返回 None（退回词面检索）。"""
    provider = emb_cfg.get("provider", "local")
    if provider == "openai":
        cfg = emb_cfg.get("openai", {})
        api_key = os.environ.get(cfg.get("api_key_env", "LLM_API_KEY"), "")
        if not api_key:
            print("[提示] 未配置 embedding 密钥，长期记忆退回词面检索。")
            return None
        return OpenAICompatEmbedder(
            cfg["base_url"], cfg["model"], api_key, cfg.get("timeout_s", 20.0)
        )

    cfg = emb_cfg.get("local", {})
    try:
        return OnnxEmbedder(
            cfg.get("model_dir", "data/models/bge-small-zh-v1.5"),
            cfg.get("max_length", 512),
        )
    except Exception as exc:  # 缺依赖 / 缺模型文件都不应拖垮主链路
        print(f"[提示] 端侧 embedding 不可用（{exc}），长期记忆退回词面检索。")
        return None


def build_memory(cfg: dict, fake: bool):
    """按配置组装长期记忆；离线桩或未启用时返回 NullMemory。"""
    mem_cfg = cfg.get("memory", {})
    if fake or not mem_cfg.get("enabled", False):
        return NullMemory()

    embedder = None
    min_score = mem_cfg.get("min_score", 0.15)  # 词面模式阈值
    if mem_cfg.get("retrieval") == "embedding":
        emb_cfg = mem_cfg.get("embedding", {})
        embedder = _build_embedder(emb_cfg)
        if embedder is not None:
            min_score = emb_cfg.get("min_score", 0.4)  # 向量模式阈值（余弦量纲）

    return MemoryStore(
        db_path=mem_cfg.get("db_path", "data/memory.db"),
        embedder=embedder,
        top_k=mem_cfg.get("top_k", 3),
        min_score=min_score,
        min_confidence=mem_cfg.get("min_confidence", 0.6),
    )


def build_short_term(cfg: dict, fake: bool):
    """按配置组装短期记忆；离线桩或未启用时返回 None。"""
    st_cfg = cfg.get("memory", {}).get("short_term", {})
    if fake or not st_cfg.get("enabled", False):
        return None
    return ShortTermMemory(
        db_path=st_cfg.get("db_path", "data/short_term.db"),
        days=st_cfg.get("days", 7),
        inject_limit=st_cfg.get("inject_limit", 5),
        max_chars=st_cfg.get("max_chars", 40),
    )


def build_scheduler(cfg: dict, bus: EventBus, fsm, persona) -> Scheduler:
    """组装主动服务调度器，并把 §9.1 约定的 scheduler -> fsm 说话链路接上。"""
    scheduler = Scheduler(
        bus,
        cfg.get("scheduler", {}),
        can_speak=lambda: fsm.state is State.IDLE,
        greet_text=persona.greeting,
    )

    async def _speak(payload: dict) -> None:
        await fsm.greet(payload["text"])  # 只有 IDLE 时才会真的开口

    bus.on("schedule.fired", _speak)
    return scheduler


def build_services(cfg: dict, scheduler: Scheduler) -> None:
    """组装内容服务（§5.1 services/），并把文本源注册给调度器。

    源的语义：拉不到内容就返回 None，调度器据此静默跳过这次主动开口。
    """
    svc_cfg = cfg.get("services", {})
    if not svc_cfg.get("enabled", False):
        return
    weather_cfg = svc_cfg.get("weather", {})
    weather = WeatherService(weather_cfg)
    calendar = CalendarService(svc_cfg.get("calendar", {}))
    news = NewsService(svc_cfg.get("news", {}))
    briefing = BriefingService(weather, calendar)
    threshold = float(weather_cfg.get("rain_threshold", 60))

    scheduler.register_source("briefing", briefing.report)
    scheduler.register_source("weather", weather.report_text)
    scheduler.register_source("calendar", calendar.report_text)
    scheduler.register_source("news", news.report_text)
    scheduler.register_source("rain_alert", lambda: weather.rain_alert_text(threshold))


def build_robot(cfg: dict, fake: bool = False, model: str | None = None):
    """按依赖顺序组装各模块，返回 (bus, fsm, expression, persona, llm, memory,
    short_term, extractor, scheduler)。"""
    bus = EventBus()
    persona = Persona(cfg["persona"])
    llm = build_llm(cfg, bus, fake, model_override=model)
    memory = build_memory(cfg, fake)
    short_term = build_short_term(cfg, fake)
    pipeline = DialoguePipeline(
        bus=bus,
        llm=llm,
        persona=persona,
        emotion=EmotionDetector(),
        memory=memory,
        short_term=short_term,
    )
    tts = ConsoleTTS(speaker=persona.name, bus=bus)
    fsm = DialogueFSM(bus=bus, pipeline=pipeline, tts=tts)
    expression = build_expression(bus, cfg, cfg["persona"])
    scheduler = build_scheduler(cfg, bus, fsm, persona)
    build_services(cfg, scheduler)
    extractor = None
    if isinstance(memory, MemoryStore):
        extractor = FactExtractor(
            bus, llm, memory, cfg["memory"].get("extract_min_confidence", 0.6)
        )
    return bus, fsm, expression, persona, llm, memory, short_term, extractor, scheduler


def _reply_text(raw: str) -> str:
    """工作记忆里存的是模型原始输出（保留 JSON 协议），展示时尽量取回可读文本。"""
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(obj, dict) and obj.get("text"):
        return str(obj["text"])
    return raw


async def _memory_report(history: list[dict], memory, short_term) -> str:
    """把三层记忆渲染成一段可直接打印的调试快照（/mem）。"""
    lines = ["===== 记忆快照 ====="]
    turns = sum(1 for msg in history if msg["role"] == "user")
    lines.append(f"[工作记忆] 当前会话最近 {turns} 轮")
    if history:
        for msg in history[-6:]:  # 只展示最近 3 轮，避免刷屏
            if msg["role"] == "user":
                lines.append(f"  你：{msg['content']}")
            else:
                lines.append(f"  小伴：{_reply_text(msg['content'])}")
    else:
        lines.append("  （空）")

    lines.append("")
    if short_term is not None:
        recent = await short_term.recent(limit=10)
        lines.append(f"[短期记忆] 近 {short_term.days} 天共 {short_term.count()} 条")
        if recent:
            lines.extend(f"  {item}" for item in recent)
        else:
            lines.append("  （空）")
    else:
        lines.append("[短期记忆] 未启用")

    lines.append("")
    if isinstance(memory, MemoryStore):
        facts = memory.all_facts()
        lines.append(f"[长期记忆] 共 {len(facts)} 条事实")
        if facts:
            for fact in facts[:10]:
                expire = f"，到期 {fact['expire']}" if fact["expire"] else ""
                lines.append(
                    f"  - {fact['content']}（{fact['kind']}，"
                    f"置信度 {fact['confidence']:.2f}{expire}）"
                )
        else:
            lines.append("  （空）")
    else:
        lines.append("[长期记忆] 未启用")

    lines.append("==================")
    return "\n".join(lines)


# 可手动预览的情绪（与策略表 §5.6 的 7 类一致）
EMOTIONS = ("happy", "excited", "sad", "anxious", "angry", "tired", "neutral")


def _expression_table(expressions: dict) -> str:
    """列出各情绪对应的灯效/动作/表情，便于 /expr 逐个试。"""
    lines = ["===== 表达预览（/expr <情绪> 触发一次）====="]
    lines.append("  情绪      灯效        动作        表情")
    for mood in EMOTIONS:
        plan = policy.resolve_emotion(mood, expressions)
        lines.append(
            f"  {mood:<8}{describe(plan['led']):<12}"
            f"{gesture_label(plan['gesture']):<12}{frame_for(mood)}"
        )
    lines.append("  例：/expr happy 0.9（情绪 + 可选强度 0~1）")
    lines.append("==========================================")
    return "\n".join(lines)


async def _run_expr(bus: EventBus, command: str, expressions: dict) -> None:
    """``/expr`` 列出各情绪表达；``/expr <情绪> [强度]`` 触发一次表达。"""
    parts = command.split()
    if len(parts) == 1:
        print(_expression_table(expressions))
        return
    mood = parts[1].lower()
    if mood not in EMOTIONS:
        print(f"        [提示] 未知情绪「{mood}」，可选：{'、'.join(EMOTIONS)}")
        return
    intensity = 1.0
    if len(parts) > 2:
        try:
            intensity = float(parts[2])
        except ValueError:
            print("        [提示] 强度需为 0~1 的数字。")
            return
    # 走模型自选表达的同一事件，顺带把动作也演一遍
    await bus.emit(
        "expression.chosen", {"emotion": mood, "intensity": policy.clamp(intensity)}
    )


async def _run_sched(scheduler: Scheduler, command: str) -> None:
    """``/sched`` 查看任务；``/sched fire <id>`` 触发；``/sched sense <源> <值>`` 模拟传感器。"""
    parts = command.split()
    if len(parts) == 1:
        print(scheduler.report())
        return
    action = parts[1]
    if action == "fire" and len(parts) > 2:
        if await scheduler.fire_now(parts[2]) is None:
            print(
                f"        [提示] 没有任务「{parts[2]}」，"
                "或暂时取不到内容（如断网、今天没日程）"
            )
        return
    if action == "sense" and len(parts) > 3:
        try:
            value = float(parts[3])
        except ValueError:
            print("        [提示] 传感器读数需为数字。")
            return
        await scheduler.push_sensor(parts[2], value)
        return
    print("        [提示] 用法：/sched | /sched fire <任务id> | /sched sense <传感器> <读数>")


async def run_sim(cfg: dict, fake: bool = False, model: str | None = None) -> None:
    bus, fsm, expression, persona, llm, memory, short_term, extractor, scheduler = (
        build_robot(cfg, fake, model)
    )
    name = persona.name

    async def on_fallback(payload: dict) -> None:
        print(f"        [降级] {payload['from']} -> {payload['to']} ({payload['reason']})")

    async def on_failed(payload: dict) -> None:
        print(f"        [错误] {payload['error']}")

    async def on_stored(payload: dict) -> None:
        for fact in payload["facts"]:
            print(f"        [记忆] 记住：{fact}")

    async def on_recalled(payload: dict) -> None:
        print(f"        [回忆] {'；'.join(payload['hits'])}")

    async def on_extract_failed(payload: dict) -> None:
        print(f"        [记忆] 抽取失败：{payload['error']}")

    async def on_scheduled(payload: dict) -> None:
        print(f"        [主动] {payload['id']}：{payload['text']}")

    async def on_scheduler_failed(payload: dict) -> None:
        print(f"        [调度] 异常：{payload['error']}")

    bus.on("llm.fallback", on_fallback)
    bus.on("llm.failed", on_failed)
    bus.on("memory.stored", on_stored)
    bus.on("memory.recalled", on_recalled)
    bus.on("memory.extract_failed", on_extract_failed)
    bus.on("schedule.fired", on_scheduled)
    bus.on("scheduler.failed", on_scheduler_failed)

    print("=" * 52)
    print(f"  {name} · 模拟模式（无硬件）")
    print(f"  模型：{llm.name}{'  [离线桩]' if fake else ''}")
    if isinstance(memory, MemoryStore):
        if isinstance(memory.embedder, OnnxEmbedder):
            mode = "端侧向量"
        elif memory.embedder is not None:
            mode = "云端向量"
        else:
            mode = "词面"
        print(f"  记忆：长期记忆已开启（{mode}检索，已存 {memory.count()} 条）")
    else:
        print("  记忆：未启用")
    if short_term is not None:
        print(
            f"  短期记忆：已开启（近 {short_term.days} 天摘要，"
            f"已存 {short_term.count()} 条）"
        )
    exp_cfg = cfg.get("expression", {})
    enabled = [
        label
        for key, label in (("screen", "屏幕表情"), ("led", "LED"), ("servo", "舵机"))
        if exp_cfg.get(key, "console") != "none"
    ]
    print(f"  表达：{'、'.join(enabled) if enabled else '未启用'}（终端预览，接硬件换驱动）")
    print(
        f"  主动服务：{'已开启' if scheduler.enabled else '停用'}"
        f"（{len(scheduler.tasks)} 个任务，每日上限 {scheduler.max_daily} 次）"
    )
    if cfg.get("services", {}).get("enabled", False):
        cal_cfg = cfg["services"].get("calendar", {})
        source = cal_cfg.get("ics_url") or cal_cfg.get("ics_path") or "未配置日程源"
        news_count = len(cfg["services"].get("news", {}).get("feeds", []))
        print(f"  内容服务：天气（Open-Meteo）＋ 日程（{source}）＋ 资讯（{news_count} 个源）")
    else:
        print("  内容服务：未启用")
    print(
        "  直接输入文字回车发送；/wake 模拟唤醒；/mem 记忆；"
        "/expr 试表情；/sched 主动服务；/quit 退出"
    )
    print("=" * 52)

    scheduler.start()  # 启动主动服务轮询
    await fsm.greet(persona.greeting())  # 开机开场白

    while True:
        try:
            line = await asyncio.to_thread(input, "你 > ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = line.strip()
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/wake":
            await fsm.on_wake()
            continue
        if text == "/mem":
            print(await _memory_report(fsm.pipeline.history, memory, short_term))
            continue
        if text == "/expr" or text.startswith("/expr "):
            await _run_expr(bus, text, cfg["persona"].get("expressions", {}))
            continue
        if text == "/sched" or text.startswith("/sched "):
            await _run_sched(scheduler, text)
            continue

        if fsm.state is State.IDLE:
            await fsm.on_wake()  # 模拟模式下未唤醒时自动唤醒
        await fsm.on_utterance(text)

    if extractor is not None:
        await extractor.aclose()  # 等后台抽取写完，避免退出时丢事实
    await scheduler.aclose()  # 停掉主动服务轮询
    await expression.aclose()  # 停掉眨眼协程，释放表达层通道
    if short_term is not None:
        short_term.close()
    if isinstance(memory, MemoryStore):
        if memory.embedder is not None:
            await memory.embedder.aclose()
        memory.close()

    print(f"\n{name}：下次见，我会一直在桌上等你。")


def main() -> None:
    parser = argparse.ArgumentParser(description="小伴 · AI 陪伴桌面机器人")
    parser.add_argument("--sim", action="store_true", help="无硬件模拟模式")
    parser.add_argument("--fake", action="store_true", help="使用离线桩模型，不联网")
    parser.add_argument("--model", default=None, help="临时覆盖云端模型名（便于逐个试）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not args.sim:
        raise SystemExit("当前仅支持模拟模式，请加 --sim 参数运行。")
    asyncio.run(run_sim(cfg, fake=args.fake, model=args.model))


if __name__ == "__main__":
    main()
