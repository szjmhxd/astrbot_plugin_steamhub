# astrbot_plugin_steamhub

<img src="logo.png" width="96" alt="logo" />

把自己的 Steam 好友名单一次性导进机器人，再按**序号**把人绑上去。之后查询、上下线推送、开玩推送全部走这份绑定关系——没人需要手敲 17 位 SteamID。

> **平台支持**：目前只在 **KOOK** 上实测通过。QQ / 微信 / 飞书等其它平台未经验证——绑定依赖各平台提供的用户 ID，换平台后先发一次 `/steam 绑定` 确认回执里的账号是自己，再往下用。

## 它到底解决了什么问题

现有两个 Steam 插件各缺一半：

| | `steamwatch` | `steamgame` | 本插件 |
|---|---|---|---|
| 轮询推送 | ✅ | ❌ | ✅ |
| 游戏库/成就/对比图 | ❌ | ✅ | 基础文本 |
| 好友名单导入 | ❌ | ❌ | ✅ |
| 绑定不需要 17 位 ID | ❌ | ❌ | ✅（用序号） |

卡住人的从来不是查询，是**第一步**：怎么把「群里的某个人」和「Steam 上那个账号」对上。`steamwatch` 的做法是让你自己去找 SteamID64 再贴进来；本插件把这个动作变成 `/steam 绑定 #12 @小王`。

## 安装

1. 整个目录放到 `AstrBot/data/plugins/astrbot_plugin_steamhub/`
2. WebUI 里启用插件（依赖 `aiohttp`，AstrBot 本体自带）
3. 配置页填三项：`steam_api_key`、`owner_steamid`、`admin_ids`

`steam_api_key` 申请：<https://steamcommunity.com/dev/apikey>
`owner_steamid` 支持 17 位 ID、`https://steamcommunity.com/id/xxx`、纯自定义名。

## 四步走

```
# 1. 管理员：导入好友（自动分配序号）
/steam 导入

# 2. 看名单分页
/steam 好友
/steam 好友 2          # 第 2 页
/steam 好友 张          # 按昵称搜

# 3. 绑定（这一步是手动的，也是本插件的核心）
/steam 绑定 #12 @小王
/steam 绑定 #12 123456789     # 直接给平台用户 ID 也行
/steam 绑定 我                # 绑自己（取配置里的 owner_steamid）
/steam 绑定 #12               # 不给对象 = 绑自己到该序号

# 4. 群里开启推送
/steam 订阅
```

之后 `/steam 查`、`/steam 查 @小王` 就都能用了。

**自己怎么绑**：好友名单里没有你自己（Steam 的好友列表不含本人），所以用 `/steam 绑定 我`，它会取配置里的 `owner_steamid`；也可以直接 `/steam 绑定 76561198...`。

**不是好友也能绑**：`/steam 绑定 76561198...` 接受任意 SteamID64 或主页链接，不要求在导入的名单里。不在名单时会现场拉一次摘要补昵称，Steam 接口暂时不通也不影响绑定本身。

「我」有一个限制：它取的是配置里的 `owner_steamid`，只有**管理员**能用——否则任何人发一句「绑定 我」就把自己绑到你的账号上。普通用户绑自己请直接给 SteamID64。

**绑自己别在名单里找。** `/steam 导入` 拉的是你的 Steam 好友，**不含你本人**，所以「先导入名单，再按序号绑自己」这条路根本走不通——名单里没有你的号。绑自己只有两种写法：`/steam 绑定 我`（管理员，取 `owner_steamid`）或 `/steam 绑定 <你的 SteamID64 / 主页链接>`。

## 指令

| 指令 | 谁可用 | 说明 |
|---|---|---|
| `/steam` | 所有人 | 帮助 |
| `/steam 导入` | 管理员 | 拉全部好友，分配序号 |
| `/steam 好友 [页码\|关键词]` | 所有人 | 名单 + 序号 + 已绑定数 |
| `/steam 绑定 <引用> [@用户\|用户ID]` | 自己 / 管理员绑他人 | 引用支持 `#序号`、`我`、SteamID64、主页链接、自定义名 |
| `/steam 绑定列表 [页码]` | 管理员 | 谁绑了谁（Steam 昵称 + 平台昵称 + SteamID64）；群聊按本群显示设置，私聊给全量 |
| `/steam 解绑 [@用户]` | 自己 / 管理员 | |
| `/steam 我` | 所有人 | 看自己的绑定 |
| `/steam 查 [@用户\|#序号\|ID]` | 所有人 | 档案 + 游戏库 + 近两周 + 封禁 |
| `/steam 订阅` / `/steam 取订` | 所有人 | 当前会话收不收推送 |
| `/steam 订阅 全部` / `/steam 取订 全部` | 管理员 | 把所有记录过的群一次开关 |
| `/steam 订阅 列表 [页码]` | 所有人 | 哪些群已订阅 |
| `/steam 设置 [昵称\|ID\|重置]` | 查看所有人 / 修改管理员 | 本群名单的显示方式，存进数据文件按群区分 |
| `/steam 状态` | 管理员 | 运行情况 |

