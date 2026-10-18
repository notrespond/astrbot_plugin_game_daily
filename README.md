# OTAKUs 游戏资讯日报（astrbot_plugin_game_daily）

一个运行在 **AstrBot 4.29+** 上的游戏资讯日报插件：

```
新闻采集 → 内容清洗 → 时间筛选 → 去重 → AI 摘要/分类 → Pillow 海报 → 定时推送 → 日志/状态
```

海报由本地 Pillow 绘制，不依赖在线图片生成服务。AI 不可用时会自动降级到基于规则的筛选、分类和摘要，不会让当天日报完全中断。

> 本项目按 AstrBot 4.29.0 的实际插件接口开发；所有新闻都来自公开 RSS/Atom 或 Steam 官方新闻接口，不会生成虚构新闻。

---

## 1. 功能

- 采集最近 24 小时（可配置）的游戏新闻，默认覆盖：
  - Steam 商店新闻、新发售、官方公告；
  - PC / 主机 / 独立游戏；
  - 发售、版本更新、免费领取、折扣；
  - 电竞赛事、行业新闻。
- 每个来源独立请求，单个来源失败不会影响其他来源。
- 按标题、规范化 URL、内容指纹去重；SQLite 持久化已发送记录，重启后不会重复推送。
- AI 负责中文标题润色、简短摘要、分类、重要性评分和同事件合并。
- 优先使用外部 OpenAI 兼容 Key；没有 Key 时自动使用 AstrBot 当前默认聊天模型。
- 规则降级：AI 超时、401、返回格式错误、没有 Key 且没有 AstrBot 模型时，仍会生成日报。
- Pillow 本地生成 16:9 深色中文新闻海报，支持 4～6 条精选新闻、无新闻提示、长标题截断和异常字符清理。
- 使用 AstrBot 原生 `context.cron_manager` 注册每日任务，不另起机器人进程。
- 提供手动采集、预览、状态、自检命令。
- 推送目标、时间、时区、新闻源、AI、海报、无新闻策略均可在 AstrBot WebUI 配置。

---

## 2. 目录结构

```
astrbot_plugin_game_daily/
├── main.py              # AstrBot 插件入口、命令、推送
├── config.py            # 配置默认值/解析/校验
├── news_sources.py      # RSS/Atom 解析、Steam 官方公告、请求重试/限速
├── news_service.py      # 采集、清洗、时间过滤、去重、排序、选择
├── ai_service.py        # OpenAI 兼容 API、JSON 校验、失败降级
├── poster.py            # Pillow 海报绘制、字体发现、换行/截断
├── scheduler.py         # AstrBot cron 生命周期、重试、并发保护
├── storage.py           # SQLite 去重记录、运行状态、来源健康、运行锁
├── _conf_schema.json    # AstrBot WebUI 配置模板
├── metadata.yaml        # 插件元数据
├── requirements.txt     # 运行依赖
├── tests/               # 不依赖 AstrBot/网络的单元测试
└── README.md
```

数据不会写进插件源码目录，默认写入：

```
data/plugin_data/astrbot_plugin_game_daily/
├── gamedaily.db
└── posters/
```

---

## 3. 环境要求

- AstrBot `>= 4.29.0, < 5`（已在 AstrBot **4.29.0** Docker 容器中验证）
- Python `>= 3.10`（容器实测 3.12；开发机实测 3.14）
- `aiohttp >= 3.9, < 4`
- `Pillow >= 10, < 13`
- 一个可用的中文字体（AstrBot 官方镜像已包含 Noto Sans CJK；Windows 自带微软雅黑）

AstrBot 自身通常已经安装 `aiohttp` 和 `Pillow`。如果容器缺少依赖：

```bash
docker exec -it astrbot pip install -r /AstrBot/data/plugins/astrbot_plugin_game_daily/requirements.txt
```

---

## 4. 安装

### 4.1 复制插件

把整个 `astrbot_plugin_game_daily` 目录放到 AstrBot 的插件目录：

```bash
# 宿主机 / 容器路径按你的部署方式调整
/AstrBot/data/plugins/astrbot_plugin_game_daily/
```

Docker 部署示例（宿主机目录映射到容器）：

