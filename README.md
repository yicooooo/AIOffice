# AIOffice

使用 scripts/run_stirrup_tasks.py 批量运行 AgentIF-OneDay 任务。

## 1) 先配置 .env

在 AIOffice 根目录执行：

```bash
cp scripts/.env.example .env
```

然后编辑 .env，至少填写以下 3 项（其他可选）：

- MODEL_BASE_URL
- MODEL_API_KEY
- MODEL_NAME

脚本会通过 load_dotenv() 自动读取这些环境变量。若命令行传了 --model / --api-key / --base-url，则以命令行参数为准。

## 2) 我们使用的数据集目录

- agentif_oneday_data/ifoneday_excel
- agentif_oneday_data/ifoneday_pdf
- agentif_oneday_data/ifoneday_ppt
- agentif_oneday_data/ifoneday_word

每类数据通常包含：

- 一个任务文件（如 excel.jsonl / pdf.jsonl / ppt.jsonl / word.jsonl）
- Questions 目录（附件）

## 3) 命令行运行示例（服务器）

服务器环境请始终使用 --browser-headless true。

### Excel 任务

```bash
python scripts/run_stirrup_tasks.py \
  --task-jsonl agentif_oneday_data/ifoneday_excel/excel.jsonl \
  --attachment-dir agentif_oneday_data/ifoneday_excel/Questions \
  --attachment-search-root agentif_oneday_data/ifoneday_excel \
  --limit 3 \
  --browser-headless true
```

### PDF 任务

```bash
python scripts/run_stirrup_tasks.py \
  --task-jsonl agentif_oneday_data/ifoneday_pdf/pdf.jsonl \
  --attachment-dir agentif_oneday_data/ifoneday_pdf/Questions \
  --attachment-search-root agentif_oneday_data/ifoneday_pdf \
  --limit 3 \
  --browser-headless true
```

### PPT 任务

```bash
python scripts/run_stirrup_tasks.py \
  --task-jsonl agentif_oneday_data/ifoneday_ppt/ppt.jsonl \
  --attachment-dir agentif_oneday_data/ifoneday_ppt/Questions \
  --attachment-search-root agentif_oneday_data/ifoneday_ppt \
  --limit 3 \
  --browser-headless true
```

### Word 任务

```bash
python scripts/run_stirrup_tasks.py \
  --task-jsonl agentif_oneday_data/ifoneday_word/word.jsonl \
  --attachment-dir agentif_oneday_data/ifoneday_word/Questions \
  --attachment-search-root agentif_oneday_data/ifoneday_word \
  --limit 3 \
  --browser-headless true
```

如需指定题目，可把 --limit 换成：

```bash
--question-ids taskif_83,taskif_88
```

## 4) 参数说明（常用在前，后面是全部参数）

常用参数：

- --task-jsonl: 任务 JSONL 路径（必填）
- --attachment-dir: 附件目录（必填）
- --attachment-search-root: 附件递归检索根目录；建议设为对应 ifoneday_xxx 目录
- --question-ids: 指定任务 ID，逗号分隔（如 taskif_83,taskif_88）
- --limit: 未传 --question-ids 时，按顺序运行前 N 条（默认 3）
- --output-dir: 输出根目录（默认 runs_stirrup_generic）
- --model: 模型名；可来自 STIRRUP_MODEL 或 MODEL_NAME
- --api-key: 模型密钥；可来自 STIRRUP_API_KEY 或 MODEL_API_KEY
- --base-url: 模型服务地址；可来自 STIRRUP_BASE_URL 或 MODEL_BASE_URL
- --browser-headless: 浏览器无头模式；服务器必须为 true
- --dry-run: 只做映射与落盘，不真实调用模型

全部参数（与脚本一致）：

- --task-jsonl: 任务 JSONL 路径（必填）
- --attachment-dir: 附件目录（必填）
- --attachment-search-root: 当附件不在 --attachment-dir 时的兜底递归搜索根目录
- --output-dir: 输出根目录
- --question-ids: 指定任务 ID 列表（逗号分隔）
- --limit: 未指定 --question-ids 时运行的任务数量
- --model: ChatCompletionsClient 使用的模型 ID
- --api-key: ChatCompletionsClient 使用的 API Key
- --base-url: ChatCompletionsClient 使用的 Base URL
- --max-turns: Agent 最大轮数（默认 30）
- --client-timeout-seconds: 模型请求超时时间（默认 1800 秒）
- --web-timeout-seconds: Web 工具超时时间（默认 180 秒）
- --browser-headless: 浏览器 headless true/false（服务器建议固定 true）
- --browser-cdp-url: 复用已有 Chrome CDP 地址（可选）
- --browser-profile-dir: 浏览器持久化 profile 目录（可选）
- --browser-user-agent: 浏览器 User-Agent 覆盖值（可选）
- --browser-timezone: 浏览器时区（可选）
- --cf-retry-attempts: Cloudflare 挑战自动重试次数
- --cf-retry-wait-seconds: 每次 Cloudflare 重试等待秒数
- --brave-api-key: WebToolProvider 的 Brave API Key（可选）
- --system-prompt: 额外 system prompt（可选）
- --include-score-criteria: 将 score_criteria 追加到提示词
- --dry-run: 仅输出映射 payload，不调用 Stirrup API

## 5) 输出位置

- 每个任务输出: <output-dir>/<question_id>/
- 运行汇总: <output-dir>/run_summary.jsonl
- 运行配置: <output-dir>/run_manifest.json
