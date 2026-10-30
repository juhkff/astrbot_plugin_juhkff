# astrbot_plugin_juhkff

综合功能插件，移植自 Yunzai 插件 [juhkff-plugin](https://github.com/juhkff/juhkff-plugin)。

全部功能聚合在一个插件里，配置页按功能分组，每组可独立开关：

| 分组 | 功能 | 状态 |
|---|---|---|
| `global` | 总开关、代理、时区、唤醒前缀 | ✅ 可用 |
| `share_parse` | B 站 / 小黑盒 / 通用 JSON 卡片解析 | ✅ 已实现 |
| `emoji_steal` | 偷表情（按群内重复出现的图片收藏） | ✅ 已实现 |
| `daily_report` | 日报简报（一言 / 60 秒读世界 / B 站热搜 / IT 之家 / 番剧日历） | ✅ 已实现 |
| `active_chat` | 主动群聊（概率接话 / @ 必回 / 多 Key / 群内概率覆盖） | ✅ 已实现 |
| `pixiv_push` | Pixiv 榜单新图订阅推送 | ✅ 已实现 |
| `help` | 帮助图（`#帮助`，HTML 渲染） | ✅ 已实现 |

所有分组默认关闭，按需在「配置」里逐个打开。

## 安装

```bash
cd /home/ubuntu/projects/AstrBot
git clone <本仓库地址> data/plugins/astrbot_plugin_juhkff
# 或者直接把目录拷进去
cp -r /path/to/astrbot_plugin_juhkff data/plugins/
```

然后在 WebUI「插件管理」里点重载（或重启 AstrBot）。

可选依赖（Redis 历史、日报出图、Pixiv 图片压缩）：

```bash
.venv/bin/pip install pillow
```

Redis 历史需要 `redis>=5.0`（插件 requirements.txt 已声明，框架加载时会自动装）
以及一个可连的 Redis 服务；没装 Pillow 也能用：日报自动退回文字模式，Pixiv
图片超限时只发链接；没有 Redis 时不记录主动群聊历史。

首次加载后：

- 插件列表出现「综合功能插件」；
- 点「配置」能看到 8 个分组；
- 群里发 `/juhkff` 应返回模块状态。

## 指令

| 指令 | 说明 |
|---|---|
| `/juhkff` | 查看插件状态、各模块启停、数据目录 |
| `#帮助` | 生成一张帮助图（功能列表） |
| `#日报` / `/日报` | 生成今日简报（一言、60 秒读世界、B 站热搜、IT 之家、番剧） |
| `/pixiv` | 查看最近一次抓到的 Pixiv 作品 |

## 数据目录

```
data/plugin_data/astrbot_plugin_juhkff/
├── emoji/               # 偷到的表情
├── emoji_library.json   # 表情索引
├── fingerprints.json    # 图片指纹 + 窗口内出现次数
├── daily_report.png     # 日报简报缓存（预生成）
├── pixiv/               # 已下载的 Pixiv 图片
└── pixiv_seen.json      # 已推送过的作品 id
```

## 实现说明

- **卡片解析**：不依赖第三方短链服务。B 站封面走 `b23.tv` 重定向 → BV 号 →
  `api.bilibili.com/x/web-interface/view` 取 `data.pic`；其余平台直接取卡片自带的
  `preview`。
- **偷表情**：对齐上游——同一张图（sha256）在 3 天窗口内被同一个群发第二次就收藏，
  按群分目录存（`emoji/<群号>/`），单张 ≤100KB、每群最多 50 张；每条群消息先按
  `default_reply_rate`、再按 `default_emoji_rate` 掷两次骰子决定要不要随机发一张。
- **日报**：对齐上游 `dailyReport.ts`——`v1.hitokoto.cn` 一言、alapi 或
  `60s-api.viki.moe` 的 60 秒新闻、`s.search.bilibili.com` 热搜、IT 之家 RSS、
  `api.bgm.tv/calendar` 当日番剧，加公历节日与周末倒数，用 Jinja2 模板交给
  AstrBot 的 HTML 渲染器出图；`#日报` 即时生成，`daily_report_time` 定时推送。
- **一个调试开关**：`global.debug`（全局设置 → 调试日志）打开后，各模块的明细
  （Redis、抓链接失败原因、模型能力判断等）用 INFO 输出，主动群聊还会打印每次
  请求的 system 人设、本条 user 消息与历史条数；内容含聊天原文，排查完要关。
- **主动群聊**：配置项与行为对齐上游 `autoReply.ts`——被 `@` 或引用回复时按
  `default_reply_at_bot` 必回，否则按 `default_chat_rate` 掷骰子，没有冷却也没有最小
  消息数；`group_chat_rate` 按群覆盖概率与「被叫必回」；以唤醒前缀（AstrBot 全局
  `wake_prefix`，默认 `/`）开头的消息、以及 `@全体` 这类「被唤醒但不是在叫它」的消息
  按指令处理，不记录也不回复，避免和 `/juhkff`、`#帮助` 抢；`use_context + max_history_length` 决定带多少条上下文；
  `chat_prompts + chat_prompt_apply` 选人设（支持 `{{botName}}`）；`attach_url_analysis`
  会先抓消息里链接的正文；图片两头都带：
  - 当前回合的图经 `image_urls` 直接给多模态模型看；
  - 历史里的图以 `ImageURLPart` 跟着 `contexts` 一起传（只存 http(s) 地址，不落
    base64），所以在 `max_history_length` 窗口内，模型能回看前面几轮的图；本地
    临时文件不进历史，图链过期取不到时会自动退回纯文本；
  - 发之前先看 provider 的 `modalities` 有没有 `image`：明确没有时，按 AstrBot
    核心 `_ensure_img_caption()` 的做法调用配置里的「图片转述模型」
    （`provider_settings.default_image_caption_provider_id`）把图转成文字；
    没配转述模型或转述失败就退纯文本；带图请求报错也会剥掉上下文里的图重试一次。
  - 每条历史带时间前缀：当天 `20:31`，跨天 `10-28 20:31`（对齐上游
    `formatDateDetail()` 的完整 `YYYY/MM/DD HH:mm:ss`，只是压缩了长度），本回合
    这条 user 消息的首行同样带时间，模型能据此判断新旧。
  - @/引用 的段落在适配器那层就被剥掉了，所以入库时会在文本后补标记：
    `（当时@了你）`（消息里真有 At 段）或 `（当时引用了你）`（引用回复了机器人），
    后续回合能看出这条是在叫自己；本回合这条 user 消息的首行同样写明
    `（这条是在@你…）` / `（这条是在引用回复你…）`。「只接着最新这条说、
    不要回复历史」属于行为约束，直接写在人设（system）里——默认人设已带，
    改 `chat_prompts` 时别把这段删掉。
  - 正文里用和历史上同一套 `[图片1] [图片2]` 占位（`小明：在吗 [图片1]`），
    不额外加说明段落。模型统一走 AstrBot 里配置的对话 provider：
    `provider` 留空就用当前会话的对话模型，不再由插件自己管理 Key / 模型名 /
    接口地址。
  历史只存 Redis（`redis_url`，key 为 `redis_key_prefix:umo` 的 LIST，只留最近
  64 条，内存不留副本），每轮要上下文时现拉，对齐上游 `loadContext()` 的做法；
  每条写入刷新 `history_ttl_hours`（默认 12 小时，对齐上游 `EX 12*60*60`，0 为
  不过期）。`clear_history_on_start`（默认开）在 AstrBot 进程重启时清空该前缀下的
  所有会话；同一进程内的插件热重载（保存配置）不会清，靠 Redis 里的 `__owner`
  标记区分上次启动的 pid。
  关掉则重启后接着用旧上下文。没填地址、没装 `redis` 客户端或连不上服务时不记录
  历史（只按当前消息回复），不会退回内存。
- **Pixiv**：只用公开榜单接口（不需要登录/cookie），带 Referer 下载原图，失败自动
  降级到 regular 与 `i.pixiv.re` 镜像。
- **调度**：所有定时任务交给 AstrBot 的 `context.cron_manager`（APScheduler，
  带时区与失败日志），插件卸载时 `Scheduler.shutdown()` 逐个删除 job。

## 复用了哪些 AstrBot 内置能力

- 路径：`get_astrbot_plugin_data_path()` + `io.ensure_dir()`
- 图片下载：`io.download_image_by_url()`（有自定义请求头时才走自建 aiohttp，
  例如 Pixiv 需要 Referer）
- 临时文件：`io.save_temp_img()`
- 日报出图：`context.html_renderer.render_custom_template()`（Jinja2 HTML 卡片），
  失败自动退回本地 Pillow
- 消息发送 / LLM 调用 / 配置 / 日志：`event.chain_result()`、
  `context.llm_generate()`、`AstrBotConfig`、`astrbot.api.logger`
- 定时任务：`context.cron_manager`（APScheduler），`Scheduler` 只做
  「HH:MM / 每 N 分钟 / Quartz 6 段 → crontab」的翻译与 job 回收

## 与上游的差异

- 「骰子」「命令预设」目前没有保留（开发过程中已移除），需要时可以从 git 历史里取回。
- 「帮助图」已按上游行为移植，改为复用 AstrBot 的 HTML 渲染，不依赖 Yunzai 的渲染器与字体资源。
- Pixiv 上游是「按画师订阅 + 可选 R18」，这里仍是「榜单新图」；画师订阅与
  AI 生成（豆包视频/图片、SiliconFlow 语音）尚未移植。
- 主动群聊已移除早期版本自造的 `reply_mode=random`、`interval_sec`、`min_messages`、
  `trigger_mode/keywords`、`whitelist` 等上游没有的项；`useEmotion` 每日心情也去掉了
  （实测用处不大，且每天要额外调一次模型）。

## 许可

GPL-3.0，与上游 juhkff-plugin 一致。