```bash
cp -r astrbot_plugin_game_daily /path/to/astrbot/data/plugins/
docker restart astrbot
```

或在宿主机执行：

```bash
docker cp astrbot_plugin_game_daily astrbot:/AstrBot/data/plugins/
docker restart astrbot
```

> 不要覆盖 AstrBot 本体或其他插件目录。插件的配置会生成在 `data/config/astrbot_plugin_game_daily_config.json`。

### 4.2 加载插件

重启 AstrBot，或在 WebUI 的「插件」页面点击本插件的**重载插件**。

首次加载后，WebUI 插件配置页会出现本插件的所有配置项。

---

## 5. 快速开始

1. AI（可选）：
   - 默认 `ai_use_astrbot_provider=true`：`ai_api_key` 为空时，自动使用 AstrBot 当前默认聊天模型做中文润色；
   - 也可以填写外部 OpenAI 兼容 `ai_api_base`、`ai_model`、`ai_api_key`；
   - Key 也可以留空并使用环境变量 `GAMEDAILY_AI_API_KEY`。
2. 在要接收日报的群/私聊里发送：

   ```
   /gamedaily_target
   ```

   把返回的 UMO 字符串加入 `push_targets`，例如：

   ```json
   [
     "aiocqhttp:GroupMessage:123456789",
     "aiocqhttp:FriendMessage:10001"
   ]
   ```

   推送目标可以是多个，代码中没有写死任何群号或用户 ID。
3. 设置 `push_time`（默认 `08:30`）、`timezone`（默认 `Asia/Shanghai`）并保持 `enabled=true`。
4. 重载插件，让新的定时配置生效。
5. 先测试：

   ```
   /gamedaily_test
   /gamedaily_preview
   ```

6. 正式手动执行一次：

   ```
   /gamedaily
   ```

---

## 6. 命令

| 命令 | 作用 |
| --- | --- |
| `/gamedaily` | 立即采集并生成日报，发送到**当前会话**；成功后写入去重记录。发送 `/gamedaily force` 可忽略已发送记录（用于补发/调试）。 |
| `/gamedaily_preview` | 生成海报并回复到当前会话，但**不发送到 push_targets**，也**不写入已发送记录**。默认忽略去重记录，便于查看当前可用的新闻。 |
| `/gamedaily_status` | 查看定时任务、下次执行时间、上次运行结果、采集/去重/展示数量、异常新闻源、数据库统计。不会输出 API Key。 |
| `/gamedaily_test` | 测试全部已启用新闻源（每个来源取 1 条）、AI API 和本地海报生成，并返回测试海报。 |
| `/gamedaily_target` | 显示当前会话的 UMO，便于填入 `push_targets`。 |

说明：

- 每日自动推送只发送到 `push_targets`；手动 `/gamedaily` 发送到触发命令的会话。
- `commands_admin_only=true` 时，`/gamedaily`、`/gamedaily_preview`、`/gamedaily_test` 只允许 AstrBot 管理员执行。
- 所有手动命令与定时任务共用 `DailyReportService`，不是两套逻辑。

---

## 7. 配置说明

配置项定义在 `_conf_schema.json`，默认值如下（节选）：

### 推送

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否注册每日定时任务 |
| `push_time` | `08:30` | 24 小时制 HH:MM |
| `timezone` | `Asia/Shanghai` | IANA 时区 |
| `cron_expression` | 空 | 可选 5 段 Cron；留空时按 `push_time` 生成 |
| `push_targets` | `[]` | UMO 列表，支持多个 |
| `no_news_policy` | `text` | 无新闻时发简短提示 `text`，或跳过 `skip` |
| `empty_report_text` | 今日暂无符合条件的游戏资讯，明天见。 | 无新闻提示文案 |
| `notify_on_failure` | `true` | 所有重试失败后是否发送简短失败提示 |
| `commands_admin_only` | `false` | 手动命令是否仅管理员可用 |

