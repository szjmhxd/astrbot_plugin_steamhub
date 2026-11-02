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
            elif sub in ("绑定", "bind"):
                text = await self._cmd_bind(event, rest)
            elif sub in ("解绑", "unbind"):
                text = await self._cmd_unbind(event, rest)
            elif sub in ("我", "me", "whoami"):
                text = await self._cmd_me(event)
            elif sub in ("查", "查询", "q", "query", "info"):
                text = await self._cmd_query(event, rest)
            elif sub in ("订阅", "sub", "subscribe"):
                text = await self._cmd_subscribe(event)
            elif sub in ("取订", "退订", "unsub", "unsubscribe"):
                text = await self._cmd_unsubscribe(event)
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
        return render.roster_text(
            items,
            page=page,
            page_size=self._cfg_int("roster_page_size", 20),
            bound_count=bound,
            title=title,
        )

    # ------------------------------------------------------------------ #
    # /steam 绑定
    # ------------------------------------------------------------------ #

    async def _cmd_bind(self, event: AstrMessageEvent, rest: list[str]) -> str:
        ats = self._extract_ats(event)

        ref_raw = ""
        target_uid = ""
        for token in rest:
            if token.startswith(_AT_PREFIX):
                continue
            if not ref_raw:
                ref_raw = token
            elif not target_uid and token.isdigit() and 5 <= len(token) <= 12:
                target_uid = token

        if ats:
            target_uid = ats[0]

        if not ref_raw:
            return (
                "用法：/steam 绑定 #12\n"
                "      /steam 绑定 #12 @小王\n"
                "      /steam 绑定 76561198000000000"
            )

        if target_uid and target_uid != self._sender_id(event):
            if not self._require_admin(event):
                return "只有管理员能给别人绑定。"

        steamid, label = await self._resolve_ref(ref_raw)
        platform = self._platform(event)
        uid = target_uid or self._sender_id(event)
        if not uid:
            return "拿不到你的用户 ID，无法绑定。"

        await self._store.bind(f"{platform}:{uid}", steamid, label)
        who = "你" if uid == self._sender_id(event) else f"用户 {uid}"
        display = label or steamid
        return (
            f"已绑定：{who} → {display}\n"
            f"SteamID64：{steamid}\n"
            "之后 /steam 查 就查这个账号，上线/开玩也会推送（在群里发 /steam 订阅 开启）。"
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
            return "你还没绑定。先看 /steam 好友 找到自己的序号，然后 /steam 绑定 #序号"
        steamid = str(record.get("steamid") or "")
        return (
            f"你绑定的是：{record.get('label') or '未命名'}\n"
            f"SteamID64：{steamid}\n"
            f"绑定时间：{render.fmt_rel_time(record.get('updated'))}"
        )

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

    async def _cmd_subscribe(self, event: AstrMessageEvent) -> str:
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
            f"监控范围：{'仅已绑定账号' if scope == 'bound' else '全部好友名单'}"
        )

    async def _cmd_unsubscribe(self, event: AstrMessageEvent) -> str:
        umo = self._umo(event)
        if not umo:
            return "拿不到当前会话标识。"
        return (
            "已关闭本会话推送。" if await self._store.unsubscribe(umo) else "本会话本来就没订阅。"
        )

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
            f"订阅会话：{stats['sessions']} 个\n"
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
        return steamid, await self._store.roster_name(steamid)

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
            return "", (
                "你还没绑定 Steam 账号。\n"
                "先用 /steam 好友 找到自己的序号，再 /steam 绑定 #序号\n"
                "或者直接 /steam 查 #序号"
            )
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
