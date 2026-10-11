# BiliSubscribe

<p align="center">
  <a href="https://github.com/assin001/BiliSubscribe"><img src="./ICON.png" width="160" alt="BiliSubscribe ICON"></a>
</p>

<h1 align="center">BiliSubscribe 0.1.0</h1>

<h4 align="center">✨基于<a href="https://github.com/Genshin-bots/gsuid_core" target="_blank">GsCore（早柚核心）</a>的 B站 UP 主订阅插件：订阅的视频投稿与动态一有更新，就画成卡片推送到群里✨</h4>

<div align="center">
  <a href="./README-Detailed.md" target="_blank">完整文档</a> &nbsp; · &nbsp;
  <a href="https://github.com/Genshin-bots/gsuid_core" target="_blank">gsuid_core</a> &nbsp; · &nbsp;
  <a href="https://github.com/assin001/BiliSubscribe/issues" target="_blank">问题反馈</a>
</div>

<div align="center">
  <a href="https://count.getloli.com/"><img src="https://count.getloli.com/get/@BiliSubscribe?theme=booru-lewd" alt="BiliSubscribe 访问计数"></a>
</div>

<br/>

## 丨安装提醒

> **注意：该插件为 [早柚核心 (gsuid_core)](https://github.com/Genshin-bots/gsuid_core) 的扩展，具体安装方式可参考核心的安装文档**
>
> **运行环境要求 Python `3.9+`**
>
> **如果是最新版本的 `gsuid_core`，可以直接对 bot 发送 `core安装插件BiliSubscribe`，然后重启 Core 以应用安装**

<br/>

## 丨我该如何安装该插件？

- 前提：你已经部署好 [gsuid_core](https://github.com/Genshin-bots/gsuid_core)。
- 将本仓库克隆到 GsCore 插件目录并重启：

```bash
cd gsuid_core/gsuid_core/plugins
git clone https://github.com/assin001/BiliSubscribe BiliSubscribe
```

- 重启 GsCore（或在 Web 控制台重载插件）后自动加载并初始化数据表，发送 `bili帮助` 确认是否成功。
- 依赖：`aiohttp`（核心自带）+ `qrcode`（核心一般也自带；真缺了只会让扫码登录退回「发登录链接」，功能不中断）。
- ⚠️ **插件目录名必须叫 `BiliSubscribe`** —— 核心按 `plugins/<目录名>` 关联插件配置，改名后控制台那个配置页会是空的。

<br/>

## 丨指令列表

| 指令 | 说明 |
|------|------|
| `bili订阅 <UID / 主页链接 / UP名>` | 订阅该 UP 的视频与动态（UP 名完全一致时直接订阅，否则列出候选） |
| `bili订阅 <UID> at` | 订阅并 @全体成员 |
| `bili取消订阅 <UID\|UP名>` | 取消订阅；`bili取消订阅 all` 清空本群订阅（需群主/管理员） |
| `bili设置 <UID\|UP名> [设置项]` | 改这条订阅推什么（开/关动态·视频·直播、按类 @全体）；不带设置项 = 看当前配置 |
| `bili列表` | 查看本群订阅的 UP，顺便显示每条订阅的开关 |
| `bili动态 <UID>` | 查看该 UP 最新动态（调试 / 验证用） |
| `bili帮助` | 群聊帮助：发帮助图（图不在则退回文字） |

管理员指令（超级用户 `user_pm <= 1`），完整列表用 `bili管理帮助` 查看：

| 指令 | 说明 |
|------|------|
| `bili登录` | 扫码登录 B站，自动获取并写入 Cookie（推荐） |
| `bili设置ck <Cookie>` | 手动设置 Cookie；`bili设置ck` 查看，`bili设置ck clear` 清空 |
| `bili配置` / `bili修改配置 <项> <值>` | 查看 / 修改配置 |
| `bili检查更新` | 立刻跑一轮检查 |
| `bili推这条 <链接\|ID\|cv号\|BV号\|短链> [订阅群]` | 手动补推一条动态或视频（默认推到本会话） |
| `bili过滤规则`（别名 `bili游戏`） | 查看内置白名单：哪些游戏、哪些栏目、没命中的怎么处理 |
| `bili过滤测试 <链接\|ID>` | 给一条动态看它会不会被推（只判断、不发送、不动游标） |
| `bili白名单 <关键词\|取消 关键词>` | 额外白名单关键词：命中就直接推 |
| `bili待定` / `可以推送 <编号\|全部>` / `bili跳过 <编号\|全部>` | 看待定队列 / 把待定项推送出去 / 过滤掉 |

> 命令前缀遵循 GsCore 的全局前缀配置（比如 `#bili订阅 123` 或直接 `bili订阅 123`）。

<br/>

## 丨配置说明

可在 **GsCore 网页控制台**「插件 → BiliSubscribe → 配置」页面可视化配置，改完立即生效；也可以在群里用 `bili修改配置` 改。下面是常用项，完整 24 项见[完整文档](./README-Detailed.md)：

| 配置项 | 类型 | 默认值 | 说明 |
|:---|:---|:---|:---|
| `bilibili_cookie` | `str` | 空 | 登录 Cookie。动态接口报 `-636`、投稿接口报 `-352` 风控时必填 |
| `check_interval` | `int` | `300` | 轮询间隔（秒），最小 60 |
| `notify_video` / `notify_dynamic` | `bool` | `true` | 是否推送投稿视频 / 图文专栏转发等动态 |
| `notify_live` | `bool` | `false` | 开播卡片 + 真正的开播提醒（打开后每 UP 每轮多一次请求） |
| `push_card` / `push_cover` | `bool` | `true` | 是否把更新画成卡片图 / 卡片里带封面 |
| `ad_filter` | `bool` | `true` | 过滤带货、广告动态（只对非官方号生效） |
| `check_video_list` | `bool` | `true` | 额外用投稿列表接口查视频，补动态接口的漏 |
| `sleep_mode` / `sleep_range` | `bool` / `str` | `true` / `01:00-07:30` | 定时休眠：休眠时段内不轮询（手动 `bili检查更新` 不受影响） |
| `cache_ttl_days` | `int` | `15` | 图片缓存天数：头像这类重复图同一个 URL 只下一次 |
| `max_uid_per_round` / `max_push_per_round` | `int` | `20` / `5` | 单轮检查的 UP 上限 / 单轮每 UP 最多补推几条 |
| `push_full_text` | `bool` | `false` | 长文是否在卡片之外补发全文（默认只发预览） |
| `queue_unmatched` | `bool` | `false` | 没命中白名单的动态是否进待定队列（默认直接过滤） |

> 订阅数据的开关是**按群、按 UP 单独存**的：同一个 UP，A 群只收视频、B 群只收动态都是合法需求，用 `bili设置` 调。

<br/>

## 丨使用限制

> [!IMPORTANT]
> 本项目以 **GNU GPL-3.0-or-later** 授权，全文见 [LICENSE](./LICENSE)：可以自由使用、修改、分发（含商用），
> 但**分发修改版或衍生作品时必须同样以 GPL 开源并提供完整源码**，并保留版权与许可声明。本软件不提供任何担保。

> [!WARNING]
> - Cookie 是账号凭证，插件会把它存在核心的数据目录里，**建议用小号扫码**。
> - B站接口有风控：轮询太频繁会触发限制（吃到 `HTTP 412` 后插件会静默 5 分钟）。建议 `check_interval` 不低于 180 秒。
> - 请遵守 B站用户协议，使用风险自负；本项目仅供学习与交流。

<br/>

## Star History

<a href="https://www.star-history.com/?repos=assin001%2FBiliSubscribe&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=assin001/BiliSubscribe&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=assin001/BiliSubscribe&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=assin001/BiliSubscribe&type=date&legend=top-left" />
 </picture>
</a>

<br/>

## 丨致谢

- [GsCore（早柚核心）](https://github.com/Genshin-bots/gsuid_core) —— 插件运行的核心
- [astrbot_plugin_bililive](https://github.com/Lianzy-Baimiao/astrbot_plugin_bililive) —— 本插件的参考实现（AstrBot 版本）
- 随插件分发的字体（`fonts/`）：优设标题黑、思源黑体、霞鹜文楷、Orbitron，各自遵循原发行方的许可条款