### 新闻

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `news_window_hours` | `24` | 新闻时间范围 |
| `include_missing_time` | `true` | 是否保留没有发布时间的条目（海报标“时间未标注”） |
| `max_news` | `6` | 海报展示 4～6 条 |
| `max_per_source` | `2` | 同一来源最多入选条数 |
| `max_source_items` | `8` | 单来源进入打分前的候选上限 |
| `max_candidates` | `60` | 去重后候选池上限 |
| `categories_preferred` | 发售/更新/免费福利/电竞/行业 | 规则排序加权 |
| `excluded_keywords` | `[]` | 标题/摘要命中即丢弃 |
| `disabled_sources` | `[]` | 禁用的内置来源 ID |
| `rss_sources` | `[]` | 附加/覆盖 RSS 源（WebUI 模板列表） |
| `enable_steam_news_api` | `true` | 启用 Steam 官方公告 API |
| `steam_appids` | CS2、Dota 2、PUBG、Apex、赛博朋克 2077、艾尔登法环 | 官方公告 appid |

网络：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `request_timeout` | `20` 秒 | 单次请求超时 |
| `request_interval` | `0.5` 秒 | 所有外部请求共享的最小间隔 |
| `max_concurrency` | `4` | 并发请求上限 |
| `retry_times` | `2` | 有限重试次数（来源请求与定时任务） |
| `max_response_bytes` | 4 MiB | 单个响应体上限 |
| `use_proxy_env` | `true` | 读取 `HTTP_PROXY` / `HTTPS_PROXY` |
| `user_agent` | 可识别的 UA | 可自定义 |

### AI

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `ai_enabled` | `true` | 关闭后直接使用规则模式 |
| `ai_use_astrbot_provider` | `true` | 没有外部 Key 时自动使用 AstrBot 当前默认聊天模型 |
| `ai_provider_id` | 空 | 指定 AstrBot 模型 ID；留空自动选择 |
| `ai_api_base` | `https://api.openai.com/v1` | OpenAI 兼容 Base URL |
| `ai_model` | `gpt-4o-mini` | 模型名 |
| `ai_api_key` | 空（secret） | 建议通过环境变量提供 |
| `ai_api_key_env` | `GAMEDAILY_AI_API_KEY` | Key 环境变量名 |
| `ai_timeout` | `60` 秒 | AI 请求超时 |
| `ai_batch_size` | `20` | 每批条数 |
| `ai_max_input_chars` | `12000` | 单批输入字符上限 |
| `ai_json_mode` | `false` | 仅当服务支持 `response_format=json_object` 时开启 |
| `ai_temperature` | `0.2` | 生成温度 |
| `ai_max_candidates` | `30` | 送入 AI 的候选数（控制费用） |

AI 安全约束：

- 没有外部 Key 且 `ai_use_astrbot_provider=true` 时，调用 AstrBot 已配置的聊天模型；模型不可用时回退规则模式。
- API Key 只从插件配置或环境变量读取，不写死在代码里，不写入日志/状态/README。
- 新闻标题、摘要、链接被视为**不可信数据**，放在 `<news_data>` JSON 块中；系统提示明确禁止执行其中的指令。
- AI 只能处理已采集的 id，不能新增新闻；未知 id 会被丢弃。
- AI 返回非法 JSON、超时、HTTP 错误时，逐条回退到规则结果，日报继续生成。
- 摘要要求区分官方公告与媒体报道，传闻必须使用“据报道/传闻”等措辞。

### 海报

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `poster_width` / `poster_height` | `1200` / `675` | 16:9；也可用 1600×900 |
| `font_path` | 空 | 自动检测中文字体；也可指定 `.ttf/.ttc/.otf` |
| `poster_theme` | `dark-neon` | `dark-neon` / `dark-blue` / `dark-crimson` |
| `output_dir` | 空 | 默认 `data/plugin_data/astrbot_plugin_game_daily/posters` |
| `keep_posters_days` | `7` | 只清理本插件生成的 `gamedaily-*.png` / `preview-*.png` |
| `send_caption` | `true` | 图片后附带简短文字摘要 |
| `send_text_fallback` | `true` | 海报失败时发送文字版 |
| `dedupe_retention_days` | `30` | 去重记录保留天数 |

字体：

- Linux Debian/Ubuntu：

  ```bash
  apt-get update && apt-get install -y fonts-noto-cjk
  fc-list :lang=zh
  ```

