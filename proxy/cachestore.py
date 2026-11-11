"""单进程内存缓存与同键请求合并。

设计要点：

1. 缓存键 = 公告 ID + Accept-Language 原始值（缺失按空串处理）。
2. 同键并发未命中：第一个请求成为 leader，仅发一次上游请求；其余请求
   等待同一个 asyncio.Future。等待者任务被取消（客户端断开）只取消它
   自己，leader 的上游请求在独立的屏蔽任务中继续，不影响其他等待者。
3. 按公告清除：清除该公告全部语言缓存，并 bump 该公告的 epoch。清除前
   已存在的在途请求即使晚于清除返回，也不得回填缓存（结果仍会交给当时
   已在等待的请求，但不会写入新缓存）；清除后的新请求看到新 epoch，会
   发起全新的上游请求，绝不复用旧在途结果。
4. 过期条目惰性失效，绝不复用过期响应。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class CachedResponse:
    status_code: int
    body: bytes
    # 仅保留可安全缓存的响应头（已过滤 hop-by-hop / Set-Cookie）
    headers: list[tuple[bytes, bytes]]
    cached_at: float
    expires_at: float
    max_age: int
    generation: int

    def is_fresh(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) < self.expires_at


@dataclass
class InFlight:
    """同键未命中请求的合并单元。"""

    key: str
    notice_id: str
    generation: int
    future: asyncio.Future
    leader_task: asyncio.Task
    purged: bool = False  # 清除发生在该在途单元存活期间


@dataclass
class CacheHit:
    entry: CachedResponse


@dataclass
class CacheWait:
    """加入既有在途单元，等待 leader 的结果。"""

    inflight: InFlight


@dataclass
class CacheLeader:
    """本请求是 leader，需要实际请求上游。"""

    inflight: InFlight


LookupResult = CacheHit | CacheWait | CacheLeader


class CacheStore:
    def __init__(self) -> None:
        # key = (notice_id, accept_language)
        self._cache: dict[tuple[str, str], CachedResponse] = {}
        self._inflight: dict[tuple[str, str], InFlight] = {}
        # 每个公告的代际计数，清除一次 +1
        self._generations: dict[str, int] = {}
        # 仅用于管理端展示
        self.lock = asyncio.Lock()

    @staticmethod
    def make_key(notice_id: str, accept_language: str) -> tuple[str, str]:
        return notice_id, accept_language or ""

    async def lookup(
        self, key: tuple[str, str], notice_id: str
    ) -> LookupResult:
        """返回缓存命中 / 加入等待 / 成为 leader 三种情形之一。"""
        async with self.lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached is not None:
                if cached.is_fresh(now):
                    return CacheHit(cached)
                # 过期不复用，直接移除并继续走上游
                self._cache.pop(key, None)

            existing = self._inflight.get(key)
            if existing is not None:
                return CacheWait(existing)

            generation = self._generations.get(notice_id, 0)
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            # leader_task 先占位，创建协程后补入；屏蔽取消，使单个
            # 等待者（含 leader 自身客户端）断开不影响其他人。
            inflight = InFlight(
                key=key,
                notice_id=notice_id,
                generation=generation,
                future=future,
                leader_task=None,  # type: ignore[arg-type]
            )
            self._inflight[key] = inflight
            return CacheLeader(inflight)

    async def store_if_current(
        self, inflight: InFlight, entry: CachedResponse | None
    ) -> bool:
        """leader 完成后调用。

        只有在途单元仍是当前公告代际、且期间没有发生清除时才写入缓存。
        返回 True 表示已回填；False 表示已被清除，禁止回填。
        """
        async with self.lock:
            current = self._inflight.get(inflight.key)
            if current is not inflight:
                return False
            self._inflight.pop(inflight.key, None)
            if inflight.purged:
                return False
            if entry is None:
                return False
            if self._generations.get(inflight.notice_id, 0) != inflight.generation:
                return False
            self._cache[inflight.key] = entry
            return True

    async def fail_all(self, inflight: InFlight, exc: BaseException) -> None:
        """上游失败：结束所有等待者，且不缓存。"""
        async with self.lock:
            current = self._inflight.pop(inflight.key, None)
        if current is not inflight:
            return
        if not inflight.future.done():
            inflight.future.set_exception(exc)
        if inflight.leader_task is not None and not inflight.leader_task.done():
            inflight.leader_task.cancel()

    async def purge_notice(self, notice_id: str) -> dict[str, int]:
        """按公告清除全部语言缓存；旧在途标记 purged，禁止其回填。"""
        purged_keys: list[tuple[str, str]] = []
        marked_inflight = 0
        async with self.lock:
            self._generations[notice_id] = (
                self._generations.get(notice_id, 0) + 1
            )
            for key in list(self._cache.keys()):
                if key[0] == notice_id:
                    purged_keys.append(key)
                    self._cache.pop(key, None)
            for key, unit in list(self._inflight.items()):
                if key[0] == notice_id:
                    unit.purged = True
                    marked_inflight += 1
        return {
            "purged_cache_entries": len(purged_keys),
            "marked_inflight": marked_inflight,
            "generation": self._generations[notice_id],
        }

    async def reset(self) -> dict[str, int]:
        """清空全部缓存与在途状态（演示/测试用，不改变代际计数）。"""
        async with self.lock:
            n_cache = len(self._cache)
            n_inflight = len(self._inflight)
            self._cache.clear()
            self._inflight.clear()
            return {"cleared_cache": n_cache, "cleared_inflight": n_inflight}

    async def snapshot(self) -> dict:
        """管理/演示页查看内部状态。"""
        now = time.monotonic()
        async with self.lock:
            cache_view = []
            for (nid, lang), item in sorted(self._cache.items()):
                cache_view.append(
                    {
                        "notice_id": nid,
                        "accept_language": lang,
                        "status_code": item.status_code,
                        "max_age": item.max_age,
                        "generation": item.generation,
                        "age_seconds": round(now - item.cached_at, 3),
                        "remaining_seconds": round(
                            max(0.0, item.expires_at - now), 3
                        ),
                        "fresh": item.is_fresh(now),
                    }
                )
            inflight_view = [
                {
                    "notice_id": unit.notice_id,
                    "accept_language": unit.key[1],
                    "generation": unit.generation,
                    "purged": unit.purged,
                    "waiters": len(unit.future._callbacks),  # type: ignore[attr-defined]
                }
                for unit in sorted(
                    self._inflight.values(), key=lambda u: u.key
                )
            ]
            return {
                "cache": cache_view,
                "inflight": inflight_view,
                "generations": dict(sorted(self._generations.items())),
            }