## 绑定列表里显示谁

一行三个人，缺一个都认不出是谁：

```
已绑定 1/1（共 5 个账号 · 订阅会话 0 个）
  1. QAQ · 得芙（KOOK 2431745721） · 76561198965256703
  2. 格温可爱捏 · KOOK 3828493731 · 76561198325137391
```

- **Steam 昵称**（`QAQ`）：Steam 上的 `personaname`，随改名更新。
- **平台昵称**（`得芙`）：这个人在这款聊天软件里叫什么，也就是 KOOK 用户名 / QQ 群名片。
  和 Steam 昵称相同时只显示一次。
- **SteamID64**：群聊默认隐藏，私聊才给全量。

平台昵称的来源有三条，任何一条命中就够：绑定时记下发指令的人；本人发任意 `/steam` 指令时自动刷新；
升级前的老绑定第一次看列表时自动回填一次（KOOK 拉一次频道成员表，QQ 逐个查群成员信息，6 秒超时，失败就跳过）。

拿不到也不会影响别的功能——那一行只显示用户 ID，列表底部会提示「本人发一次 `/steam 我`」来补。
群聊里平台昵称不打码，它本来就是群里公开可见的名字；打码只作用于 Steam 昵称。

## 批量订阅怎么工作

AstrBot 只有在收到消息时才知道某个会话存在，所以插件采用「见过就记一笔」：任何人在某个群发过一次 `/steam` 指令，这个群就进 `chats` 记录。

之后管理员在任意会话发：

```
/steam 订阅 全部
```

所有记录过的**群聊**一次性订阅完成，私聊不在此列（推送只发群）。以后新建的群只要有人发过一次 `/steam`，再来一次 `订阅 全部` 就会把它带上——已订阅的不会重复计数。

`/steam 订阅 列表` 看清单，`✓` 是已订阅，`·` 是只记录过还没订阅。

## 序号为什么固定

第一次入库时按昵称排序分配 `#1..#N`，之后**永不重排**，计数器只往前走。

原因很直接：你会用 `#12` 这个写法去绑定。如果每次 `/steam 导入` 都按昵称重新排一遍，改个名、加个好友，`#12` 就指向别人了，历史绑定全部错位。

新增好友永远是往后追加的号，旧号不受影响。

## 名单隐私

好友名单只对你自己有意义，不该在群里全员可见。**昵称和 SteamID 是两个独立开关**：

- 插件配置里的是**默认值**，所有群共用
- **每个群可以用命令单独改**，改完只影响这个群，存进 `steamhub.json` 的 `groups` 段
- **私聊固定显示全名**，不提供开关

默认行为：

| 场景 | 昵称 | SteamID |
|---|---|---|
| 群聊 | 打码（`Zeta` → `Ze*a`） | 不显示 |
| 私聊 | 全名 | 完整 17 位 |

### 在群里改

```
/steam 设置                  看本群当前设置，以及每项是「本群设置」还是「插件默认」
/steam 设置 昵称 masked      昵称：masked / full / hidden
/steam 设置 ID hidden        SteamID：hidden / masked / full
/steam 设置 重置             本群恢复跟随插件默认
```

只有管理员能改。改完 `/steam 好友` 立刻生效，不用重载插件。私聊里发 `/steam 设置` 会提示设置按群生效。

### 插件配置里的默认值

| 配置项 | 取值 | 作用域 |
|---|---|---|
| `roster_group_privacy` | `masked`（默认）/ `full` / `hidden` | 群聊昵称的初始值 |
| `roster_group_show_id` | `hidden`（默认）/ `masked` / `full` | 群聊 SteamID 的初始值 |
| `roster_private_show_id` | `full`（默认）/ `masked` | 私聊 SteamID |

几个说明：

