---
name: new-energy-daily
description: Generate a daily new-energy industry briefing with Italian, Hungarian, and Spanish day-ahead electricity prices; European EEX TTF, Italian GME, Hungarian CEEGEX, and Spanish MIBGAS natural-gas prices fetched through Firecrawl; and news from configured websites, Exa domain searches, RSS feeds, and article pages. Use when Hermes needs to fetch today's renewable energy, EV, battery, energy storage, hydrogen, charging infrastructure, grid, solar, wind, European power-market or gas-price news; rank items by news value; select the top 15 stories; write a Chinese daily report; save it; and send an HTML email with Agent Mail from a VPS or scheduled job.
---

# New Energy Daily

Use this skill to produce a daily Chinese new-energy briefing from user-provided sources. Prefer deterministic fetching and normalization in `scripts/new_energy_daily.py`, then use AI only for value assessment and report writing.

## Inputs

- `config/sources.yaml`: user-provided news sources. Start from `assets/sources.example.yaml`.
- Environment variables: start from `assets/env.example`.
- `config/exa_keys.txt`: Exa keys, one per line. Blank lines and lines beginning with `#` are ignored.
- `config/firecrawl_key.txt`: one Firecrawl API key used for all electricity and natural-gas market-price collection.
- Optional run date: default to today in `Asia/Shanghai`.
- Optional output directory: default to `output/`.

## Workflow

1. Load source config and secrets.
2. Fetch the target day's PUN Index GME and seven physical Italian zonal MGP prices through Firecrawl.
   - Send the GME page URL to Firecrawl with `proxy: auto`, Italian location settings, and a fresh scrape.
   - Use Firecrawl Interact code to retrieve `d` daily values and `qh` 15-minute values.
   - Use the `d` value as the daily average and calculate the highest and lowest prices from the 96 `qh` values.
   - Never request the GME website directly from the VPS. All GME page and data requests must run inside Firecrawl's hosted browser.
   - Stop the Interact session after every attempt to avoid unnecessary billing.
   - Put this deterministic module after the selected news and before candidate statistics or diagnostics.
   - Render it as `## 日前电价`, then `### 意大利`, followed by the four-column price table and only the GME source line.
   - Continue the report if Firecrawl or GME is unavailable and show the failure in the module and diagnostics.
3. Fetch Hungarian and Spanish day-ahead electricity prices through separate Firecrawl sessions.
   - For Hungary, load HUPX Labs and call its official `dam_aggregated_trading_data_15min` endpoint inside Firecrawl for `Region=HU` and the target delivery day.
   - Use HUPX's published `BaseloadPrice` as the daily average and calculate highest and lowest prices from the final 15-minute `Price` values.
   - For Spain, load the OMIE day-ahead results page and fetch its official dated `INT_PBC_EV_H_1_*.TXT` data file inside Firecrawl.
   - Parse the Spanish marginal-price row, then calculate the arithmetic average, highest, and lowest values from its 15-minute periods.
   - Render `### 匈牙利` and `### 西班牙` as separate four-column tables under the existing `## 日前电价` heading, each followed only by its source line.
   - Never request HUPX or OMIE directly from the VPS; stop each Interact session after use and let either market fail independently.
4. Fetch four natural-gas benchmarks through independent Firecrawl requests.
   - For Europe, scrape EEX `NDI_45_Days.csv`, match `Hub=TTF` and the target delivery day, and label the source `EEX TTF NDI`.
   - If NDI cannot be parsed or has no target-day TTF value, scrape `TTF_NGP_60_Days.csv`, use the target or latest prior value, and label the source `EEX TTF NGP`.
   - For Italy, fetch the target day's reference price from the IG Index GME results page through Firecrawl.
   - Read `window.GmeIGIndex` for the page-specific `ModuleId` and `TabId`, plus the anti-forgery token.
   - Call the page's internal `GetGasIGI` endpoint inside Firecrawl with the target year, month, and daily detail.
   - Match the report date to the returned `data` field and use `igi` as the published `€/MWh` value.
   - Do not parse the rendered HTML or Markdown table for the production value.
   - For Hungary, read CEEGEX Day-Ahead table rows, match `Contract=DA` by `Start of Delivery`, prefer CEEREP, and use Volume Weighted Average Price only when CEEREP is missing.
   - For Spain, read the MIBGAS Highcharts JSON and match the PVB `MIBGAS-ES Index` point by gas day.
   - Prefer the target gas day; CEEGEX, MIBGAS, and EEX NGP may use the latest earlier valid day and must log the fallback date.
   - Render `## 天然气价` immediately after the electricity section, without a country subheading.
   - Keep rows in this order: `欧洲总体`, `意大利`, `匈牙利`, `西班牙`. Render unavailable rows as `N/A` and log the reason.
   - Render one compact bullet labeled `数据源：` below the table, then indent the four source links as separate continuation lines inside that single bullet. Do not repeat the label or create four bullets. Show `EEX TTF NGP` explicitly whenever the EEX fallback is used.
5. Fetch today's candidate news from enabled sources.
   - Prefer RSS or Atom feeds when available.
   - Use Exa with `include_domains` when the VPS should search specified websites and retrieve article text through Exa.
   - Use webpage selectors only for sources without feeds.
   - Normalize URLs, remove tracking parameters, and deduplicate by canonical URL.
   - Keep only items published on the target date in `Asia/Shanghai`; include undated items only when a source sets `allow_undated: true`.
6. Extract article text for each candidate when possible.
   - Preserve title, URL, source, published time, summary, and extracted text.
   - Drop content shorter than 80 Chinese characters unless the title clearly carries material news.
