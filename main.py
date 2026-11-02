"""Steam 好友中心 —— 导入好友名单，按序号绑定，之后查询与推送全走绑定关系。

为什么要这么设计
----------------
两个现成插件各缺一半：``steamwatch`` 有轮询推送，但要你手动喂 SteamID；
``steamgame`` 能出漂亮战报，但没有任何持续监控。真正卡住人的是**第一步**：
怎么把「群里的某个人」和「Steam 上的某个账号」对上。

所以这里把顺序反过来：

1. 用你自己的 Key 一次性拉全部好友（``/steam 导入``），每人分配一个**固定序号**；
2. 你照着名单人工绑定（``/steam 绑定 #12 @小王``），全程不用碰 17 位 ID；
3. 之后查询、上线推送、开玩推送，全部以这份绑定表为准。

序号是单调递增的，重导入不会重排，历史绑定不会错位。

开发约定
--------
所有 ``@filter.*`` 必须写在插件类里（AstrBot 用 ``handler_module_path`` 精确匹配
归属 handler，写在其它模块会被静默丢弃）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import render
from .steam import SteamClient, SteamError
from .store import SteamStore

try:  # 主动推送用的消息链，不同 AstrBot 版本位置有差异
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # pragma: no cover
    MessageChain = None  # type: ignore[assignment]

try:
    from astrbot.api.message_components import Plain
except Exception:  # pragma: no cover
    Plain = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_steamhub"

_AT_PREFIX = ("@", "＠")
_CMD_PREFIX = "/~!#！"
_INDEX_RE = re.compile(r"^[#＃]?\d{1,5}$")
# 「我」的几个写法：指代执行者本人
_ME_ALIASES = ("我", "me", "self", "自己")

# --------------------------------------------------------------------- #
# 名单显示的可选值
# 群聊：昵称三档、SteamID 三档；私聊昵称固定全名，只有 SteamID 两档
# --------------------------------------------------------------------- #
_NAME_MODES = ("masked", "full", "hidden")
_GROUP_ID_MODES = ("hidden", "masked", "full")
_PRIVATE_ID_MODES = ("masked", "full")


def _pick(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    """把配置/存储里的杂值归一到允许集合，不合法就用 fallback。"""
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


class SteamHubPlugin(Star):
    """Steam 好友中心。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context, config)
        # AstrBot v4.29 的 Star.__init__ 不再把 config 存成 self.config，插件必须自己留一份。
        # 漏了这步时 self.config 直接抛 AttributeError，被 _raw 的兜底吃掉，所有配置项退化成
        # 默认值——表现就是「面板里明明填了 owner_steamid，插件一直说没配」。
        # 以 _conf 为准，同时补上 self.config，兼容仍会自己赋值的老版本框架。
        self._conf: dict = config if isinstance(config, dict) else {}
        try:
            self.config = self._conf  # type: ignore[assignment]
        except Exception:  # noqa: BLE001 框架把 config 定义成只读属性时不影响主流程
            pass
        # 配置文件 mtime，用来发现「WebUI 保存了但插件没重载」
        self._cfg_mtime: int = -1
        self._cfg_file: str | None = None

        self._data_dir = self._resolve_data_dir()
        self._store = SteamStore(self._data_dir / "steamhub.json")
        self._client = SteamClient(
            self._cfg_str("steam_api_key"),
            proxy=self._cfg_str("proxy"),
            timeout=self._cfg_int("http_timeout", 12),
            retries=self._cfg_int("request_retries", 3),
        )
        # steamid -> (到期时间, 渲染好的文本)
        self._cache: dict[str, tuple[float, str]] = {}
        self._poll_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        await self._store.load()
        stats = await self._store.stats()
        # 逐项自证：这一行能区分「面板里没保存」和「保存了没读到」
        logger.info(f"[Steam好友中心] 配置文件：{self._config_path or '未找到'}")
        logger.info(f"[Steam好友中心] 配置读到 {len(self._conf)} 项：{self._describe_conf()}")
        logger.info(
            f"[Steam好友中心] 已启动：好友 {stats['roster']} 人，"
            f"绑定 {stats['bindings']} 个，订阅会话 {stats['sessions']} 个"
        )
        logger.info("[Steam好友中心] 平台支持：KOOK 已实测；QQ / 微信 / 飞书等未验证")
        if not self._cfg_str("steam_api_key"):
            logger.warning("[Steam好友中心] 未配置 steam_api_key，导入与查询都会失败")
        if not self._cfg_str("owner_steamid"):
            logger.warning("[Steam好友中心] 未配置 owner_steamid，/steam 导入 无法使用")
        if self._cfg_bool("enabled", True) and self._cfg_str("monitor_scope", "bound") != "off":
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._poll_task = None
        await self._client.close()
        self._cache.clear()

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #

    @filter.command("steam")
    async def cmd_steam(self, event: AstrMessageEvent):
        """Steam 好友中心"""
        if not self._cfg_bool("enabled", True):
            yield event.plain_result("Steam 好友中心已关闭")
            return

        self._client.set_api_key(self._cfg_str("steam_api_key"))
        # 每条指令顺手记一笔会话，之后 /steam 订阅 全部 才有东西可枚举
        await self._touch_chat(event)
        args = self._strip_command(event.message_str, "steam")
        sub = args[0].lower() if args else ""
        rest = args[1:]

        try:
            if sub in ("", "帮助", "help", "?", "？", "菜单"):
                text = render.HELP_TEXT
            elif sub in ("导入", "同步", "import", "sync"):
                text = await self._cmd_import(event)
            elif sub in ("好友", "列表", "list", "friends", "名单"):
                text = await self._cmd_roster(event, rest)
            elif sub in ("绑定列表", "已绑定", "绑定们", "bindings", "list_bind"):
                text = await self._cmd_bindings(event, rest)
            elif sub in ("绑定", "bind"):
                text = await self._cmd_bind(event, rest)
            elif sub in ("解绑", "unbind"):
                text = await self._cmd_unbind(event, rest)
            elif sub in ("我", "me", "whoami"):
                text = await self._cmd_me(event)
            elif sub in ("查", "查询", "q", "query", "info"):
                text = await self._cmd_query(event, rest)
            elif sub in ("订阅", "sub", "subscribe"):
                text = await self._cmd_subscribe(event, rest)
            elif sub in ("取订", "退订", "unsub", "unsubscribe"):
                text = await self._cmd_unsubscribe(event, rest)
            elif sub in ("设置", "set", "settings", "配置"):
                text = await self._cmd_settings(event, rest)
            elif sub in ("状态", "status"):
                text = await self._cmd_status(event)
            else:
                text = f"不认识的子指令「{sub}」。\n\n" + render.HELP_TEXT
        except SteamError as exc:
            text = f"失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Steam好友中心] 指令执行异常：{exc}")
            text = f"出错了：{type(exc).__name__}: {exc}"

        yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # /steam 导入
    # ------------------------------------------------------------------ #

    async def _cmd_import(self, event: AstrMessageEvent) -> str:
        if not self._require_admin(event):
            return "只有管理员能导入好友名单。"

        owner = self._cfg_str("owner_steamid")
        if not owner:
            return "配置里没填 owner_steamid（你自己的 SteamID64 或主页），无法导入。"

        owner_id = await self._client.resolve_id(owner)
        friends = await self._client.friends(owner_id)
        if not friends:
            return (
                "一个好友都没拿到。最常见的原因是**你的好友列表没公开**：\n"
                "Steam → 隐私设置 → 好友列表 → 设为公开，然后再导入。"
            )

        since = {str(f["steamid"]): int(f.get("friend_since") or 0) for f in friends}
        players = await self._client.summaries(since.keys())
        added, updated = await self._store.upsert_roster(players, since)
        self._cache.clear()

        missing = len(since) - len(players)
        lines = [
            f"导入完成：好友 {len(since)} 人，可读资料 {len(players)} 人",
            f"新增 {added} 人，刷新 {updated} 人",
        ]
        if missing > 0:
            lines.append(
                f"有 {missing} 人读不到资料（资料私密），他们在名单里不会出现，"
                "让他们把「个人资料」设为公开再导入一次。"
            )
        lines.append("")
        lines.append("下一步：/steam 好友  看名单和序号")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # /steam 好友
    # ------------------------------------------------------------------ #

    async def _cmd_roster(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if await self._store.roster_count() == 0:
            return "名单是空的。管理员先执行 /steam 导入。"

        page = 1
        keyword = ""
        if rest:
            token = rest[0].lstrip("#＃")
            if token.isdigit():
                page = max(1, int(token))
            else:
                keyword = " ".join(rest)

        items = (
            await self._store.search_roster(keyword)
            if keyword
            else await self._store.roster_items()
        )
        bound = await self._store.binding_count()
        title = f"好友名单（搜「{keyword}」）" if keyword else "好友名单"

        # 昵称与 SteamID 是两个独立开关：群聊优先本群设置，私聊固定全名
        is_group = self._is_group_chat(event)
        name_mode, id_mode, _n_over, _i_over = await self._display_modes(event)

        if is_group and name_mode == "hidden":
            private_id = self._plugin_modes()[2]
            tail = "含 SteamID" if private_id == "full" else "SteamID 打码"
            return (
                "本群不显示好友名单。\n"
                f"私聊机器人发 /steam 好友 可以看完整名单（{tail}）。\n"
                "管理员可以发 /steam 设置 调整本群的显示方式。"
            )

        return render.roster_text(
            items,
            page=page,
            page_size=self._cfg_int("roster_page_size", 20),
            bound_count=bound,
            title=title,
            name_mode=name_mode,
            id_mode=id_mode,
        )

    # ------------------------------------------------------------------ #
    # /steam 设置
    # ------------------------------------------------------------------ #

    async def _cmd_settings(self, event: AstrMessageEvent, rest: list[str]) -> str:
        _group_name, group_id = self._chat_label(event)
        private_id = self._plugin_modes()[2]

        # 私聊：没有「本群」这回事
        if not self._is_group_chat(event):
            return (
                "显示设置按群生效，私聊没有群可以设。\n"
                "私聊固定显示全名，SteamID 跟随插件配置里的「私聊 SteamID」"
                f"（当前：{render.ID_LABELS.get(private_id, private_id)}）。\n"
                "在群里发 /steam 设置 可以查看或修改那个群的设置。"
            )

        if not group_id:
            return (
                "拿不到本群 ID，没法按群保存设置。\n"
                "这通常是适配器没把 group_id 传过来。可以退回插件配置里的默认值，"
                "或检查适配器版本。"
            )

        key = self._group_key(event)
        chat_label = f"{_group_name}（{group_id}）" if _group_name else group_id

        if not rest:
            name_mode, id_mode, n_over, i_over = await self._display_modes(event)
            return render.settings_text(
                chat_label=chat_label,
                name_mode=name_mode,
                id_mode=id_mode,
                name_overridden=n_over,
                id_overridden=i_over,
                private_id_mode=private_id,
            )

        if not self._require_admin(event):
            return "只有管理员能改本群的显示设置。"

        action = rest[0].lower()
        value = rest[1].lower() if len(rest) > 1 else ""

        if action in ("重置", "reset", "默认", "default", "清除"):
            removed = await self._store.reset_group_setting(key)
            if not removed:
                return "本群本来就是跟随插件默认，没有可重置的设置。"
            return "已恢复跟随插件配置。\n之后发 /steam 设置 可以确认当前生效的值。"

        if action in ("昵称", "name", "名字"):
            if value not in _NAME_MODES:
                return (
                    "昵称模式只有三种：\n"
                    "  masked  打码（Hanpaopao → Ha******o）\n"
                    "  full    全名\n"
                    "  hidden  本群不显示名单\n"
                    "用法：/steam 设置 昵称 masked"
                )
            await self._store.set_group_setting(key, name=value)
            label = render.NAME_LABELS.get(value, value)
            return f"本群昵称显示已设为「{label}」。\n发 /steam 好友 看看效果。"

        if action in ("id", "steamid", "编号"):
            if value not in _GROUP_ID_MODES:
                return (
                    "SteamID 显示只有三种：\n"
                    "  hidden  不显示\n"
                    "  masked  打码（76561********6703）\n"
                    "  full    完整 17 位\n"
                    "用法：/steam 设置 ID hidden"
                )
            await self._store.set_group_setting(key, steamid=value)
            label = render.ID_LABELS.get(value, value)
            return f"本群 SteamID 显示已设为「{label}」。\n发 /steam 好友 看看效果。"

        return (
            "用法：\n"
            "  /steam 设置                看本群当前设置\n"
            "  /steam 设置 昵称 masked|full|hidden\n"
            "  /steam 设置 ID hidden|masked|full\n"
            "  /steam 设置 重置           回到插件默认"
        )

    # ------------------------------------------------------------------ #
    # /steam 绑定列表
    # ------------------------------------------------------------------ #

    async def _cmd_bindings(self, event: AstrMessageEvent, rest: list[str]) -> str:
        if not self._require_admin(event):
            return "只有管理员能看全部绑定记录。\n看自己绑的是谁：/steam 我"

        page = 1
        if rest and rest[0].isdigit():
            page = max(1, int(rest[0]))

        rows = await self._store.bindings()
        name_mode, id_mode, _n, _i = await self._display_modes(event)
        in_group = self._is_group_chat(event)
        if in_group and name_mode == "hidden":
            # 「昵称 hidden」的语义是不展示名单，绑定列表同理
            return "本群设置为不展示名单与绑定记录。\n私聊机器人发 /steam 绑定列表 可以看。"

        stats = await self._store.stats()
        return render.bindings_text(
            rows,
            page=page,
            page_size=self._cfg_int("roster_page_size", 20),
            name_mode="full" if not in_group else name_mode,
            id_mode=id_mode,
            subscribed=stats["sessions"],
        )

    # ------------------------------------------------------------------ #
    # /steam 绑定
    # ------------------------------------------------------------------ #

    async def _cmd_bind(self, event: AstrMessageEvent, rest: list[str]) -> str:
        ats = self._extract_ats(event)

        tokens = [t for t in rest if not t.startswith(_AT_PREFIX)]
        ref_raw = tokens[0] if tokens else ""
        # 第二项可能是 Steam 账号（17 位 / 链接），也可能是平台用户 ID（5-12 位数字）
        steam_hint = ""
        target_uid = ""
        for token in tokens[1:]:
            if not steam_hint and (len(token) >= 17 or "/" in token or "." in token):
                steam_hint = token
            elif not target_uid and token.isdigit() and 5 <= len(token) <= 12:
                target_uid = token

        if ats:
            target_uid = ats[0]

        if not ref_raw:
            return (
                "用法：/steam 绑定 #12               绑自己\n"
                "      /steam 绑定 #12 @小王          把序号 12 绑给小王\n"
                "      /steam 绑定 76561198000000000  用 SteamID64 绑自己（不必是好友）\n"
                "      /steam 绑定 我                 管理员：绑配置里的 owner_steamid"
            )

        # 「我」只用于绑自己。好友名单里没有本人，owner 只有管理员知道是谁
        if ref_raw.lower() in _ME_ALIASES:
            if ats or target_uid:
                return "「我」只用于绑自己。\n给别人绑请用：/steam 绑定 <SteamID64 或 #序号> @对方"
            if steam_hint:
                ref_raw = steam_hint  # 「我 7656…」= 把这个账号绑到我自己身上
            else:
                if not self._require_admin(event):
                    return (
                        "「我」取的是配置里的 owner_steamid，只有管理员能用。\n"
                        "绑自己直接发：/steam 绑定 <你的 17 位 SteamID64>"
                    )
                owner = self._cfg_str("owner_steamid")
                if not owner:
                    return (
                        "配置里没填 owner_steamid，用不了「我」。\n"
                        "直接发 /steam 绑定 <你的 17 位 SteamID64> 即可。"
                    )
                ref_raw = owner

        if target_uid and target_uid != self._sender_id(event):
            if not self._require_admin(event):
                return "只有管理员能给别人绑定。"

        steamid, label = await self._resolve_ref(ref_raw)
        platform = self._platform(event)
        uid = target_uid or self._sender_id(event)
        if not uid:
            return "拿不到你的用户 ID，无法绑定。"

        await self._store.bind(f"{platform}:{uid}", steamid, label)

        if uid == self._sender_id(event):
            who = f"你（{uid}）"
        else:
            name = self._at_name(event, uid)
            who = f"@{name}（{uid}）" if name else f"用户 {uid}"

        group_name, group_id = self._chat_label(event)
        if group_id:
            chat = f"{group_name}（{group_id}）" if group_name else group_id
        else:
            chat = "私聊"

        return "\n".join(
            [
                "已绑定",
                f"  会话：{chat}",
                f"  平台：{platform.upper()}",
                f"  成员：{who}",
                f"  Steam 昵称：{label or '（未设昵称）'}",
                f"  SteamID64：{steamid}",
                "",
                "之后 /steam 查 就查这个账号；在群里发 /steam 订阅 开启上线/开玩推送。",
            ]
        )

    async def _cmd_unbind(self, event: AstrMessageEvent, rest: list[str]) -> str:
        ats = self._extract_ats(event)
        target_uid = ats[0] if ats else ""
        if not target_uid and rest and rest[0].isdigit() and 5 <= len(rest[0]) <= 12:
            target_uid = rest[0]
        if target_uid and target_uid != self._sender_id(event):
            if not self._require_admin(event):
                return "只有管理员能解绑别人。"

        uid = target_uid or self._sender_id(event)
        removed = await self._store.unbind(f"{self._platform(event)}:{uid}")
        if not removed:
            return "没有找到绑定记录。"
        self._cache.pop(str(removed.get("steamid") or ""), None)
        return f"已解绑（原账号 {removed.get('label') or removed.get('steamid')}）。"

    async def _cmd_me(self, event: AstrMessageEvent) -> str:
        record = await self._store.get_binding(self._binding_key(event))
        if not record:
            return self._bind_hint(event)
        steamid = str(record.get("steamid") or "")
        return (
            f"你绑定的是：{record.get('label') or '未命名'}\n"
            f"SteamID64：{steamid}\n"
            f"绑定时间：{render.fmt_rel_time(record.get('updated'))}"
        )

    def _bind_hint(self, event: AstrMessageEvent) -> str:
        """没绑定时该怎么说。

        ``/steam 好友`` 里**找不到自己**（好友列表不含本人），所以不能
        像早期那样让用户去名单里翻自己的序号，那是死路。
        """
        lines = ["你还没绑定 Steam 账号。任选一种："]
        if self._cfg_str("owner_steamid") and self._require_admin(event):
            lines.append("/steam 绑定 我              绑配置里的 owner_steamid（你是管理员）")
        lines.extend(
            [
                "/steam 绑定 76561198...    直接给 17 位 SteamID64",
                "/steam 绑定 https://steamcommunity.com/id/你的名字   主页链接也行",
                "",
                "好友名单来自你的好友列表，不含你自己，所以名单里翻不到你。",
                "不知道自己的 SteamID？看个人资料页地址栏，或用 https://steamid.io 转一下。",
            ]
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # /steam 查
    # ------------------------------------------------------------------ #

    async def _cmd_query(self, event: AstrMessageEvent, rest: list[str]) -> str:
        steamid, note = await self._resolve_query_target(event, rest)
        if not steamid:
            return note

        ttl = self._cfg_int("query_cache_ttl", 300)
        cached = self._cache.get(steamid)
        if ttl > 0 and cached and cached[0] > time.monotonic():
            return cached[1]

        show_ban = self._cfg_bool("show_ban", True)
        tasks: list[Any] = [
            self._client.summaries([steamid]),
            self._client.owned_games(steamid),
            self._client.recent_games(steamid, 5),
        ]
        if show_ban:
            tasks.append(self._client.bans([steamid]))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        def _pick(index: int, default: Any) -> Any:
            if index >= len(results):
                return default
            value = results[index]
            if isinstance(value, Exception):
                logger.debug(f"[Steam好友中心] 查询子请求失败：{value}")
                return default
            return value

        summaries = _pick(0, {}) or {}
        owned = _pick(1, []) or []
        recent = _pick(2, []) or []
        ban_map = _pick(3, {}) or {}

        player = summaries.get(steamid) or {}
        if not player:
            return "查不到这个账号：SteamID 不存在，或对方资料完全私密。"

        text = render.profile_text(
            player=player,
            owned=owned,
            recent=recent,
            ban=ban_map.get(steamid) if show_ban else None,
        )
        if not owned and not recent:
            text += (
                "\n\n（拿不到游戏库，对方可能把「游戏详情」设成了私密，"
                "或者这个账号没有公开游戏记录）"
            )

        if ttl > 0:
            self._cache[steamid] = (time.monotonic() + ttl, text)
        return text

    # ------------------------------------------------------------------ #
    # 订阅 / 状态
    # ------------------------------------------------------------------ #

    async def _cmd_subscribe(self, event: AstrMessageEvent, rest: list[str]) -> str:
        arg = rest[0].lower() if rest else ""

        if arg in ("列表", "清单", "list", "ls"):
            chats = await self._store.chats()
            sessions = await self._store.sessions()
            page = 1
            for token in rest[1:]:
                if token.isdigit():
                    page = max(1, int(token))
                    break
            return render.subscriptions_text(
                chats,
                sessions=sessions,
                page=page,
                page_size=self._cfg_int("roster_page_size", 20),
            )

        if arg in ("全部", "all", "*"):
            if not self._require_admin(event):
                return "批量订阅只有管理员能用。\n本群订阅发 /steam 订阅 即可。"
            chats = await self._store.chats()
            umos = [umo for umo, rec in chats if rec.get("is_group", True)]
            if not umos:
                return "还没有记录到任何群。先在各群发一次 /steam 任意指令，我才能知道那些群存在。"
            added = await self._store.subscribe_many(umos)
            scope = self._cfg_str("monitor_scope", "bound")
            tail = "但当前 monitor_scope = off，不会有推送。" if scope == "off" else ""
            return (
                f"已批量订阅 {len(umos)} 个群（新增 {added} 个，其余此前已订阅）。{tail}\n"
                "只覆盖群聊；私聊不推送。用 /steam 订阅 列表 核对。"
            )

        umo = self._umo(event)
        if not umo:
            return "拿不到当前会话标识，无法订阅。"
        await self._store.subscribe(umo)
        scope = self._cfg_str("monitor_scope", "bound")
        if scope == "off":
            return "已记录订阅，但当前 monitor_scope = off，不会有推送。"
        interval = max(60, self._cfg_int("poll_interval", 180))
        return (
            f"本会话已订阅推送，轮询间隔 {interval} 秒。\n"
            f"监控范围：{'仅已绑定账号' if scope == 'bound' else '全部好友名单'}\n"
            "其它群也想开？管理员发 /steam 订阅 全部 一次搞定。"
        )

    async def _cmd_unsubscribe(self, event: AstrMessageEvent, rest: list[str]) -> str:
        arg = rest[0].lower() if rest else ""
        if arg in ("全部", "all", "*"):
            if not self._require_admin(event):
                return "批量退订只有管理员能用。"
            sessions = await self._store.sessions()
            if not sessions:
                return "当前没有任何订阅。"
            removed = await self._store.unsubscribe_many(sessions)
            return f"已关闭全部 {removed} 个订阅会话。"

        umo = self._umo(event)
        if not umo:
            return "拿不到当前会话标识。"
        return (
            "已关闭本会话推送。" if await self._store.unsubscribe(umo) else "本会话本来就没订阅。"
        )

    async def _touch_chat(self, event: AstrMessageEvent) -> None:
        """记录会话，供 ``/steam 订阅 全部`` 枚举。失败不影响指令本身。"""
        umo = self._umo(event)
        if not umo:
            return
        group_name, group_id = self._chat_label(event)
        in_group = self._is_group_chat(event)
        if in_group and group_id:
            label = f"{group_name}（{group_id}）" if group_name else group_id
        elif in_group:
            label = "未知群"
        else:
            label = "私聊"
        try:
            await self._store.record_chat(
                umo,
                label=label,
                platform=self._platform(event),
                is_group=in_group,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Steam好友中心] 记录会话失败：{exc}")

    async def _cmd_status(self, event: AstrMessageEvent) -> str:
        if not self._require_admin(event):
            return "只有管理员能看运行状态。"
        stats = await self._store.stats()
        key_set = "已配置" if self._cfg_str("steam_api_key") else "未配置（所有接口都会失败）"
        return (
            "Steam 好友中心\n"
            f"API Key：{key_set}\n"
            f"好友名单：{stats['roster']} 人\n"
            f"已绑定：{stats['bindings']} 个\n"
            f"订阅会话：{stats['sessions']} 个（记录到的群 {stats['chats']} 个）\n"
            f"监控范围：{self._cfg_str('monitor_scope', 'bound')}\n"
            f"轮询间隔：{max(60, self._cfg_int('poll_interval', 180))} 秒\n"
            f"下一个序号：#{stats['next_index']}"
        )

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #

    async def _poll_loop(self) -> None:
        interval = max(60, self._cfg_int("poll_interval", 180))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[Steam好友中心] 本轮轮询失败：{type(exc).__name__}: {exc}")

    async def _poll_once(self) -> None:
        scope = self._cfg_str("monitor_scope", "bound")
        if scope == "off":
            return

        if scope == "roster":
            ids = [sid for sid, _ in await self._store.roster_items()]
        else:
            ids = await self._store.bound_steamids()
        if not ids:
            return

        sessions = await self._store.sessions()
        players = await self._client.summaries(ids)
        notify_stop = self._cfg_bool("notify_on_stop", True)
        notify_presence = self._cfg_bool("notify_online_offline", False)
        now = int(time.time())

        snapshots: dict[str, dict[str, Any]] = {}
        events: list[str] = []

        for sid in ids:
            player = players.get(sid)
            if not player:
                continue

            previous = await self._store.get_watch(sid)
            first_sight = not previous

            gameid = str(player.get("gameid") or "")
            game = str(player.get("gameextrainfo") or "")
            online = 1 if player.get("personastate") else 0
            name = str(player.get("personaname") or previous.get("name") or sid)

            previous_game = str(previous.get("gameid") or "")
            previous_online = int(previous.get("online") or 0)
            game_since = int(previous.get("game_since") or 0)

            if not first_sight and sessions:
                if gameid and gameid != previous_game:
                    game_since = now
                    events.append(render.notify_playing(name=name, game=game, appid=gameid))
                elif not gameid and previous_game:
                    if notify_stop:
                        duration = max(0, now - game_since) if game_since else 0
                        events.append(
                            render.notify_stop(
                                name=name, game=str(previous.get("game") or ""), seconds=duration
                            )
                        )
                    game_since = 0
                if notify_presence and online != previous_online:
                    events.append(
                        render.notify_online(name) if online else render.notify_offline(name)
                    )
            elif gameid and not game_since:
                game_since = now
            elif not gameid:
                game_since = 0

            snapshots[sid] = {
                "gameid": gameid,
                "game": game,
                "online": online,
                "game_since": game_since,
                "name": name,
            }

        await self._store.set_watch_many(snapshots)
        for text in events:
            for umo in sessions:
                await self._send(umo, text)

    # ------------------------------------------------------------------ #
    # 目标解析
    # ------------------------------------------------------------------ #

    async def _resolve_ref(self, raw: str) -> tuple[str, str]:
        """把 ``#12`` / SteamID64 / 主页链接 解析成 ``(steamid, 显示名)``。"""
        text = raw.lstrip("#＃").strip()
        if not text:
            raise SteamError("没有给出要绑定的 Steam 账号")

        if _INDEX_RE.match(text) and len(text) <= 5:
            found = await self._store.find_by_index(int(text))
            if not found:
                raise SteamError(f"名单里没有 #{text}，用 /steam 好友 看一下序号")
            return found[0], str(found[1].get("name") or "")

        steamid = await self._client.resolve_id(text)
        label = await self._store.roster_name(steamid)
        if label:
            return steamid, label

        # 不在好友名单里也允许绑（好友列表本来就装不下所有想追踪的人）。
        # 补一次摘要拿昵称，Steam 不通时不影响绑定本身。
        try:
            players = await self._client.summaries([steamid])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Steam好友中心] 绑定 {steamid} 时取昵称失败：{exc}")
            players = {}
        player = players.get(steamid) or {}
        return steamid, str(player.get("personaname") or "")

    async def _resolve_query_target(
        self, event: AstrMessageEvent, rest: list[str]
    ) -> tuple[str, str]:
        """返回 ``(steamid, 给用户的提示)``，解析失败时 steamid 为空。"""
        ats = self._extract_ats(event)
        if ats:
            record = await self._store.get_binding(f"{self._platform(event)}:{ats[0]}")
            if not record:
                return "", f"用户 {ats[0]} 还没绑定 Steam 账号。"
            return str(record.get("steamid") or ""), ""

        if rest:
            raw = rest[0]
            text = raw.lstrip("#＃")
            if _INDEX_RE.match(raw) and len(text) <= 5:
                found = await self._store.find_by_index(int(text))
                if not found:
                    return "", f"名单里没有 #{text}。"
                return found[0], ""
            if len(raw) >= 17 or "/" in raw or "." in raw:
                return await self._client.resolve_id(raw), ""

        record = await self._store.get_binding(self._binding_key(event))
        if not record:
            return "", self._bind_hint(event)
        return str(record.get("steamid") or ""), ""

    # ------------------------------------------------------------------ #
    # 推送
    # ------------------------------------------------------------------ #

    async def _send(self, umo: str, text: str) -> bool:
        sender = getattr(self.context, "send_message", None)
        if not callable(sender):
            logger.warning("[Steam好友中心] 当前 AstrBot 版本不支持主动发送")
            return False
        if MessageChain is not None and Plain is not None:
            try:
                await sender(umo, MessageChain([Plain(text)]))
                return True
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[Steam好友中心] 消息链发送失败，回退纯文本：{exc}")
        try:
            await sender(umo, text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Steam好友中心] 推送到 {umo} 失败：{exc}")
            return False

    # ------------------------------------------------------------------ #
    # 事件 / 配置工具
    # ------------------------------------------------------------------ #

    def _platform(self, event: AstrMessageEvent) -> str:
        for attr in ("get_platform_name", "get_platform_id"):
            getter = getattr(event, attr, None)
            if not callable(getter):
                continue
            try:
                value = getter()
            except Exception:  # noqa: BLE001
                continue
            if value:
                return str(value)
        return "unknown"

    def _sender_id(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:  # noqa: BLE001
                pass
        return str(getattr(event, "user_id", "") or "")

    def _binding_key(self, event: AstrMessageEvent) -> str:
        """绑定跟人走，不跟群走。"""
        return f"{self._platform(event)}:{self._sender_id(event)}"

    def _chat_label(self, event: AstrMessageEvent) -> tuple[str, str]:
        """当前会话的（群名，群 ID）。私聊时群 ID 为空。

        群名各家适配器给的字段不一致，按 ``get_group_name()`` → ``message_obj.group``
        → ``message_obj`` 依次探测，全都拿不到就只显示群 ID。
        """
        group_id = ""
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                group_id = str(getter() or "")
            except Exception:  # noqa: BLE001
                group_id = ""
        if not group_id:
            group_id = str(getattr(event, "group_id", "") or "")

        name = ""
        getter = getattr(event, "get_group_name", None)
        if callable(getter):
            try:
                name = str(getter() or "").strip()
            except Exception:  # noqa: BLE001
                name = ""
        if not name:
            message_obj = getattr(event, "message_obj", None)
            holders = (getattr(message_obj, "group", None), message_obj)
            for holder in holders:
                if holder is None:
                    continue
                for attr in ("group_name", "name", "channel_name", "guild_name"):
                    value = getattr(holder, attr, None)
                    if isinstance(value, str) and value.strip():
                        name = value.strip()
                        break
                if name:
                    break
        return name, group_id

    def _group_key(self, event: AstrMessageEvent) -> str:
        """按群存的键。带上平台，避免 KOOK 频道和 QQ 群撞号。"""
        _name, group_id = self._chat_label(event)
        return f"{self._platform(event)}:{group_id}"

    def _is_group_chat(self, event: AstrMessageEvent) -> bool:
        """是不是群聊。群 ID 取不到时退回看消息类型。

        这个判断只用于「要不要开放完整信息」：宁可把拿不到 ID 的群当成群聊（保守），
        也不能因为字段缺失就把它当私聊、把全名和完整 SteamID 打进群里。
        """
        _name, group_id = self._chat_label(event)
        if group_id:
            return True
        mtype = str(getattr(getattr(event, "message_obj", None), "type", "") or "").lower()
        return "group" in mtype

    def _plugin_modes(self) -> tuple[str, str, str]:
        """插件配置里的三个默认值：群聊昵称、群聊 ID、私聊 ID。"""
        return (
            _pick(self._cfg_str("roster_group_privacy", "masked"), _NAME_MODES, "masked"),
            _pick(self._cfg_str("roster_group_show_id", "hidden"), _GROUP_ID_MODES, "hidden"),
            _pick(self._cfg_str("roster_private_show_id", "full"), _PRIVATE_ID_MODES, "full"),
        )

    async def _display_modes(self, event: AstrMessageEvent) -> tuple[str, str, bool, bool]:
        """当前会话实际生效的 ``(昵称模式, SteamID 模式, 昵称被本群改过, ID 被本群改过)``。

        私聊固定全名 + 插件配置里的私聊 ID；群聊优先本群设置，没设过就用插件配置。
        群消息但拿不到群 ID 时（个别适配器），用插件默认值，不当私聊处理。
        """
        plugin_name, plugin_id, private_id = self._plugin_modes()
        _name, group_id = self._chat_label(event)
        if not group_id:
            if self._is_group_chat(event):
                return plugin_name, plugin_id, False, False
            return "full", private_id, False, False

        saved = await self._store.group_setting(self._group_key(event))
        raw_name = str(saved.get("name") or "").strip().lower()
        raw_id = str(saved.get("steamid") or "").strip().lower()
        name_mode = raw_name if raw_name in _NAME_MODES else plugin_name
        id_mode = raw_id if raw_id in _GROUP_ID_MODES else plugin_id
        return name_mode, id_mode, raw_name in _NAME_MODES, raw_id in _GROUP_ID_MODES

    def _at_name(self, event: AstrMessageEvent, uid: str) -> str:
        """被 @ 的人在消息链里带的昵称，拿不到就返回空。"""
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for comp in message:
            if type(comp).__name__ != "At" and getattr(comp, "type", "") != "at":
                continue
            if str(getattr(comp, "qq", "") or "") != uid:
                continue
            for attr in ("name", "nickname", "display_name"):
                value = getattr(comp, attr, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _umo(self, event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                return value
        return ""

    def _extract_ats(self, event: AstrMessageEvent) -> list[str]:
        """从消息链里抠出被 @ 的用户 ID。"""
        found: list[str] = []
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for comp in message:
            if type(comp).__name__ != "At" and getattr(comp, "type", "") != "at":
                continue
            qq = str(getattr(comp, "qq", "") or "")
            if qq and qq != "all" and qq not in found:
                found.append(qq)
        return found

    def _require_admin(self, event: AstrMessageEvent) -> bool:
        admins = self._cfg_list("admin_ids")
        if not admins:
            return True
        return self._sender_id(event) in admins

    def _resolve_data_dir(self) -> Path:
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:  # pragma: no cover
            fallback = Path("data") / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(f"[Steam好友中心] 数据目录回退到 {fallback}（{exc}）")
            return fallback

    @staticmethod
    def _strip_command(message_str: str, command: str) -> list[str]:
        text = (message_str or "").replace("\u3000", " ").strip()
        text = re.sub(r"^(?:@\S+\s+)+", "", text)
        text = re.sub(
            rf"^[~/!#！]{{0,2}}\s*{re.escape(command)}(?=\s|$)\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return text.split() if text else []

    @property
    def _config_path(self) -> str:
        """定位插件配置文件。

        框架给的 ``AstrBotConfig`` 带 ``config_path``，直接用；拿不到时按数据目录反推：
        数据目录是 ``data/plugin_data/<插件名>``，配置文件在同级的 ``data/config/`` 下。
        """
        if self._cfg_file is not None:
            return self._cfg_file
        path = ""
        try:
            path = str(getattr(self._conf, "config_path", "") or "")
        except Exception:  # noqa: BLE001
            path = ""
        if not path:
            try:
                guess = self._data_dir.parent.parent / "config" / f"{PLUGIN_NAME}_config.json"
                if guess.is_file():
                    path = str(guess)
            except Exception:  # noqa: BLE001
                path = ""
        self._cfg_file = path
        return path

    def _sync_config(self) -> None:
        """WebUI 保存配置不会重载插件，这里按文件 mtime 自动重读。

        AstrBot 的保存接口只写 ``data/config/<插件名>_config.json``，正在跑的实例仍持有旧
        dict。每次读配置前 stat 一下文件，mtime 变了就把新值合并进来，省掉手动重载。
        """
        path = self._config_path
        if not path:
            return
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return
        if self._cfg_mtime < 0:  # 首次只记时间戳，值已经由框架加载好了
            self._cfg_mtime = mtime
            return
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            self._conf.update(data)
        except Exception:  # noqa: BLE001 配置对象只读时放弃热更新，不影响主流程
            return
        logger.info(f"[Steam好友中心] 配置文件已变更，自动重读：{self._describe_conf(data)}")

    def _raw(self, key: str, default: Any = None) -> Any:
        self._sync_config()
        try:
            value = self._conf.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    @staticmethod
    def _mask(value: str) -> str:
        """日志里打码：只露长度，不露内容。"""
        text = str(value or "")
        if not text:
            return "空"
        if len(text) <= 8:
            return f"{len(text)} 位"
        return f"{text[:4]}…（{len(text)} 位）"

    def _describe_conf(self, conf: dict | None = None) -> str:
        """逐项自证，用来区分「没保存」和「保存了没读到」。"""
        data = conf if conf is not None else self._conf
        try:
            key = str(data.get("steam_api_key") or "")
            owner = str(data.get("owner_steamid") or "")
            owners = data.get("admin_ids") or []
        except Exception:  # noqa: BLE001
            return "配置读取失败"
        count = len(owners) if isinstance(owners, (list, tuple, set)) else 0
        return f"key={self._mask(key)} owner={owner or '空'} 管理员 {count} 人"

    def _cfg_str(self, key: str, default: str = "") -> str:
        return str(self._raw(key, default) or "").strip()

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._raw(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(float(self._raw(key, default)))
        except (TypeError, ValueError):
            return default

    def _cfg_list(self, key: str) -> list[str]:
        value = self._raw(key, [])
        if isinstance(value, str):
            items = re.split(r"[,，\s]+", value.strip())
        elif isinstance(value, (list, tuple, set)):
            items = [str(v) for v in value]
        else:
            items = []
        return [str(item).strip() for item in items if str(item).strip()]