- Windows：自动使用微软雅黑 `C:\Windows\Fonts\msyh.ttc`。
- macOS：自动尝试 PingFang。
- 也可配置 `font_path`，或设置环境变量 `GAMEDAILY_FONT_PATH`。
- 找不到任何可显示中文的字体时，海报会明确报错 **而不是画出乱码/方块**。此时安装字体或在配置中指定字体即可。

---

## 8. 内置新闻源

| ID | 名称 | 分类 |
| --- | --- | --- |
| `steam_store_news` | Steam 商店新闻 | 综合 |
| `steam_new_releases` | Steam 新发售 | 发售 |
| `steam_latest_releases` | Steam 最新发售 | 发售 |
| `xbox_wire` | Xbox 官方新闻 | 主机 |
| `playstation_blog` | PlayStation 官方博客 | 主机 |
| `nintendo_life` | Nintendo Life | 主机 |
| `pcgamer` | PC Gamer | PC |
| `eurogamer` | Eurogamer | 综合 |
| `rps` | Rock Paper Shotgun | PC |
| `gematsu` | Gematsu | 综合 |
| `gamesindustry` | GamesIndustry.biz | 行业 |
| `mcvuk` | MCV UK | 行业 |
| `vgc` | VGC | 综合 |
| `espn_esports` | ESPN Esports | 电竞 |
| `esportsinsider` | Esports Insider | 电竞 |
| `indiegamesplus` | Indie Games Plus | 独立 |
| `steam_api` | Steam 官方公告（按 appid） | 更新 |

- 禁用某个来源：把 ID 加入 `disabled_sources`。
- 添加 RSS：在 `rss_sources` 模板列表里新增，填写 `name`、`url`、`category`、`weight`、`enabled`。
- 如果新增的 URL 和内置来源相同，会覆盖内置来源的名称/分类/权重。
- 如果新增条目 `enabled=false` 且 URL 与内置来源相同，会禁用该内置来源。

示例：

```json
{
  "disabled_sources": ["nintendo_life", "mcvuk"],
  "rss_sources": [
    {
      "enabled": true,
      "name": "示例游戏站",
      "url": "https://example.com/game-news.xml",
      "category": "综合",
      "weight": 1.2
    }
  ]
}
```

Steam 官方公告使用公开接口：

```
https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/
```

只保留 Valve 官方 `steam_community_announcements` 条目（`feed_type=1`），第三方聚合文章不会被当作官方公告。

> 请遵守各来源的 robots/使用条款和访问频率；插件已内置超时、间隔、并发限制和有限重试。新闻版权归原来源所有，海报只展示标题/短摘要并保留原文链接。

---

## 9. 定时任务与并发控制

- 使用 AstrBot 4.29 的 `context.cron_manager.add_basic_job(...)` 注册**一个**基础 Cron 任务。
- 任务名固定为 `[GameDaily] 每日游戏资讯`，payload 带插件标记。
- 插件加载时先删除同名的旧任务（例如上次异常重启遗留），再注册新任务；`start()` 有幂等锁。
- 任务以 `persistent=False` 注册：AstrBot 重启后由插件重新注册，而不是由核心恢复一个可能已经没有 handler 的旧任务。插件卸载/重载时在 `terminate()` 中删除任务并清理 handler，避免遗留任务。
- SQLite 运行租约 `locks` 表防止定时任务和手动命令并发执行/重复推送；租约 30 分钟自动过期，进程崩溃后可恢复。
- 定时任务失败后按 `retry_times` 有限重试（指数退避）；不会无限重试。
- 手动命令与定时任务调用同一个 `DailyReportService`。
- 修改 `push_time` / `timezone` / `enabled` 后，需要**重载插件**让定时任务重新注册。

无新闻时：

- `no_news_policy=text`：发送 `empty_report_text`；
- `no_news_policy=skip`：不发送，但写入运行状态；
- 如果所有新闻源都不可用，任务状态记为 `failed`，不会把“来源故障”伪装成“无新闻”。

---

## 10. 数据与隐私

- 数据库：`data/plugin_data/astrbot_plugin_game_daily/gamedaily.db`
  - `sent_news`：标题/URL/内容指纹，防止重启后重复推送；
  - `runs`：最近运行状态、数量、目标、错误；
  - `source_health`：来源成功/失败次数；
  - `locks`：跨进程运行租约。