7. Ask AI to score each candidate from 0-100.
   - Reward policy/regulatory impact, large company moves, investment/financing, project commissioning, production or shipment data, technology breakthroughs, battery/storage/hydrogen/grid relevance, market impact, freshness, and source authority.
   - Penalize generic announcements, product ads, repeated syndicated copy, unverifiable claims, SEO pages, and content without concrete facts.
8. Select the top 15 items.
   - Keep the score order unless it creates obvious duplication.
   - When two items cover the same event, keep the better sourced or more detailed one.
   - Prefer a balanced mix across policy, industry, company, market, and technology when scores are close.
9. Write the report in Chinese Markdown using `references/report-format.md`.
   - The "今日看点" section must be <=200 Chinese characters.
   - Each of the 15 stories must include title, source, time, news value, concise summary, and original link.
   - Do not invent details that are not in the fetched material.
10. Save Markdown to `output/YYYY-MM-DD.md` and render HTML to `output/YYYY-MM-DD.html`.
11. Send the HTML file with Agent Mail if `AGENT_MAIL_RECIPIENTS` is configured.
12. Log failed sources separately and continue when other sources succeed.

## Commands

Install dependencies on the VPS:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r assets/requirements.txt
npm install -g @tencent-qqmail/agently-cli
```

Add Exa keys to `config/exa_keys.txt` when using Exa sources:

```text
exa_key_1
exa_key_2
exa_key_3
```

Restrict the file on Linux with `chmod 600 config/exa_keys.txt` and never commit real keys. Override its location with `EXA_KEYS_FILE`; `EXA_API_KEYS` and the legacy `EXA_API_KEY` are also supported.

The key pool rotates after every successful Exa request and stores only the next key index in `config/exa_key_state.json`. On authentication, credit, quota, or rate-limit errors, try each remaining key automatically without logging key values. Configure each Exa source with `type: exa`, a natural-language `query` containing the optional `{date}` placeholder, and one or more exact hostnames in `include_domains`. Keep `use_api_date_filter: false` unless the target site is known to work with Exa date filters; the script always applies an exact local date check in the report timezone.

Add the Firecrawl key used for market prices to `config/firecrawl_key.txt`:

```text
fc-your-firecrawl-key
```

Restrict it with `chmod 600 config/firecrawl_key.txt` and never commit it. Override its location with `FIRECRAWL_KEY_FILE`, or use `FIRECRAWL_API_KEY`. The configured `FIRECRAWL_API_BASE` defaults to the hosted service at `https://api.firecrawl.dev`; using a self-hosted instance does not hide the VPS IP unless that instance has its own outbound proxy.

Install or update the Agent Mail skill for the target agent:

```bash
npx skills add https://agent.qq.com --skill -g -y
```

Authorize Agent Mail once on the VPS before scheduling:

```bash
agently-cli auth login
agently-cli +me
```

When running `agently-cli auth login`, present the raw OAuth URL exactly as printed by the CLI to the user with this prompt: `请点击或复制以下链接在浏览器中完成授权：`. Do not modify, encode, decode, or reformat the URL. After authorization, `agently-cli +me` should return the authorized mailbox.

Run manually:

```bash
python scripts/new_energy_daily.py --sources config/sources.yaml --output output
```

Dry run without email:

```bash
python scripts/new_energy_daily.py --sources config/sources.yaml --output output --dry-run
```

Use cron for a daily Beijing-time run:

```cron
30 18 * * * cd /opt/new-energy-daily && . .venv/bin/activate && python scripts/new_energy_daily.py --sources config/sources.yaml --output output >> logs/daily.log 2>&1
```

## AI Evaluation Prompt

Use this scoring rubric whenever modifying or reimplementing the evaluator:

```text
你是新能源产业新闻编辑。请只根据输入材料评估新闻价值，不要补充外部事实。

评分维度：
1. 政策与监管影响：国家/地方政策、补贴、准入、碳市场、电力市场机制。
2. 产业与市场影响：销量、装机、招标、中标、价格、产能、供应链变化。
3. 企业与资本动作：头部企业战略、并购、融资、重大订单、海外扩张。
4. 技术价值：电池、储能、光伏、风电、氢能、充换电、电网、虚拟电厂等实质突破。
5. 新鲜度与稀缺性：当天发生、非重复、非软文、信息密度高。
6. 信源可信度：权威媒体、政府/交易所/公司公告、行业协会、可靠数据库优先。

请为每条新闻输出 0-100 分、1 句价值理由、主题标签，并指出是否建议入选日报。
```

## Report Writing Rules

- Write in professional Chinese for investors, operators, analysts, and industry practitioners.
- Keep the tone factual and concise.
- Do not use clickbait language.
- Put concrete numbers, named companies, policy names, locations, and dates in the summary when present.
- Use Markdown links only for original links.
- If fewer than 15 valid items exist, state the actual count and do not pad.
- If no valid item exists, send a short "今日无可用新闻" email with failed-source diagnostics.

## Agent Mail Sending

Use `agently-cli message +send`. The bundled script sends the generated HTML report as the mail body:

```bash
agently-cli message +send --to recipient@example.com --subject "新能源日报 - YYYY-MM-DD" --body-file output/YYYY-MM-DD.html
```

Agent Mail send operations use two-step confirmation. The first command returns `data.confirmation_token`; rerun the same command with `--confirmation-token <token>` to complete the send. `scripts/new_energy_daily.py` performs both steps automatically.

Set recipients with `AGENT_MAIL_RECIPIENTS`, comma-separated. Do not store mail-account passwords for this skill.
