# astrbot_plugin_juhkff

综合功能插件，移植自 Yunzai 插件 [juhkff-plugin](https://github.com/juhkff/juhkff-plugin)。

全部功能聚合在一个插件里，配置页按功能分组，每组可独立开关：

| 分组 | 功能 | 状态 |
|---|---|---|
| `global` | 总开关、代理、时区、唤醒前缀 | ✅ 可用 |
| `share_parse` | B 站 / 小黑盒 / 通用 JSON 卡片解析 | ✅ 已实现 |
| `emoji_steal` | 偷表情（按群内重复出现的图片收藏） | ✅ 已实现 |
| `daily_report` | 今日日报（时政 / 国内社会 / 游戏资讯 / 数码，五种随机或固定风格） | ✅ 已实现 |
| `active_chat` | 主动群聊（概率接话 / @ 必回 / 多 Key / 群内概率覆盖） | ✅ 已实现 |
| `pixiv_push` | Pixiv 画师订阅与新插画推送 | ✅ 已实现 |
| `help` | 帮助图（`#帮助`，指令功能与自动功能分区，本地 HTML 渲染） | ✅ 已实现 |

总开关与六个功能模块默认开启，可在「配置」里按需关闭。日报定时推送默认开启，但必须配置推送群才生效；提前生成仍需单独开启。

## 安装

```bash
cd /home/ubuntu/projects/AstrBot
git clone <本仓库地址> data/plugins/astrbot_plugin_juhkff
# 或者直接把目录拷进去
cp -r /path/to/astrbot_plugin_juhkff data/plugins/
```

然后在 WebUI「插件管理」里点重载（或重启 AstrBot）。

相关依赖（Redis 历史、日报出图、Pixiv 图片验证及可选压缩）：

```bash
.venv/bin/pip install pillow
# 日报/帮助图的本机渲染（Chromium 需单独下一次，约 120MB）
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
# 国内下载慢可以走镜像
# PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright .venv/bin/playwright install chromium
```

Redis 历史需要 `redis>=5.0`（插件 requirements.txt 已声明，框架加载时会自动装）
以及一个可连的 Redis 服务；没装 playwright/Chromium 时日报与帮助图会直接报错。
Pixiv 配图下载、验证或发送失败时保留推送进度，递增等待后重试；默认不压缩图片。
没有 Redis 时不记录主动群聊历史。

首次加载后：

- 插件列表出现「综合功能插件」；
- 点「配置」能看到 7 个分组；
- 群里发 `/juhkff` 应返回模块状态。

需要指定群的地方（主动群聊的分群概率、偷表情的分群概率、日报推送群、Pixiv
推送目标）都接入了 AstrBot 内置的会话选择器：点输入框右侧图标会弹出按群名/别名
搜索的对话框，可多选；保存的是会话标识（`aiocqhttp:GroupMessage:123456`），
代码也兼容手填纯群号。

## 指令

| 指令 | 说明 |
|---|---|
| `/juhkff` | 查看插件状态、各模块启停、数据目录 |
| `#帮助` | 生成帮助图：有指令的功能集中展示，无指令的自动功能单独分区；文字模式也按此分组 |
| `#日报` | 按设置的随机或固定风格生成今日日报 |
| `#pixiv订阅 UID` | 在当前群订阅指定画师，首次仅记录最新作品，不补推旧作 |
| `#pixiv取消订阅 UID` | 取消当前群对该画师的订阅，其他群不受影响 |

## 数据目录

```
data/plugin_data/astrbot_plugin_juhkff/
├── emoji/               # 偷到的表情
├── emoji_index.json     # 表情索引
├── fingerprints.json    # 图片指纹 + 窗口内出现次数
├── daily_report/         # 按日期缓存新闻快照、配图与五种风格的日报图
├── pixiv/               # 已下载的 Pixiv 全部分页及可选压缩派生图
└── pixiv_subscriptions.json # 同进程内的检查点、各群投递记录与递增退避状态
```

## 实现说明

- **卡片解析**：只识别 B 站的准确域名，`b23.tv` 短链的每次跳转也必须留在 B 站域名内。
  从视频路径提取 BV/av 号，分别用 `bvid`/`aid` 查询封面。所有平台的 `preview` 和 API
  封面均先安全下载，再由 Pillow 验证并解码（最多 10 MiB、2000 万像素），以图片字节
  发送；不会让 NapCat 直接访问卡片给出的图片 URL。封面失败时保留文字信息。
- **偷表情**：同一张图（sha256）默认在 72 小时内被同群发送第二次时收藏，
  判定窗口最多可选 336 小时，0 表示永不过期。按群分目录存（`emoji/<群号>/`），
  `size_limit_mb` 默认 2 MB、最多可选 10 MB；每群默认最多 30 张、最多可选 100 张。
  表情包判断默认开启，但必须选择 `judge_provider` 才会调用模型；留空直接跳过判断，
  不使用群聊模型。只接受完整的肯定或否定答案，解释句不会按首字误判；判为「否」就
  不计数并清掉旧计数，回答不明确或模型报错时仍按重复出现规则收藏。
  每条群消息只按 `default_emoji_rate`（或分群 `emoji_rate`）判断一次是否发图，
  不再另设接话概率。命中后优先使用本群收藏；没有收藏时使用 `gallery_path` 中的
  png/jpg/gif/webp。发图概率设为 0 时两种图库都不发图，收藏仍正常进行。
  本地图库相对路径从插件数据目录算起，目录不存在会自动创建，增删在 60 秒内生效。
- **日报**：时政、国内社会、游戏资讯、数码四个板块，每类最多 12 条；显示实际条数与
  新闻来源、发布时间，缺失时不编造、不用热门视频代替游戏新闻。数据源按内容相关性
  确定优先级，依次请求，首个有合格数据的来源即停止；当来源不足 12 条时保留实际条数。
  时政与社会优先中国新闻网，数码优先 IT之家、其次超能网和爱范儿；游戏先尝试游民星空、
  3DM、机核、游研社、触乐、17173，再尝试限定上述媒体的 Google News RSS。
  配图优先取原文；游戏聚合新闻缺图时可使用同一游戏的媒体报道配图，并单独标明图源。
  无法确认相关配图时正常展示标题，不填无关图片。
  五款样式统一标题「今日日报」：**1 红黑索引、2 深色电讯、3 纸张手帐、4 撞色网格、
  5 青绿四栏**。设置中的 `daily_report.style` 默认 `random`，每次手动请求或一次定时
  推送随机抽取一款（可能连续相同），也可选固定风格。群里只需发送 `#日报`，
  风格统一在设置页选择，命令不接受风格参数。
  公历、星期、农历、周末、下个节气、元旦和春节由日期与 `lunar-python` 动态计算，
  使用全局时区；春节不固定公历日期。正文需系统中文字体（如 Noto Sans SC）。
  出图使用**本机 Chromium**：前四款 960px、第五款 1440px 视口，2.5 倍像素密度；
  CSS 与配图均内联，渲染失败直接报错，不调用远端渲染或发送文字替代品。
  新闻快照按日期缓存，图片按日期和风格分别缓存，因此换风格无需重新抓取新闻。
  手动生成、提前生成和定时推送共用该流程；缓存命中无需等待其他样式生成，缓存未命中
  时串行生成并再次检查缓存，避免重复抓取和渲染。一次定时推送向所有目标群发送同一款图片。
  提前生成在随机模式下准备五款图片，固定模式只准备选定的一款；成功图片不重复生成。
  `pre_handle_retry_sec` 默认 120 秒，提前生成失败最多再重试 5 次。
- **一个调试开关**：`global.debug`（全局设置 → 详细日志）默认开启，各模块的明细
  （Redis、抓链接失败原因、模型能力判断等）用 INFO 输出，主动群聊还会打印每次
  请求的 system 人设、本条 user 消息与历史条数；内容含聊天原文。
- **主动群聊**：配置项与行为对齐上游 `autoReply.ts`——被 `@` 或引用回复时按
  `default_reply_at_bot` 必回，否则按 `default_chat_rate` 掷骰子，没有冷却也没有最小
  消息数；`group_chat_rate` 按群覆盖概率与「被叫必回」。框架已识别的命令交给原处理器；
  本插件的日报、帮助、Pixiv 命令优先独占处理。普通群消息先记录一次历史，再交给自动
  模块；卡片或表情先回复时仍留下群友原消息，但不再触发闲聊。主动群聊最后决定是否
  接话，使用记录当前消息之前的历史，避免重复上下文；命令处理失败也不会转为闲聊。命令即使带 `@机器人` 或引用机器人消息，
  也不进入闲聊或写入历史；判断同时检查框架命令匹配结果与原始文本中的 `#`、`/` 和全局
  `wake_prefix`，避免前缀被框架剥除后误判。`@全体` 这类「被唤醒但不是在叫它」的消息
  同样跳过；默认接话概率为 0.2，`use_context + max_history_length` 决定带多少条上下文，
  默认携带 20 条；设为 0 时只发当前消息；
  `persona_prompt` 就是人设（system 内容，支持 `{{botName}}`；配置页里点编辑器右上角可全屏编辑）；`url_mode`
  三选一（默认模型处理）——**关闭**不处理链接；**模型处理**给模型一个 `fetch_url` 工具，它觉得需要才去读（多一次模型请求；
  工具可以带一个可选的 `focus`，写「我想找什么」，插件会按这个词挑出相关段落而不是只给开头，同一链接
  10 分钟内只抓一次，反复调用按不同关注点重新挑，不重复走网络）；**提取**先把正文抓下来（长度由
  `url_content_chars` 控制，默认 400 字/链接），作为独立的参考消息交给模型，不拼进群友原话，也**不写进历史**——后续轮次想再看内容，用模型处理模式让模型自己现抓；图片两头都带：
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
    改 `persona_prompt` 时别把这段删掉。
  - 正文里用和历史上同一套 `[图片1] [图片2]` 占位（`小明：在吗 [图片1]`），
    不额外加说明段落。模型统一走 AstrBot 里配置的对话 provider：
    `provider` 留空就用当前会话的对话模型，不再由插件自己管理 Key / 模型名 /
    接口地址。
  历史只存 Redis（`redis_url`，key 为 `redis_key_prefix:umo` 的 LIST，只留最近
  64 条，内存不留副本），每轮要上下文时现拉，对齐上游 `loadContext()` 的做法；
  `history_ttl_hours` 默认 0，不自动过期，并会移除已有历史的旧过期时间；设为正数时，
  每条写入都会刷新过期时间。`clear_history_on_start`（默认开）在 AstrBot 进程重启时清空该前缀下的
  所有会话；同一进程内的插件热重载（保存配置）不会清，靠 Redis 里的 `__owner`
  标记区分上次启动的 pid。
  关掉则重启后接着用旧上下文。没填地址、没装 `redis` 客户端或连不上服务时不记录
  历史（只按当前消息回复），不会退回内存。
- **Pixiv**：通过公开 `profile/all` 接口检查订阅画师的插画，不混入漫画；首次成功
  检查只记录最新作品，之后按 `check_minutes`（默认 60 分钟）补齐检查点之后的全部新作，
  按作品 ID 从旧到新排列。多图作品通过官方 pages 接口获取全部分页，不猜测图片地址。
  同一画师本轮的新作与所有分页集中为合并转发，每页包含作者、标题、作品链接和页码；
  发送失败不退回逐图发送，避免刷屏。接收平台需要支持 AstrBot 的合并转发组件，
  当前 QQ/NapCat 对应 OneBot 的 `send_group_forward_msg`。
  `#pixiv订阅 UID` 将当前群加入接收群，重复订阅不会重复添加；取消订阅只影响当前群。
  命令修改会保存到插件配置，设置页的 `subscriptions` 也可用会话选择器指定多个群；
  无有效订阅时不请求接口。订阅保存在原生插件 JSON 配置，不使用数据库。
  图片带 Pixiv Referer，使用全局代理，优先下载原图；原图不可用时再尝试替代地址。
  `max_image_mb` 默认 **0（不压缩）**：验证图片后保留原始字节，不缩小、不转换格式。
  仅手动设置正数阈值时压缩超过阈值的图片；8 MB 是旧插件阈值，不是已确认的平台硬限制。
  API 请求仅允许 `www.pixiv.net`，图片及所有跳转仅允许 `i.pximg.net`、`i.pixiv.re`。
  单张下载另设 **64 MiB 安全上限**，超过时保留进度并进入重试；这是下载保护，不会自动
  压缩图片，也不是 QQ 平台限制。原始图、常规图和压缩派生图分开缓存，关闭压缩后不会
  复用旧的压缩图充当原图。
  每群保存成功接收的作品进度；部分群失败时，只补该群尚未收到的作品，即使重试期间
  又有新作品也不会重发成功群已收到的旧作。`retry_interval_minutes` 默认 2 分钟，
  连续失败按基础间隔 × 失败次数递增，即 2、4、6 分钟，以此类推；成功检查后归零。
  失败重试从失败结束计时，并由每分钟调度触发，因此实际可能略晚，不会提前重试。
  检查点与退避状态保存在 `pixiv_subscriptions.json`，同一 AstrBot 进程热重载时保留；
  **进程重启后清空检查点**，首次成功检查重新记录当前最新作品，不补推停机期间积压。
  订阅配置不会清空。无需登录或 cookie，没有翻译、R18 筛选或其他额外指令。
- **帮助图**：从已启用模块的 `help_entries()` 按条目收集。以 `#` 或 `/` 开头的指令集中放在「指令功能」，其余无指令条目放在「自动功能」；同一模块可以同时出现在两个区域。图片使用暖白底色与深浅分区，文字模式采用相同分组。
  每次生成新图，保留最新 20 张及最近一小时的图；后续请求才清理过期超额图片，避免
  并发发送时提前删除。只清理本模块的 `help_<数字>.png`，不处理符号链接或其他文件。
- **网络请求**：主动群聊工具、卡片封面、日报和 Pixiv 共用安全下载层，仅允许公共
  HTTP(S) 及 80/443 端口。每次跳转重新校验域名和全部 DNS 结果，并连接已批准的 IP，
  保留原始 Host、TLS SNI 与证书验证，防止 DNS 检查后被重新解析到内网。请求有整体超时
  和解压后流量上限；主动群聊工具也使用全局代理。日报仍支持环境代理及 `NO_PROXY`。
- **调度**：所有定时任务交给 AstrBot 的 `context.cron_manager`（APScheduler，
  带时区与失败日志），插件卸载时 `Scheduler.shutdown()` 逐个删除 job。

## 复用了哪些 AstrBot 内置能力

- 路径：`get_astrbot_plugin_data_path()` + `io.ensure_dir()`
- 图片下载：使用 `aiohttp` 及插件共用的安全校验层，统一限制重定向、DNS 和响应大小
- 临时文件：`io.save_temp_img()`
- 日报/帮助图出图：共用本机 Chromium（playwright），各自指定视口与像素密度，
  不依赖远端 t2i 服务
- 消息发送 / LLM 调用 / 配置 / 日志：`event.chain_result()`、
  `context.llm_generate()`、`AstrBotConfig`、`astrbot.api.logger`
- 定时任务：`context.cron_manager`（APScheduler），`Scheduler` 只做
  「HH:MM / 每 N 分钟 / Quartz 6 段 → crontab」的翻译与 job 回收

## 与上游的差异

- 「骰子」「命令预设」目前没有保留（开发过程中已移除），需要时可以从 git 历史里取回。
- 「帮助图」已按上游行为移植，改为复用 AstrBot 的 HTML 渲染，不依赖 Yunzai 的渲染器与字体资源。
- Pixiv 已由早期榜单轮询改为旧版画师订阅；定时任务复用 AstrBot 调度器，修复了旧版失败后
  可能遗留锁、部分群发送失败后重复推送等问题。AI 生成（豆包视频/图片、SiliconFlow 语音）尚未移植。
- 主动群聊已移除早期版本自造的 `reply_mode=random`、`interval_sec`、`min_messages`、
  `trigger_mode/keywords`、`whitelist` 等上游没有的项；`useEmotion` 每日心情也去掉了
  （实测用处不大，且每天要额外调一次模型）。

## 回归检查

在 AstrBot 已安装依赖的 Python 环境中运行。网络、模型和发消息均使用替身，不读取实际
订阅配置或发送群消息：

```bash
cd /home/ubuntu/projects/AstrBot/data/plugins/astrbot_plugin_juhkff
PYTHONPATH=/home/ubuntu/projects/AstrBot /home/ubuntu/projects/AstrBot/.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=/home/ubuntu/projects/AstrBot /home/ubuntu/projects/AstrBot/.venv/bin/python tests/check_command_routing.py
```

第二条使用真实 AstrBot 唤醒流程和消息组件，单独运行以隔离插件入口的热重载模块注册。
覆盖网络安全、卡片、表情、渲染器、缓存、Pixiv 投递状态，以及命令与历史记录分发。

## 许可

GPL-3.0，与上游 juhkff-plugin 一致。
