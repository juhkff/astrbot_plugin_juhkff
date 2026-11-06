# 综合功能插件

将 [juhkff-plugin](https://github.com/juhkff/juhkff-plugin) 的群聊功能移植到 AstrBot，包含主动群聊、分享卡片解析、偷表情、每日日报、Pixiv 画师订阅和帮助图。各功能可独立开关。

## 安装

1. 在 AstrBot WebUI 的插件管理中，通过[本仓库地址](https://github.com/juhkff/astrbot_plugin_juhkff)安装插件。
2. 在 **AstrBot 使用的 Python 环境**中安装依赖和 Chromium。以下命令从 AstrBot 根目录执行：

   ```bash
   python -m pip install -r data/plugins/astrbot_plugin_juhkff/requirements.txt
   python -m playwright install chromium
   ```

3. 重启或重载插件，在「插件管理 → 综合功能插件 → 设置」中配置模型、接收群及各功能开关。

日报和帮助图使用本机 Chromium 渲染，Linux 需要中文字体，安装方法见下方。群聊历史还需要可用的 Redis 服务。

## 常用指令

以下为默认写法，命令前缀以 AstrBot 配置为准。日报和 Pixiv 指令在群聊中使用。

| 指令 | 用途 |
| --- | --- |
| `/juhkff` | 查看插件状态 |
| `#帮助` | 查看本插件已启用功能的帮助，可在设置中修改触发词 |
| `#日报` | 获取今日日报 |
| `#pixiv订阅 UID` | 当前群订阅指定画师，例如 `#pixiv订阅 123456` |
| `#pixiv取消订阅 UID` | 当前群取消该画师订阅，不影响其他群 |

主动群聊、卡片解析和偷表情自动处理群消息，无需指令。卡片解析支持 B 站、小黑盒等分享卡片，不处理普通文字链接。

## 配置要点

各功能默认启用；完整选项及说明以 WebUI 设置页为准。

**主动群聊**

- 对话模型留空时，使用 AstrBot 为当前群选择的模型。默认随机接话概率为 20%，被 @ 或引用时必回，可单独设置每个群。
- 「延迟发送时间」默认 1–3 秒：回复生成后随机等待再发送，支持小数；两项均为 0 时立即发送，相同则固定等待。
- 历史只存入 Redis，默认携带最近 20 条，最多保存 64 条。连接失败时仍可接话，但不记录或使用历史。
- 默认进程重启清空历史，热重载保留；关闭「携带聊天上下文」只停止携带，仍会记录新消息。

**偷表情**

- 同群相同图片在默认 72 小时内第二次出现时收藏，默认单图不超过 2 MB，每群最多 30 张。
- 发图优先使用本群收藏，没有可用收藏时使用配置的本地表情包文件夹。发图概率设为 0 仍会收藏。
- 表情判断需同时开启开关并选择支持图片输入的模型；未选模型或判断失败时，按重复规则收藏。

**日报**

- 包含时政、国内社会、游戏、数码四类新闻，每类最多 12 条；提供五款样式，可固定或每次随机选择。
- 自动推送需选择接收群并设置时间；接收群为空时不自动发送。「提前生成」可单独开启，减少发送时等待。
- 图片按日期和样式缓存；样式仅在设置页选择，获取指令统一为 `#日报`。

**Pixiv 订阅**

- UID 是画师主页的数字用户 ID。群内订阅指令会同步到设置页的「画师订阅列表」，也可直接在设置页编辑接收群。
- 默认每 60 分钟检查一次，补齐两次检查间的全部新插画及全部分页；同一画师本轮新作合并为一条转发消息，接收平台需支持合并转发（如 QQ/NapCat）。
- 首次订阅及 AstrBot 进程重启后，只以最新作品建立基线，不补推旧作或停机积压；订阅配置保留。热重载保留进度，部分群失败只补发未收到的作品。
- 默认原图优先、不压缩；可设置正数压缩阈值。单张下载另有 **64 MiB 安全上限**，并非 QQ 大小限制。
- 获取作品、下载或发送失败时，默认依次等待 2、4、6…分钟；基础间隔可调，成功后恢复正常检查。任务按分钟调度，实际可能略晚。
- 无需登录，只订阅插画，不包含漫画、小说，也不提供 R18 筛选。

**全局设置**：网络代理填写 HTTP 地址，例如 `http://127.0.0.1:7890`；时区默认 `Asia/Shanghai`。详细日志默认开启，会包含主动群聊原文和发给模型的内容，可按需关闭。

## 常见问题

### 日报或帮助图的中文显示为方框

在 **AstrBot 实际运行的系统或容器**中安装中文字体，按系统选择命令。

Ubuntu / Debian：

```bash
sudo apt-get update
sudo apt-get install -y fontconfig fonts-noto-cjk
```

Rocky / AlmaLinux / RHEL 8、9 等 dnf 系统：

```bash
sudo dnf install -y fontconfig google-noto-sans-cjk-ttc-fonts google-noto-serif-cjk-ttc-fonts
```

刷新并确认字体：

```bash
sudo fc-cache -f
fc-list :lang=zh family
```

应能看到 Noto Sans CJK 和 Noto Serif CJK。其他 dnf 发行版的包名可能不同，可用 `dnf list --available '*noto*cjk*'` 查询。

安装后重启 AstrBot。日报还需备份移走 `data/plugin_data/astrbot_plugin_juhkff/daily_report/YYYY-MM-DD/` 下当天的五张样式 PNG，再发送 `#日报`；保留新闻快照和配图即可。帮助图每次重新生成，无需清理。

### 设置代理后仍无法访问 Pixiv

- 代理必须能被 AstrBot 所在环境访问。Docker 中的 `127.0.0.1` 指向容器自身；仅提供 SOCKS5 的地址不能直接使用。
- 插件代理用于网页和图片请求，模型 API、QQ 连接按各自配置处理。
- 本机 DNS 仍需返回真实公网地址；代理的 Fake-IP 模式可能被安全校验拒绝。网络请求仅允许公网 HTTP(S) 的 80/443 端口。

### 数据保存在哪里

- 订阅等设置：`data/config/astrbot_plugin_juhkff_config.json`。
- 群聊历史：配置的 Redis。
- 表情、日报和 Pixiv 缓存：`data/plugin_data/astrbot_plugin_juhkff/`。备份迁移时可一并保留该目录和配置文件。

## 开发与测试

在已安装依赖的 AstrBot Python 环境中，从 **AstrBot 根目录**执行：

```bash
PYTHONPATH=.:data/plugins/astrbot_plugin_juhkff \
  python -m unittest discover -s data/plugins/astrbot_plugin_juhkff/tests -p 'test_*.py'
PYTHONPATH=. python data/plugins/astrbot_plugin_juhkff/tests/check_command_routing.py
```

第二条独立运行，以隔离插件入口的热重载注册。测试覆盖网络安全、卡片、表情、渲染、缓存、Pixiv 状态及命令分发，不调用真实模型或发送群消息。

## 许可

GPL-3.0，与上游一致。