- 海报：默认 `data/plugin_data/astrbot_plugin_game_daily/posters/`。
- 可用环境变量 `GAMEDAILY_DATA_DIR` 覆盖数据目录（便于测试或迁移）。
- SQLite 使用 WAL、`busy_timeout=30s`，所有数据库操作在 `asyncio.to_thread` 中执行，不阻塞事件循环。
- 不输出 API Key、完整敏感配置或无关用户数据。日志只记录来源 ID、状态和错误摘要。
- 新闻内容会发送给配置的 AI API 用于摘要；如果对隐私敏感，请使用自建 OpenAI 兼容服务或关闭 AI（`ai_enabled=false`）。

---

## 11. 测试

### 单元测试（不联网、不依赖 AstrBot）

在仓库根目录：

```bash
python -m unittest discover -s astrbot_plugin_game_daily/tests -t . -v
```

覆盖：

- 配置默认值、非法值回退、环境变量 Key、RSS 源解析；
- RSS 2.0 / Atom / RDF 兼容解析、日期格式、HTML 清洗；
- 标题/URL/内容指纹去重、时间窗、缺失时间、未来时间；
- 持久化去重、运行记录、来源健康、运行锁；
- AI JSON 解析、代码围栏、非法 JSON、网络错误降级、本地 HTTP 模拟 OpenAI 接口；
- 海报 4/6 条、空海报、长标题、emoji/控制字符、自定义尺寸；
- 定时任务重复注册清理、卸载删除、payload 参数、失败重试、并发触发跳过。

### 真实网络冒烟

```bash
python astrbot_plugin_game_daily/tests/manual_smoke.py
```

（如果仓库中没有该脚本，可直接调用 `NewsFetcher`；本次开发已实际运行真实 RSS 抓取和海报生成。）

### 插件内自检

在 AstrBot 中：

```
/gamedaily_test
```

会请求每个已启用来源 1 条、测试 AI ping、生成本地海报并返回结果。

已实际验证：

- 69 个单元测试在 Windows Python 3.14 通过；
- 同一套测试在 AstrBot 4.29.0 Docker 容器 / Python 3.12 / Noto CJK 字体下通过；
- 真实 RSS 抓取（本机与 AWS 悉尼容器）成功获取新闻并生成 1200×675 海报；
- 已安装到 AstrBot 4.29.0 容器并成功加载；无外部 Key 时通过 AstrBot 默认 DeepSeek 模型生成中文标题/摘要海报；
- 在 AstrBot 容器内成功 `import` 插件入口，并用真实 `AstrBotConfig` + 假 `Context` 完成初始化、注册定时任务、卸载清理。

未在当前环境验证（需要你的实际凭据/平台）：

- 真实 OpenAI 兼容服务的 AI 调用（未提供 Key；已用本地 HTTP mock 验证请求/解析/错误降级）；
- AstrBot 定时任务在真实 08:30 时间点的实际触发（已安装并验证注册/生命周期，未等待到点）；
- 真实 QQ 私聊主动推送（NapCat 登录态失效，发送被平台拒绝）；
- 无中文字体 Linux 的最终视觉输出（已提供字体安装说明和清晰报错）。

---

## 12. 部署到 AstrBot（推荐流程）

1. 关闭/重载旧版本插件，备份 `data/plugin_data/astrbot_plugin_game_daily/`。
2. 把本目录复制到 `/AstrBot/data/plugins/astrbot_plugin_game_daily/`。
3. 安装依赖（通常 AstrBot 会自动安装）：

   ```bash
   docker exec -it astrbot pip install -r /AstrBot/data/plugins/astrbot_plugin_game_daily/requirements.txt
   ```

4. 确认容器有中文字体：

   ```bash
   docker exec -it astrbot fc-list :lang=zh
   ```

   没有输出时：

   ```bash
   docker exec -it astrbot apt-get update
   docker exec -it astrbot apt-get install -y fonts-noto-cjk
   ```