- 昵称打码留**前 2 后 1**，中间填 `*`：`Hanpaopao` → `Ha******o`，`谷雨茶丶` → `谷雨*丶`。
  名字太短时逐级退化——三字 `谷雨茶` → `谷*茶`（前 2 后 1 会全覆盖，改成首尾各留一个），
  两字 `谷雨` → `谷*`，单字原样。
- `masked` 的 SteamID 形如 `76561********6703`——留前 5 位和后 4 位，能认出是谁，不能直接拿去查。
- 群聊昵称选 `hidden` 时整个名单都不显示，SteamID 的设置自然不起作用。
- 改插件配置只影响**没自己设过的群**。已经用命令设过的群继续用它自己的值，要让新默认值生效，在该群执行 `/steam 设置 重置`。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| `steam_api_key` | 空 | 必填 |
| `owner_steamid` | 空 | 必填，导入好友的起点 |
| `admin_ids` | `[]` | 留空 = 不限制，任何人可导入和绑定他人。**群聊务必填** |
| `monitor_scope` | `bound` | `bound` 只监控已绑定；`roster` 监控全部好友；`off` 不轮询 |
| `poll_interval` | 180 | 秒，最小 60 |
| `notify_on_stop` | `true` | 结束游戏也推 |
| `notify_online_offline` | `false` | 上下线也推（人多时关掉） |
| `query_cache_ttl` | 300 | 同一账号重复查询走缓存 |
| `roster_page_size` | 20 | 名单每页条数 |
| `roster_group_privacy` | `masked` | 群聊昵称的默认值：`masked` / `full` / `hidden` |
| `roster_group_show_id` | `hidden` | 群聊 SteamID 的默认值：`hidden` / `masked` / `full` |
| `roster_private_show_id` | `full` | 私聊 SteamID：`full` / `masked` |
| `proxy` | 空 | 大陆访问 Steam 接口基本必备 |
| `show_ban` | `true` | 查询时打 VAC 接口 |

## 用到的 Steam 接口

```
ISteamUser/GetFriendList/v1             好友名单
ISteamUser/GetPlayerSummaries/v2        昵称/头像/在线/当前游戏（100 个一批）
IPlayerService/GetOwnedGames/v1         游戏库与总时长
IPlayerService/GetRecentlyPlayedGames/v1 近两周
ISteamUser/GetPlayerBans/v1             VAC / 游戏封禁
ISteamUser/ResolveVanityURL/v1          自定义名 → SteamID64
```

轮询只在 `GetPlayerSummaries` 上花配额，一次 100 个账号，按 `monitor_scope=bound` 时绑定人数通常只有十几个，一天几千次调用，离 10 万上限很远。

## 已知限制

- **好友列表必须公开**。你自己的隐私设置里「好友列表」要设成公开，否则 `/steam 导入` 一个都拿不到。
- **资料私密的好友不会出现**。Valve 不会把私密资料的好友列进 `GetFriendList`，这不是 bug。让对方把个人资料设为公开再导入一次。
- **游戏库私密就查不到时长**，只能看到公开档案部分。
- 绑定跟人走不跟群走（`平台:用户ID`），换群不用重新绑。
- 推送依赖 AstrBot 的主动发送能力，个别适配器可能不支持。

## 数据存在哪

`StarTools.get_data_dir("astrbot_plugin_steamhub")`，也就是 AstrBot 的 `data/` 目录下，**不在插件目录里**。插件更新或重装不会覆盖你的绑定关系。

## 灵感与致谢

思路来自两个现有插件，本插件没有复用它们的代码：

- [Chinachani/astrbot_plugin_steamwatch](https://github.com/Chinachani/astrbot_plugin_steamwatch) —— 轮询 `GetPlayerSummaries` 做上下线/开玩推送的实现思路，以及代理与重试的处理方式。
- [bvzrays/astrbot_plugin_steamgame](https://github.com/bvzrays/astrbot_plugin_steamgame) —— 「游戏库 + 近两周 + 封禁」这套展示维度。

两者都要求用户自己准备 SteamID64，本插件把这一步换成了导入好友名单 + 序号绑定。

插件 Logo 使用 Steam 官方标志，版权归 Valve Corporation 所有，仅作识别用途。

## 想扩展

数据层和文本层是分开的：`store.py` 管状态，`steam.py` 管接口，`render.py` 只负责把 dict 拼成字符串。

想把文本换成图片战报，只需要重写 `render.py` 里的几个函数（换成 HTML 模板 → 截图），`main.py` 一行不用动。想加成就/愿望单/打折监控，在 `steam.py` 加方法、`main.py` 加子指令即可。
