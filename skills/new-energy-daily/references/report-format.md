# Report Format

Use this exact structure for the final Markdown report:

```markdown
# 新能源日报 - YYYY-MM-DD

## 今日看点

不超过200个中文字符。概括今天最重要的2-4条线索，点出政策、企业、市场或技术方向，不写空泛套话。

## 今日精选

### 1. 标题

- 来源：来源名称
- 时间：YYYY-MM-DD HH:mm 或 YYYY-MM-DD
- 领域：政策 / 光伏 / 风电 / 储能 / 电池 / 新能源车 / 氢能 / 充换电 / 电网 / 电力市场 / 资本市场 / 其他
- 价值判断：一句话说明为什么入选
- 摘要：2-4句话，保留关键事实、数字、主体和影响范围
- 原文：[链接标题](URL)

### 2. 标题

...

## 日前电价

### 意大利

| 区域 | 日均价格 | 最高价格 | 最低价格 |
|---|---:|---:|---:|
| 意大利（PUN） | 000.00 EUR/MWh | 000.00 EUR/MWh | 000.00 EUR/MWh |
| 北部（NORD） | 000.00 EUR/MWh | 000.00 EUR/MWh | 000.00 EUR/MWh |

- 数据源：[Gestore dei Mercati Energetici](URL)

### 匈牙利

| 区域 | 日均价格 | 最高价格 | 最低价格 |
|---|---:|---:|---:|
| 匈牙利（HUPX） | 000.00 EUR/MWh | 000.00 EUR/MWh | 000.00 EUR/MWh |

- 数据源：[HUPX Hungarian Power Exchange](https://hupx.hu)

### 西班牙

| 区域 | 日均价格 | 最高价格 | 最低价格 |
|---|---:|---:|---:|
| 西班牙（OMIE） | 000.00 EUR/MWh | 000.00 EUR/MWh | 000.00 EUR/MWh |

- 数据源：[OMIE](https://www.omie.es/en/market-results/daily/daily-market/day-ahead-price)

## 天然气价

| 区域 | 天然气价格 |
|---|---:|
| 欧洲总体 | 00.00 EUR/MWh |
| 意大利 | 00.00 EUR/MWh |
| 匈牙利 | 00.00 EUR/MWh |
| 西班牙 | 00.00 EUR/MWh |

- 数据源：<br>
  [EEX TTF NDI](https://gasandregistry.eex.com/Gas/NDI/NDI_45_Days.csv)，<br>
  [Gestore dei Mercati Energetici](URL)，<br>
  [CEEGEX](https://ceegex.hu/en/market-data/daily-data)，<br>
  [MIBGAS PVB](https://www.mibgas.es/en/market-results/gas-daily-price-index-and-volumes)

## 候选概况

- 今日抓取：N 条
- 去重后：N 条
- 入选：N 条
- 主要来源：来源A、来源B、来源C

## 抓取异常

仅在存在异常时保留。列出无法访问、解析失败、无今日内容的来源。
```

Rules:

- "今日看点" must be <=200 Chinese characters, excluding punctuation is not necessary.
- Put the Firecrawl-fetched `日前电价` section after all selected news and before `候选概况` or `抓取异常`.
- For Italy, use GME `d` as the daily average and derive each row's highest and lowest prices from the 96 `qh` values.
- For Hungary, use HUPX `BaseloadPrice` as the daily average and derive highest and lowest values from final 15-minute prices.
- For Spain, derive the arithmetic average, highest, and lowest values from the OMIE Spanish marginal-price row.
- Keep only the corresponding market source line below each country table.
- Put `天然气价` immediately after `日前电价`, without a country subheading.
- Keep gas rows ordered as Europe, Italy, Hungary, and Spain; preserve unavailable rows as `N/A`.
- Use EEX TTF NDI for Europe and mark the source as EEX TTF NGP when the NGP fallback is used.
- Use Italy's IG Index GME, Hungary's CEEGEX DA CEEREP (or VWAP fallback), and Spain's MIBGAS PVB Price Index.
- Append `EUR/MWh` to every available gas value and render one `数据源：` bullet with the four source links indented as continuation lines below it.
- Log missing target-day values, latest-day substitutions, metric fallbacks, and failed sources.
- Label PUN Index GME as the national benchmark and the seven physical-zone values as zonal prices.
- Keep each selected item self-contained.
- Do not include unverifiable claims.
- Do not include raw AI scores in the final report unless the user asks for them.
- Do not copy long passages from original articles.
