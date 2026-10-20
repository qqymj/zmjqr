# 小伴 · AI 陪伴桌面机器人

住在桌上的温柔伙伴。当前处于**软件原型**阶段：所有能力先在 PC / 终端跑通，硬件后置。
设计原则见 [技术方案文档](docs/技术方案-AI陪伴桌面机器人.md) §1.3——**软件先行、端云混合、可降级、接口解耦**。

## 快速开始

```bash
# ① 离线桩模型：不联网、不需要任何 Key，最省事
python3 -m robot.main --sim --fake

# ② 接真实 LLM：云端优先，失败/超时自动降级到本地 Ollama
python3 -m robot.main --sim
```

依赖：Python ≥ 3.11，必需 `httpx`。可选端侧向量检索另需 `onnxruntime` + `tokenizers`：

```bash
pip install -e ".[local-embed]"          # 可选组
python -m robot.memory.onnx_embedder --fetch   # 首次下载量化模型（约 23MB）
```

### 环境变量（均为可选）

| 变量 | 用途 | 缺失时行为 |
| --- | --- | --- |
| `SCNET_API_KEY` | 云端 LLM / 云端 embedding | LLM 自动降级到本地 Ollama；embedding 退回词面检索 |

没有 Key 也能完整跑通：云端不可用就降级本地，本地不可用就播兜底话术——**机器人不哑**。

## 里程碑进度

| 里程碑 | 目标 | 状态 |
| --- | --- | --- |
| M0 骨架 | 目录结构 + 事件总线 + 状态机 + `--sim` | 完成 |
| M1 对话闭环 | LLM 流式 + 按句切分 + 终端输出 | 完成 |
| M2 语音闭环 | KWS + VAD + ASR + TTS | **未做**（需麦克风/扬声器） |
| M3 人格与情感 | 角色卡 + 情感识别 + 表达策略 | 完成 |
| M4 记忆 | 三层记忆 + 事实抽取 + 双模式检索 | 完成 |
| M5 硬件表达 | 屏幕表情 + LED + 舵机 | 软件层完成，终端预览；**真机驱动未接** |
| M6 主动服务 | 调度器 + 传感器 + 日历/天气/资讯 | 调度与内容服务完成；**传感器为模拟上报** |
| M7 稳定性 | 降级链路 + watchdog + 长稳测试 | 未做 |
| M8 配置面板 | Web 面板 + 人格编辑器 | 未做 |
| M9 小批量 | 自研主板 + 外壳 + OTA | 未做 |

## 交互指令

| 指令 | 作用 |
| --- | --- |
| 直接输入文字 | 走一轮完整对话（唤醒 → 思考 → 说话 → 表达） |
| `/wake` | 模拟唤醒 |
| `/mem` | 查看三层记忆快照（工作 / 短期 / 长期） |
| `/expr` | 表达对照表；`/expr happy 0.9` 手动触发某情绪 |
| `/sched` | 任务列表；`/sched fire <id>` 手动触发；`/sched sense <源> <值>` 模拟传感器 |
| `/quit` | 退出 |

其他启动参数：`--model <名>` 临时覆盖云端模型、`--config <路径>` 换配置、`--help`。

## 目录结构

```
robot/
  main.py            程序入口：装配所有模块 + 交互循环 + 调试命令
  core/              事件总线、对话状态机、配置加载、主动服务调度器
  brain/             LLM 客户端、降级路由、流式 JSON 解析、角色卡、对话管线
  emotion/           情绪识别 + 「情绪 → 表达」策略映射
  expression/        屏幕表情 / LED / 舵机三通道（协议 + 终端实现）
  memory/            短期记忆、长期记忆、事实抽取、向量嵌入（可选端侧 ONNX）
  services/          天气 / 日程 / 资讯 / 早间播报
  speech/            TTS（当前为终端文本打印）
config/config.toml   所有可调项集中在此，代码里无硬编码
tests/               9 个测试文件
docs/                技术方案设计文档
data/                运行时数据（记忆库）与可选模型文件，均不入库
```

一轮对话的数据流：`core/fsm.py`（状态流转）→ `brain/pipeline.py`（组装 Prompt + 调 LLM）
→ `brain/router.py`（降级）→ `brain/parser.py`（解析情绪/表达字段）→ `speech/tts.py`
→ `expression/controller.py`（驱动灯效/表情/动作）。

## 配置要点（config/config.toml）

| 段落 | 常改项 |
| --- | --- |
| `[llm]` | `provider`（cloud/local）、模型名、Key 环境变量名 |
| `[memory]` | `retrieval`（`lexical` 词面 / `embedding` 向量）、`top_k`、置信度阈值 |
| `[memory.embedding]` | `provider`（`local` 端侧 ONNX / `openai` 云端兼容）、向量模式阈值 |
| `[memory.short_term]` | `days`（滚动窗口天数）、`inject_limit` |
| `[expression]` | `screen` / `led` / `servo` 后端（`console` 终端预览 或 `none`） |
| `[scheduler]` | 安静时段、每日上限、各任务触发窗口 |
| `[services.*]` | 天气坐标与城市、日历 `ics_path`/`ics_url`、资讯 RSS 源列表 |
| `[persona]` | 人格设定、口头禅、按时段问候语、情绪→表达映射 |

## 测试与检查

```bash
python3 -m pytest tests/ -q     # 单元测试
ruff check robot/ tests/        # 静态检查
```

## 调试与数据

```bash
# 查看记忆库内容
sqlite3 data/memory.db     "select kind, content, confidence from facts;"
sqlite3 data/short_term.db "select datetime(ts,'unixepoch','localtime'), user, reply from short_term;"

# 清空记忆（重开一段关系）
rm -f data/memory.db data/short_term.db
```

交互式调试：用 `/mem` 看记忆、`/expr` 试表达、`/sched` 手动触发主动服务。
