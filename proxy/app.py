"""FastAPI 单进程 HTTP 缓存代理。

规则摘要：
- 仅代理 GET /notices/{notice_id}，上游地址固定（环境变量 UPSTREAM_BASE）。
- 带 Cookie 或 Authorization 的请求直连上游：不读、不写缓存，也不参与
  匿名请求合并。
- 匿名响应仅在 200 + Cache-Control 含 public 且含合法非负整数 max-age、
  无 private/no-store、无 Set-Cookie 时缓存；过期不复用。
- 同键并发未命中只请求上游一次；单个等待者断开不影响其他等待者；
  上游失败不缓存并结束所有等待。
- 按公告清除会删除该公告全部语言缓存，并使清除前的在途响应无法回填、
  清除后的新请求无法复用旧在途结果。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .cachestore import (
    CachedResponse,
    CacheHit,
    CacheLeader,
    CacheStore,
    CacheWait,
    InFlight,
)
from .policy import cache_eligibility

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "http://upstream:8001").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "15"))

# 转发给上游的请求头白名单（逐跳头、Host 等不带）
FORWARD_REQUEST_HEADERS = {
    "accept",
    "accept-language",
    "user-agent",
    "cookie",
    "authorization",
    "x-request-id",
}

# 回传给客户端 / 写入缓存的响应头白名单。
# content-length / transfer-encoding / content-encoding 由框架按解码后的
# body 重新处理；Set-Cookie 仅直连（带身份）响应可能出现。
PASS_RESPONSE_HEADERS = {
    "content-type",
    "content-language",
    "cache-control",
    "etag",
    "last-modified",
    "set-cookie",
    "x-upstream-request-id",
    "x-notice-id",
    "x-notice-kind",
}

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Notice Cache Proxy", version="1.0.0")
store = CacheStore()
_client: httpx.AsyncClient | None = None


@dataclass
class FetchResult:
    status_code: int
    body: bytes
    headers: list[tuple[str, str]]
    request_id: str | None


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient(
        base_url=UPSTREAM_BASE,
        timeout=httpx.Timeout(HTTP_TIMEOUT),
        follow_redirects=False,
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client is not None:
        await _client.aclose()


def _is_personal(request: Request) -> bool:
    cookie = request.headers.get("cookie", "").strip()
    auth = request.headers.get("authorization", "").strip()
    return bool(cookie) or bool(auth)


def _forward_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in FORWARD_REQUEST_HEADERS and lk not in HOP_BY_HOP:
            out[k] = v
    return out


def _filter_response_headers(
    headers, *, include_set_cookie: bool
) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for k, v in headers:
        lk = k.lower()
        if lk in HOP_BY_HOP:
            continue
        if lk == "set-cookie" and not include_set_cookie:
            continue
        if lk in PASS_RESPONSE_HEADERS:
            result.append((k, v))
    return result


async def _fetch_upstream(request: Request, notice_id: str) -> FetchResult:
    assert _client is not None
    resp = await _client.get(
        f"/notices/{notice_id}", headers=_forward_headers(request)
    )
    body = resp.content  # 读取完整 body，连接即可释放
    return FetchResult(
        status_code=resp.status_code,
        body=body,
        headers=list(resp.headers.multi_items()),
        request_id=resp.headers.get("x-upstream-request-id"),
    )


def _build_cached_entry(
    result: FetchResult, generation: int
) -> CachedResponse | None:
    ok, _reason, max_age = cache_eligibility(result.status_code, result.headers)
    if not ok or max_age is None:
        return None
    safe_headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in _filter_response_headers(
            result.headers, include_set_cookie=False
        )
    ]
    now = time.monotonic()
    return CachedResponse(
        status_code=result.status_code,
        body=result.body,
        headers=safe_headers,
        cached_at=now,
        expires_at=now + max_age,
        max_age=max_age,
        generation=generation,
    )


class UpstreamFailure(Exception):
    """上游返回 5xx：视为失败，不缓存并结束所有等待者。"""

    def __init__(self, result: "FetchResult") -> None:
        super().__init__(f"upstream responded {result.status_code}")
        self.result = result


async def _leader_work(
    inflight: InFlight, request: Request, notice_id: str
) -> None:
    """独立任务执行上游请求；任何等待者（含 leader 客户端）断开都不会取消它。

    成功且可缓存：仅在仍是当前代际时回填，再交付全部等待者。
    传输异常 / 上游 5xx：不缓存，以异常结束所有等待者。
    """
    try:
        result = await _fetch_upstream(request, notice_id)
        if result.status_code >= 500:
            raise UpstreamFailure(result)
    except BaseException as exc:  # noqa: BLE001 - 需传播给全部等待者
        await store.fail_all(inflight, exc)
        if not inflight.future.done():
            inflight.future.set_exception(exc)
        return
    try:
        entry = _build_cached_entry(result, inflight.generation)
        await store.store_if_current(inflight, entry)
    finally:
        if not inflight.future.done():
            inflight.future.set_result(result)


def _live_response(
    result: FetchResult, *, cache_status: str, reason: str
) -> Response:
    # raw_headers 支持重复的 Set-Cookie；与缓存条目一样使用 latin-1 字节
    raw = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in _filter_response_headers(
            result.headers, include_set_cookie=True
        )
    ]
    raw.append((b"x-cache", cache_status.encode("latin-1")))
    raw.append((b"x-cache-reason", reason.encode("latin-1")))
    response = Response(content=result.body, status_code=result.status_code)
    response.raw_headers = raw
    return response


def _error_response(message: str, status: int = 502) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": message, "upstream": UPSTREAM_BASE},
        headers={"X-Cache": "ERROR"},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/notices/{notice_id}")
async def proxy_notice(notice_id: str, request: Request) -> Response:
    # ---- 带身份的请求：直连，不碰缓存、不参与合并 ----
    if _is_personal(request):
        try:
            result = await _fetch_upstream(request, notice_id)
        except httpx.HTTPError as exc:
            return _error_response(f"upstream error (bypass): {exc}")
        if result.status_code >= 500:
            return _error_response(
                f"upstream responded {result.status_code} (bypass)"
            )
        return _live_response(
            result, cache_status="BYPASS", reason="authenticated"
        )

    accept_language = request.headers.get("accept-language", "")
    key = store.make_key(notice_id, accept_language)
    decision = await store.lookup(key, notice_id)

    # ---- 1) 缓存命中（过期条目在 lookup 阶段已被剔除，绝不复用）----
    if isinstance(decision, CacheHit):
        entry = decision.entry
        raw = list(entry.headers)
        age = int(time.monotonic() - entry.cached_at)
        raw.append((b"x-cache", b"HIT"))
        raw.append((
            b"x-cache-key",
            f"notice={notice_id};lang={accept_language or '<none>'}".encode("latin-1"),
        ))
        raw.append((b"age", str(age).encode("latin-1")))
        response = Response(content=entry.body, status_code=entry.status_code)
        response.raw_headers = raw
        return response

    # ---- 2) 已有同键在途：加入等待，不发新的上游请求 ----
    if isinstance(decision, CacheWait):
        try:
            result = await decision.inflight.future
        except UpstreamFailure as exc:
            return _error_response(
                f"upstream responded {exc.result.status_code} (shared)"
            )
        except httpx.HTTPError as exc:
            return _error_response(f"upstream error (shared): {exc}")
        except Exception as exc:  # noqa: BLE001
            return _error_response(f"shared request failed: {exc}")
        return _live_response(
            result,
            cache_status="MISS-WAIT",
            reason="coalesced with in-flight request",
        )

    # ---- 3) leader：发起唯一的上游请求 ----
    # 独立任务 + 等待 future：leader 客户端断开只会取消路由处理任务，
    # 不会取消 leader_work，其他等待者照常得到结果。
    assert isinstance(decision, CacheLeader)
    inflight = decision.inflight
    task = asyncio.create_task(_leader_work(inflight, request, notice_id))
    inflight.leader_task = task
    try:
        result = await inflight.future
    except UpstreamFailure as exc:
        return _error_response(
            f"upstream responded {exc.result.status_code}"
        )
    except httpx.HTTPError as exc:
        return _error_response(f"upstream error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _error_response(f"upstream request failed: {exc}")

    ok, reason, _ = cache_eligibility(result.status_code, result.headers)
    return _live_response(
        result,
        cache_status="MISS" if ok else "MISS-NO-CACHE",
        reason=reason,
    )


# ---------------- 管理 / 演示接口 ----------------


def _passthrough(r: httpx.Response) -> Response:
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
    )


@app.get("/proxy/state")
async def proxy_state() -> JSONResponse:
    return JSONResponse(await store.snapshot())


@app.post("/proxy/purge/{notice_id}")
async def purge_notice(notice_id: str) -> JSONResponse:
    info = await store.purge_notice(notice_id)
    return JSONResponse({"purged": notice_id, **info})


@app.post("/proxy/reset")
async def proxy_reset() -> JSONResponse:
    return JSONResponse(await store.reset())


@app.get("/proxy/upstream/state")
async def upstream_state() -> Response:
    assert _client is not None
    try:
        return _passthrough(await _client.get("/admin/state"))
    except httpx.HTTPError as exc:
        return _error_response(f"upstream admin unreachable: {exc}")


@app.get("/proxy/upstream/requests")
async def upstream_requests() -> Response:
    assert _client is not None
    try:
        return _passthrough(await _client.get("/admin/requests"))
    except httpx.HTTPError as exc:
        return _error_response(f"upstream admin unreachable: {exc}")


@app.post("/proxy/upstream/config")
async def upstream_config(request: Request) -> Response:
    assert _client is not None
    payload = await request.body()
    try:
        r = await _client.post(
            "/admin/config",
            content=payload,
            headers={
                "content-type": request.headers.get(
                    "content-type", "application/json"
                )
            },
        )
    except httpx.HTTPError as exc:
        return _error_response(f"upstream admin unreachable: {exc}")
    return _passthrough(r)


@app.post("/proxy/upstream/reset")
async def upstream_reset() -> Response:
    assert _client is not None
    try:
        return _passthrough(await _client.post("/admin/reset"))
    except httpx.HTTPError as exc:
        return _error_response(f"upstream admin unreachable: {exc}")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
