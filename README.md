# New Energy Daily

面向 Hermes Agent 与 Linux VPS 的新能源日报 Skill。它抓取新能源新闻及欧洲电力、天然气市场数据，使用 OpenAI 兼容模型进行评分和中文写作，生成 Markdown/HTML，并可通过 Agent Mail 投递。

## 项目结构

```text
skills/new-energy-daily/
├── SKILL.md
├── agents/
├── assets/
├── config/
├── references/
└── scripts/new_energy_daily.py
```

该布局可作为 Hermes GitHub skill tap 安装，运行文件、参考资料和脚本会作为一个完整 Skill 下载。

## Hermes 安装

Hermes 的 GitHub tap 当前读取仓库默认分支，不提供选择非默认分支的参数。因此，以下 tap 命令应在本分支合并到 `main` 后使用：

```bash
hermes skills tap add sulirang/new-energy-daily
hermes skills install sulirang/new-energy-daily/new-energy-daily
hermes skills list | grep new-energy-daily
```

合并前测试 `codex/hermes-linux-hardening`，或在 VPS 长期部署该分支时，必须显式指定分支：

```bash
git clone --branch codex/hermes-linux-hardening --single-branch \
  https://github.com/sulirang/new-energy-daily.git /opt/new-energy-daily
cd /opt/new-energy-daily/skills/new-energy-daily

python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r assets/requirements.txt

cp assets/env.example .env
mkdir -p output logs state
chmod 700 output logs state
```

推荐 Python 3.11 或 3.12。不要用 root 身份运行日报；执行定时任务的 Linux 用户必须与完成 Agent Mail OAuth 授权的用户相同。

## 配置

编辑 `.env`：

```dotenv
AI_API_KEY=your_api_key
AI_BASE_URL=https://api.deepseek.com
AI_MODEL=deepseek-v4-pro

AGENT_MAIL_RECIPIENTS=recipient@example.com
REPORT_TIMEZONE=Europe/Rome
COLLECTION_CUTOFF_TIME=07:00
MARKET_MAX_WORKERS=2
```

添加 Firecrawl 与 Exa Key：

```bash
printf '%s\n' 'fc-key-1' 'fc-key-2' > config/firecrawl_key.txt
printf '%s\n' 'exa-key-1' 'exa-key-2' > config/exa_keys.txt
chmod 600 .env config/firecrawl_key.txt config/exa_keys.txt
```

真实凭据已被 `.gitignore` 排除。程序不会将 Key 写入报告，Exa 错误也会脱敏。

## 运行

从任意目录都可以运行，默认路径会相对于 Skill 自身解析：

```bash
/opt/new-energy-daily/skills/new-energy-daily/.venv/bin/python \
  /opt/new-energy-daily/skills/new-energy-daily/scripts/new_energy_daily.py \
  --dry-run
```

正式生成并发送：

```bash
cd /opt/new-energy-daily/skills/new-energy-daily
.venv/bin/python scripts/new_energy_daily.py
```

默认输出：

- `output/YYYY-MM-DD.md`
- `output/YYYY-MM-DD.html`

同一日期成功发送后会记录到 `state/sent_reports.json`，再次运行默认不会重复发信。只有明确需要重发时才使用 `--force-send`。

新闻采集窗口使用 `Europe/Rome` 时区和配置的 07:00 截止时间：

- 周二至周日：前一日 07:00（不含）至当日 07:00（含）。
- 周一：上周五 07:00（不含）至周一 07:00（含），覆盖周五白天、周六、周日和周一清晨，避免周末新闻遗漏。
- RSS、网页和 Exa 返回的候选都会再按上述本地时间窗口精确过滤；Exa 查询本身也会携带窗口起止时间。
- 周六和周日仍可生成本地预览，但 Agent Mail 始终跳过发送；`--force-send` 也不会绕过此规则。

## Agent Mail

```bash
npm install -g @tencent-qqmail/agently-cli
agently-cli auth login
agently-cli +me
```

如全局 npm 可执行目录不在定时任务的 `PATH` 中，请在 `.env` 中给 `AGENT_MAIL_CLI` 配置绝对路径。

## Hermes 定时任务

仓库提供 [`deploy/hermes-new-energy-daily.sh`](deploy/hermes-new-energy-daily.sh) 模板。将其复制到 `~/.hermes/scripts/new-energy-daily.sh`，执行 `chmod 700 ~/.hermes/scripts/new-energy-daily.sh`，设置 `NEW_ENERGY_DAILY_HOME` 后使用 Hermes `no-agent` cron 运行。这样日报脚本负责确定性抓取和模型调用，Hermes 只负责调度与失败告警，不会额外启动一层 agent 推理。

若使用传统 cron，先确保日志目录存在：

```cron
CRON_TZ=Europe/Rome
0 7 * * * cd /opt/new-energy-daily/skills/new-energy-daily && .venv/bin/python scripts/new_energy_daily.py >> logs/daily.log 2>&1
```

## 可靠性设计

- 默认并发抓取两组市场数据，匹配 Firecrawl 当前常见的两个并发任务限制；高额度套餐可通过 `MARKET_MAX_WORKERS` 调高。
- Firecrawl 返回 429 时会根据服务端提示等待后自动重试，避免立即重放进一步放大限流。
- Firecrawl 支持多 Key 轮询；每次市场请求预留下一个 Key，认证、额度或限流失败时自动切换，其轮询位置只记录在 `state/firecrawl_key_state.json`。
- Linux wrapper 默认给整次运行设置 15 分钟总超时，可通过 `NEW_ENERGY_DAILY_TIMEOUT` 调整。
- 单一新闻源或市场源失败时继续生成报告，并在异常模块中说明。
- 仅选择 AI 标记为入选且分数达到 `minimum_news_score` 的新闻，最多 15 条。
- Agent Mail 命令带超时，确认令牌不会写入错误日志。
- 同一日期默认仅发送一次。
- 周末不发送邮件；周一自动汇总周末以来的新闻。

日报结构见 [`skills/new-energy-daily/references/report-format.md`](skills/new-energy-daily/references/report-format.md)，完整工作流见 [`skills/new-energy-daily/SKILL.md`](skills/new-energy-daily/SKILL.md)。
