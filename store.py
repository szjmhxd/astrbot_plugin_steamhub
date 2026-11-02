"""好友名单 + 用户绑定的本地持久化。

一张 JSON 搞定五件事：

``roster``    导入的好友名单，每个好友有**固定序号**（重导入不变，绑定时靠它指代）
``bindings``  平台用户 → SteamID64，这是后续一切查询和推送的依据
``sessions``  订阅了推送的会话
``watch``     上一次轮询看到的状态，用来做差异判断
``groups``    每个群自己的名单显示设置，缺省跟随插件配置

序号为什么必须固定：用户会用 ``/steam 绑定 #12`` 这种写法，如果每次导入都按
昵称重新排序，序号就会漂，历史绑定全部错位。所以序号是单调递增的计数器，
只在第一次入库时分配，之后永不改动。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

try:  # 单测环境没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

MAX_ROSTER = 5000
MAX_BINDINGS = 50000
MAX_SESSIONS = 1000
MAX_GROUPS = 2000


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "next_index": 1,
        "roster": {},
        "bindings": {},
        "sessions": {},
        "watch": {},
        "groups": {},
    }


class SteamStore:
    """``SteamHub`` 的全部持久化状态，带进程内锁与原子写入。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = empty_state()
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #

    async def load(self) -> None:
        async with self._lock:
            self._loaded = True
            if not self._path.exists():
                self._state = empty_state()
                return
            try:
                raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("顶层不是对象")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning(f"[Steam好友中心] 存储文件读取失败，按空表处理：{exc}")
                self._state = empty_state()
                return

            state = empty_state()
            for key in ("roster", "bindings", "sessions", "watch", "groups"):
                value = parsed.get(key)
                if isinstance(value, dict):
                    state[key] = {str(k): v for k, v in value.items() if isinstance(v, dict)}
            try:
                state["next_index"] = max(1, int(parsed.get("next_index") or 1))
            except (TypeError, ValueError):
                state["next_index"] = 1

            # 历史数据里序号可能缺失，补一遍并推进计数器
            highest = 0
            for record in state["roster"].values():
                try:
                    index = int(record.get("index") or 0)
                except (TypeError, ValueError):
                    index = 0
                highest = max(highest, index)
            state["next_index"] = max(state["next_index"], highest + 1)
            self._state = state

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.load()

    async def _flush_locked(self) -> None:
        payload = json.dumps(self._state, ensure_ascii=False, indent=2)
        tmp = self._path.with_suffix(".tmp")

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self._path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.error(f"[Steam好友中心] 存储写入失败：{exc}")

    # ------------------------------------------------------------------ #
    # 好友名单
    # ------------------------------------------------------------------ #

    async def upsert_roster(
        self,
        players: dict[str, dict[str, Any]],
        friend_since: dict[str, int] | None = None,
    ) -> tuple[int, int]:
        """写入/刷新好友名单。返回 ``(新增数, 更新数)``。序号只在新入库时分配。"""
        await self._ensure_loaded()
        friend_since = friend_since or {}
        async with self._lock:
            roster = self._state["roster"]
            now = int(time.time())
            added = updated = 0

            # 新好友按昵称排序后依次分配序号，保证同一批导入内序号可预期
            new_ids = sorted(
                (sid for sid in players if sid not in roster),
                key=lambda sid: str(players[sid].get("personaname") or "").lower(),
            )

            for sid in new_ids:
                if len(roster) >= MAX_ROSTER:
                    logger.warning("[Steam好友中心] 好友名单已达上限，剩余新增被忽略")
                    break
                player = players[sid]
                roster[sid] = {
                    "index": int(self._state["next_index"]),
                    "name": str(player.get("personaname") or sid),
                    "avatar": str(player.get("avatarfull") or ""),
                    "profileurl": str(player.get("profileurl") or ""),
                    "friend_since": int(friend_since.get(sid) or 0),
                    "synced": now,
                }
                self._state["next_index"] = int(self._state["next_index"]) + 1
                added += 1

            for sid, player in players.items():
                record = roster.get(sid)
                if not record:
                    continue
                record["name"] = str(player.get("personaname") or record.get("name") or sid)
                record["avatar"] = str(player.get("avatarfull") or record.get("avatar") or "")
                record["profileurl"] = str(
                    player.get("profileurl") or record.get("profileurl") or ""
                )
                record["synced"] = now
                if sid in friend_since and not record.get("friend_since"):
                    record["friend_since"] = int(friend_since[sid] or 0)
                updated += 1

            await self._flush_locked()
            return added, updated

    async def roster_items(self) -> list[tuple[str, dict[str, Any]]]:
        await self._ensure_loaded()
        async with self._lock:
            items = [(sid, dict(rec)) for sid, rec in self._state["roster"].items()]
        items.sort(key=lambda kv: int(kv[1].get("index") or 0))
        return items

    async def roster_count(self) -> int:
        await self._ensure_loaded()
        async with self._lock:
            return len(self._state["roster"])

    async def find_by_index(self, index: int) -> tuple[str, dict[str, Any]] | None:
        await self._ensure_loaded()
        async with self._lock:
            for sid, rec in self._state["roster"].items():
                try:
                    if int(rec.get("index") or 0) == index:
                        return sid, dict(rec)
                except (TypeError, ValueError):
                    continue
        return None

    async def search_roster(self, keyword: str) -> list[tuple[str, dict[str, Any]]]:
        """按昵称模糊匹配（不区分大小写）。"""
        needle = keyword.strip().lower()
        items = await self.roster_items()
        if not needle:
            return items
        return [kv for kv in items if needle in str(kv[1].get("name") or "").lower()]

    async def roster_name(self, steamid: str) -> str:
        await self._ensure_loaded()
        async with self._lock:
            record = self._state["roster"].get(steamid) or {}
            return str(record.get("name") or "")

    # ------------------------------------------------------------------ #
    # 绑定
    # ------------------------------------------------------------------ #

    async def bind(self, key: str, steamid: str, label: str = "") -> None:
        """绑定 ``平台:用户`` → SteamID64。解除旧绑定在这里隐式完成。"""
        await self._ensure_loaded()
        async with self._lock:
            if len(self._state["bindings"]) >= MAX_BINDINGS and key not in self._state["bindings"]:
                logger.warning("[Steam好友中心] 绑定表已达上限，写入被忽略")
                return
            self._state["bindings"][key] = {
                "steamid": steamid,
                "label": label,
                "updated": int(time.time()),
            }
            await self._flush_locked()

    async def unbind(self, key: str) -> dict[str, Any] | None:
        await self._ensure_loaded()
        async with self._lock:
            record = self._state["bindings"].pop(key, None)
            if record:
                await self._flush_locked()
            return dict(record) if record else None

    async def get_binding(self, key: str) -> dict[str, Any] | None:
        await self._ensure_loaded()
        async with self._lock:
            record = self._state["bindings"].get(key)
            return dict(record) if record else None

    async def bound_steamids(self) -> list[str]:
        """所有被绑定的 SteamID，去重。轮询就扫这个集合。"""
        await self._ensure_loaded()
        async with self._lock:
            return sorted(
                {
                    str(rec.get("steamid"))
                    for rec in self._state["bindings"].values()
                    if rec.get("steamid")
                }
            )

    async def bindings_of(self, steamid: str) -> list[str]:
        await self._ensure_loaded()
        async with self._lock:
            return [
                key
                for key, rec in self._state["bindings"].items()
                if str(rec.get("steamid")) == steamid
            ]

    async def binding_count(self) -> int:
        await self._ensure_loaded()
        async with self._lock:
            return len(self._state["bindings"])

    # ------------------------------------------------------------------ #
    # 订阅会话
    # ------------------------------------------------------------------ #

    async def subscribe(self, session: str) -> None:
        await self._ensure_loaded()
        async with self._lock:
            if (
                len(self._state["sessions"]) >= MAX_SESSIONS
                and session not in self._state["sessions"]
            ):
                logger.warning("[Steam好友中心] 订阅会话已达上限")
                return
            self._state["sessions"][session] = {"added": int(time.time())}
            await self._flush_locked()

    async def unsubscribe(self, session: str) -> bool:
        await self._ensure_loaded()
        async with self._lock:
            if session not in self._state["sessions"]:
                return False
            del self._state["sessions"][session]
            await self._flush_locked()
            return True

    async def sessions(self) -> list[str]:
        await self._ensure_loaded()
        async with self._lock:
            return list(self._state["sessions"].keys())

    async def is_subscribed(self, session: str) -> bool:
        await self._ensure_loaded()
        async with self._lock:
            return session in self._state["sessions"]

    # ------------------------------------------------------------------ #
    # 按群的显示设置
    # ------------------------------------------------------------------ #

    async def group_setting(self, group: str) -> dict[str, str]:
        """该群自定义的显示设置。空字典 = 全部跟随插件配置。"""
        await self._ensure_loaded()
        async with self._lock:
            record = self._state["groups"].get(group) or {}
            return {k: str(v) for k, v in record.items() if isinstance(v, str)}

    async def set_group_setting(
        self, group: str, *, name: str | None = None, steamid: str | None = None
    ) -> dict[str, str]:
        """只更新传入的字段，``None`` 表示该项不动。"""
        await self._ensure_loaded()
        async with self._lock:
            groups = self._state["groups"]
            if group not in groups and len(groups) >= MAX_GROUPS:
                logger.warning("[Steam好友中心] 群设置已达上限，写入被忽略")
                return {}
            record = dict(groups.get(group) or {})
            if name is not None:
                record["name"] = name
            if steamid is not None:
                record["steamid"] = steamid
            record["updated"] = int(time.time())
            groups[group] = record
            await self._flush_locked()
            return {k: str(v) for k, v in record.items() if isinstance(v, str)}

    async def reset_group_setting(self, group: str) -> bool:
        """删掉该群的自定义，回到跟随插件配置。返回是否真的删了东西。"""
        await self._ensure_loaded()
        async with self._lock:
            if group not in self._state["groups"]:
                return False
            del self._state["groups"][group]
            await self._flush_locked()
            return True

    # ------------------------------------------------------------------ #
    # 轮询快照
    # ------------------------------------------------------------------ #

    async def get_watch(self, steamid: str) -> dict[str, Any]:
        await self._ensure_loaded()
        async with self._lock:
            record = self._state["watch"].get(steamid)
            return dict(record) if record else {}

    async def set_watch(self, steamid: str, snapshot: dict[str, Any]) -> None:
        await self._ensure_loaded()
        async with self._lock:
            self._state["watch"][steamid] = {**snapshot, "ts": int(time.time())}
            await self._flush_locked()

    async def set_watch_many(self, snapshots: dict[str, dict[str, Any]]) -> None:
        """一次轮询只有一次落盘，避免逐个写文件。"""
        if not snapshots:
            return
        await self._ensure_loaded()
        async with self._lock:
            now = int(time.time())
            for sid, snapshot in snapshots.items():
                self._state["watch"][sid] = {**snapshot, "ts": now}
            await self._flush_locked()

    async def stats(self) -> dict[str, int]:
        await self._ensure_loaded()
        async with self._lock:
            return {
                "roster": len(self._state["roster"]),
                "bindings": len(self._state["bindings"]),
                "sessions": len(self._state["sessions"]),
                "next_index": int(self._state["next_index"]),
            }
