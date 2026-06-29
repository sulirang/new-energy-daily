# New Energy Daily

面向 Hermes/VPS 的新能源日报 Skill。它每天从配置的网站获取新能源新闻，由 AI 评估新闻价值并选出最多 15 篇，生成中文 Markdown 与 HTML 日报，再通过 Agent Mail 发送邮件。

## 主要功能

- RSS、网页选择器和 Exa 指定域名搜索。
- Exa 多 Key 轮询，额度耗尽或限流时自动切换。
- AI 新闻价值评分、去重、Top 15 和 200 字以内“今日看点”。
- 意大利、匈牙利、西班牙日前电价。
- 欧洲总体、意大利、匈牙利、西班牙天然气价格。
- Firecrawl 托管浏览器抓取市场数据，避免 VPS 直接访问目标交易所。
- Markdown/HTML 日报与 Agent Mail 邮件发送。
- 单个新闻源或市场数据源失败时继续生成日报，并记录异常。

## 市场数据来源

日前电价：

- 意大利：GME PUN 与物理分区价格。
- 匈牙利：HUPX Day-Ahead 15 分钟数据。
- 西班牙：OMIE Day-Ahead 官方数据文件。

天然气价：

- 欧洲总体：EEX TTF NDI，缺失时回退到 EEX TTF NGP。
- 意大利：GME IG Index。
- 匈牙利：CEEGEX DA，优先 CEEREP，缺失时使用 VWAP。
- 西班牙：MIBGAS PVB Price Index。

网站、关键词及市场数据抓取参数位于 [`config/sources.yaml`](config/sources.yaml)。通用示例位于 [`assets/sources.example.yaml`](assets/sources.example.yaml)。

## 所需凭据

| 配置 | 是否必需 | 用途 | 推荐保存位置 |
|---|---|---|---|
| `AI_API_KEY` | 必需 | AI 评分和日报写作 | `.env` |
| Firecrawl API Key | 启用市场价格时必需 | 托管浏览器和 CSV 抓取 | `config/firecrawl_key.txt` |
| Exa API Key | 启用 Exa 新闻源时必需 | 指定网站新闻搜索与正文提取 | `config/exa_keys.txt`，每行一个 |
| Agent Mail 授权 | 发送邮件时必需 | HTML 邮件投递 | 在 VPS 执行 OAuth 登录，不保存邮箱密码 |

`AI_BASE_URL` 支持任意 OpenAI 兼容接口。Exa 可以配置多个 Key，脚本会在成功请求后轮换，并在认证、额度、限流错误时尝试下一个 Key。

## 让 AI 协助配置

将下面的提示词交给 Hermes、Codex 或其他负责部署的 AI：

```text
请帮我部署 new-energy-daily Skill。先检查 config/sources.yaml、assets/env.example 和 .gitignore，然后逐项检查以下配置是否缺失：

1. AI_API_KEY、AI_BASE_URL、AI_MODEL。
2. Firecrawl API Key；将真实值写入 config/firecrawl_key.txt，每行一个值，不要写入 sources.yaml。
3. Exa API Key；如果启用了 type: exa 的新闻源，询问我提供一个或多个 Key，并逐行写入 config/exa_keys.txt。
4. AGENT_MAIL_RECIPIENTS；如果需要发邮件，指导我执行 agently-cli auth login 和 agently-cli +me。

每次只询问当前缺少的配置。不要在回复、日志或命令输出中回显完整 Key，不要把真实 Key 提交到 Git。Linux 上将 .env、config/firecrawl_key.txt 和 config/exa_keys.txt 权限设为 600。配置完成后先运行 --dry-run，确认 Markdown 和 HTML 正常生成，再启用邮件和 cron。
```

AI 应主动说明哪些凭据是必需的、哪些只在对应功能启用时需要；不要要求用户提供邮箱密码。

## VPS 安装

```bash
git clone https://github.com/sulirang/new-energy-daily.git
cd new-energy-daily

python -m venv .venv
. .venv/bin/activate
pip install -r assets/requirements.txt

cp assets/env.example .env
npm install -g @tencent-qqmail/agently-cli
```

编辑 `.env`：

```dotenv
AI_API_KEY=your_ai_api_key
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini

AGENT_MAIL_RECIPIENTS=recipient@example.com
REPORT_TIMEZONE=Europe/Rome
COLLECTION_CUTOFF_TIME=12:30
```

添加 Firecrawl Key：

```bash
printf '%s\n' 'fc-your-firecrawl-key' > config/firecrawl_key.txt
chmod 600 config/firecrawl_key.txt
```

添加一个或多个 Exa Key：

```bash
printf '%s\n' 'exa-key-1' 'exa-key-2' > config/exa_keys.txt
chmod 600 config/exa_keys.txt
```

授权 Agent Mail：

```bash
agently-cli auth login
agently-cli +me
```

## 运行

先生成日报但不发邮件：

```bash
python scripts/new_energy_daily.py \
  --sources config/sources.yaml \
  --output output \
  --dry-run
```

正式生成并发送：

```bash
python scripts/new_energy_daily.py \
  --sources config/sources.yaml \
  --output output
```

指定日期：

```bash
python scripts/new_energy_daily.py --date 2026-06-26 --dry-run
```

默认输出：

- `output/YYYY-MM-DD.md`
- `output/YYYY-MM-DD.html`

## 定时任务

以下示例每天意大利时间 12:30 运行，并抓取前一日 12:30（不含）至当日 12:30（含）的新闻。`Europe/Rome` 会自动处理意大利夏令时：

```cron
CRON_TZ=Europe/Rome
30 12 * * * cd /opt/new-energy-daily && . .venv/bin/activate && python scripts/new_energy_daily.py --sources config/sources.yaml --output output >> logs/daily.log 2>&1
```

## 安全说明

以下内容已由 `.gitignore` 排除，禁止提交：

- `.env`
- `config/firecrawl_key.txt`
- `config/exa_keys.txt`
- `config/exa_key_state.json`
- `output/`
- `logs/`

提交前建议运行：

```bash
git status --short
git diff --cached --check
```

日报结构和写作约束见 [`references/report-format.md`](references/report-format.md)，完整 Skill 工作流见 [`SKILL.md`](SKILL.md)。