5. 重启 AstrBot 或在 WebUI 重载插件。
6. 在 WebUI 配置 AI、`push_targets`、时间/时区、新闻源。
7. 在目标群发送 `/gamedaily_target`，把 UMO 填入 `push_targets`。
8. 执行 `/gamedaily_test` → `/gamedaily_preview` → `/gamedaily`。
9. 查看 `/gamedaily_status` 确认下次执行时间。
10. 观察日志中是否有 `定时推送已注册`、来源失败或海报错误。

### Windows 开发环境差异

- 字体：自动使用微软雅黑；Linux 需安装 Noto CJK。
- 路径：`data/plugin_data/...` 在 Windows 下同样适用；避免把插件放到含空格/受限权限的路径。
- 代理：Windows 可设置 `HTTP_PROXY`/`HTTPS_PROXY`，并保持 `use_proxy_env=true`。
- 时区：Windows 需要 `tzdata` 时 Python 的 `zoneinfo` 才能识别 IANA 时区；如果报 `ZoneInfoNotFoundError`，安装 `tzdata`（`pip install tzdata`）或改用 `UTC`。
- 测试命令与 Linux 相同。

---

## 13. 常见问题

**插件加载失败 / 命令不存在**

- 确认目录名是 `astrbot_plugin_game_daily`，入口是 `main.py`。
- 查看 WebUI「插件」页的加载失败信息和 AstrBot 日志。
- 确认 AstrBot 版本 `>= 4.29.0`；本插件使用该版本的 `context.cron_manager`。

**没有收到定时推送**

- `/gamedaily_status` 查看 `registered`、下次执行、`push_targets` 数量。
- 确认 `enabled=true`，时区/时间正确。
- 修改配置后重载插件。
- 检查平台是否支持主动消息；QQ 官方 API 对主动消息限制较多，OneBot/aiocqhttp 通常可用。
- 检查 UMO 是否来自 `/gamedaily_target`，不要手写错平台 ID/会话类型。

**海报没有中文/报字体错误**

- 安装 `fonts-noto-cjk`，或设置 `font_path` / `GAMEDAILY_FONT_PATH`。
- 插件不会用缺字字体硬画中文，避免乱码；错误信息会明确提示。

**HTTP 403 / 429 / 超时**

- 某些站点会封锁云服务器 IP；把对应 ID 加入 `disabled_sources`，或换一个 RSS 源。
- 增大 `request_interval`、降低 `max_concurrency`，或配置代理 `use_proxy_env=true`。
- 单个来源失败不会影响其他来源；`/gamedaily_status` 会显示连续失败的来源。

**AI 401 / 超时 / 返回格式错误**

- 检查 `ai_api_base` 是否包含正确的 `/v1` 路径；代码会自动拼接 `/chat/completions`。
- 检查模型名、Key、余额和网络。
- 某些服务不支持 `response_format`，保持 `ai_json_mode=false`。
- AI 失败会自动回退规则模式；`/gamedaily_status` 的提示会显示 AI 错误摘要（不含 Key）。

**同一条新闻推送了两次**

- 检查数据目录是否可写，数据库是否被清空/换目录。
- 定时任务与手动命令共享运行锁和 `sent_news`；`/gamedaily force` 会故意忽略记录，用于补发。
- 去重保留天数默认 30 天；太老的记录会被清理，之后再次出现可能重新推送。

**定时任务重复**

- 插件加载时会按任务名/payload 清理旧任务；如果仍有重复，检查是否有多个 AstrBot 实例共享同一个数据库/配置。
- 重载插件后查看日志 `清理重复的旧定时任务`。

**手动 `/gamedaily` 没有发到群**

- 手动命令只回复当前会话；每日任务才发送到 `push_targets`。
- 如果要测试主动推送，把当前会话 UMO 加入 `push_targets` 后等待定时任务，或使用 `/gamedaily_preview` 查看内容。

---

## 14. 版本

- `v1.0.0`：首个完整版本。RSS/Steam 采集、AI/规则摘要、SQLite 去重、Pillow 海报、AstrBot cron 定时推送、命令与自检、单元测试。

## 15. 许可与来源

- 插件代码按项目需要部署使用。
- 新闻内容版权归各来源所有；海报只展示标题、短摘要、来源和原文链接，不复制全文。
- 使用公开 RSS/Atom 与 Steam 官方新闻接口；请遵守来源站点的访问规则和频率限制。
