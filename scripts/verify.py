#!/usr/bin/env python3
"""端到端行为验证（stdlib 实现）。

前置：代理在 :8000、上游在 :8001 已启动（scripts/run-local.sh 或 compose）。
覆盖：
  1. 匿名 MISS -> HIT，且上游只收到一次请求
  2. 缓存键按 Accept-Language 区分
  3. Cookie / Authorization 直连绕过缓存、不参与合并
  4. personal（Set-Cookie + private,no-store）不缓存
  5. 500 失败不缓存且结束所有在途等待
  6. 同键并发未命中只打一次上游；等待者断开不影响其他人
  7. 过期不复用
  8. 按公告清除全部语言；旧在途不回填；清除后不复用旧在途
  9. 各种非法 Cache-Control 不缓存
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROXY = os.environ.get("PROXY_ADDR", "127.0.0.1:8000")
UPSTREAM = os.environ.get("UPSTREAM_ADDR", "127.0.0.1:8001")

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def raw_request(host_port: str, method: str, path: str, headers=None, timeout=20):
    conn = http.client.HTTPConnection(host_port, timeout=timeout)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    hs = {k.lower(): v for k, v in resp.getheaders()}
    return resp.status, hs, body, conn


def proxy_get(path, headers=None, timeout=20):
    status, hs, body, conn = raw_request(PROXY, "GET", path, headers, timeout)
    conn.close()
    return status, hs, body


def admin_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://{UPSTREAM}{path}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _post(url: str) -> bytes:
    req = urllib.request.Request(url, data=b"", method="POST")
    return urllib.request.urlopen(req, timeout=10).read()


def admin_reset() -> None:
    _post(f"http://{UPSTREAM}/admin/reset")
    _post(f"http://{PROXY}/proxy/reset")


def upstream_count() -> int:
    with urllib.request.urlopen(
        f"http://{UPSTREAM}/admin/state", timeout=10
    ) as r:
        return json.loads(r.read())["counters"]["total_requests"]


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def main() -> int:
    section("0. 重置环境")
    admin_reset()
    admin_post("/admin/config",
               {"profile": "public", "latency_ms": 100, "max_age": 30})
    check("healthz", proxy_get("/healthz")[0] == 200)

    section("1. 匿名 MISS -> HIT，上游仅一次请求")
    n0 = upstream_count()
    s1, h1, b1 = proxy_get("/notices/1", {"Accept-Language": "zh-CN"})
    s2, h2, _ = proxy_get("/notices/1", {"Accept-Language": "zh-CN"})
    check("首次 MISS", h1.get("x-cache") == "MISS", h1.get("x-cache"))
    check("二次 HIT", h2.get("x-cache") == "HIT", h2.get("x-cache"))
    check("两次仅一次上游请求", upstream_count() - n0 == 1,
          f"delta={upstream_count() - n0}")
    check("HIT 响应 Age 头存在", h2.get("age") is not None)

    section("2. 缓存键按 Accept-Language 区分")
    n0 = upstream_count()
    s3, h3, b3 = proxy_get("/notices/1", {"Accept-Language": "en-US"})
    check("新语言 MISS", h3.get("x-cache") == "MISS", h3.get("x-cache"))
    zh = json.loads(b1)["language"]
    en = json.loads(b3)["language"]
    check("语言内容不同", zh != en, f"{zh} vs {en}")
    s4, h4, _ = proxy_get("/notices/1", {"Accept-Language": "en-US"})
    check("新语言二次 HIT", h4.get("x-cache") == "HIT")
    check("两种语言各打一次上游", upstream_count() - n0 == 1)

    section("3. Cookie / Authorization 直连，绕过缓存且不污染缓存")
    n0 = upstream_count()
    s5, h5, _ = proxy_get("/notices/1",
                          {"Accept-Language": "zh-CN", "Cookie": "SID=u1"})
    check("Cookie 请求 BYPASS", h5.get("x-cache") == "BYPASS", h5.get("x-cache"))
    s6, h6, _ = proxy_get(
        "/notices/1",
        {"Accept-Language": "zh-CN", "Authorization": "Bearer t"},
    )
    check("Auth 请求 BYPASS", h6.get("x-cache") == "BYPASS", h6.get("x-cache"))
    check("两次绕过都打了上游", upstream_count() - n0 == 2)
    s7, h7, _ = proxy_get("/notices/1", {"Accept-Language": "zh-CN"})
    check("绕过后匿名仍 HIT（缓存未被污染）", h7.get("x-cache") == "HIT",
          h7.get("x-cache"))

    section("4. personal 响应（Set-Cookie + private,no-store）不缓存")
    admin_post("/admin/config", {"notice_id": "2", "profile": "personal"})
    n0 = upstream_count()
    s8, h8, _ = proxy_get("/notices/2", {"Accept-Language": "zh-CN"})
    check("personal 首次 MISS-NO-CACHE",
          h8.get("x-cache") == "MISS-NO-CACHE", h8.get("x-cache"))
    check("personal 响应带回 Set-Cookie", "set-cookie" in h8)
    s9, h9, _ = proxy_get("/notices/2", {"Accept-Language": "zh-CN"})
    check("personal 再次仍是 MISS-NO-CACHE",
          h9.get("x-cache") == "MISS-NO-CACHE", h9.get("x-cache"))
    check("每次都打上游（无缓存）", upstream_count() - n0 == 2)

    section("5. 上游 500：不缓存，所有并发等待者都收到失败")
    admin_post("/admin/config", {"notice_id": "3", "failure": "error500",
                                 "latency_ms": 300})
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(
            proxy_get("/notices/3", {"Accept-Language": "zh-CN"})))
        for _ in range(4)
    ]
    for t in threads: t.start()
    for t in threads: t.join()
    check("4 个并发全部 502", all(r[0] == 502 for r in results),
          str([r[0] for r in results]))
    check("全部标记 ERROR", all(r[1].get("x-cache") == "ERROR" for r in results))
    s10, h10, _ = proxy_get("/notices/3", {"Accept-Language": "zh-CN"})
    check("失败后再次请求仍重新打上游（未缓存失败）",
          h10.get("x-cache") == "ERROR")
    admin_post("/admin/config", {"notice_id": "3", "failure": "none",
                                 "latency_ms": 100})

    section("6. 同键并发未命中只请求上游一次")
    admin_post("/admin/config", {"notice_id": "1", "latency_ms": 1500,
                                 "profile": "public", "max_age": 30})
    urllib.request.urlopen(urllib.request.Request(
        f"http://{PROXY}/proxy/purge/1", data=b"", method="POST"), timeout=10).read()
    n0 = upstream_count()
    conc: list = []
    threads = [
        threading.Thread(target=lambda: conc.append(
            proxy_get("/notices/1", {"Accept-Language": "fr-FR"}, timeout=20)))
        for _ in range(6)
    ]
    t0 = time.monotonic()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.monotonic() - t0
    tags = [r[1].get("x-cache") for r in conc]
    check("6 个请求全部 200", all(r[0] == 200 for r in conc))
    check("恰好 1 个 MISS + 5 个 MISS-WAIT",
          sorted(tags).count("MISS") == 1 and tags.count("MISS-WAIT") == 5,
          str(tags))
    check("上游只收到 1 次请求", upstream_count() - n0 == 1,
          f"delta={upstream_count() - n0}")
    check("总耗时≈单次延迟而非 6 倍", elapsed < 3.5, f"{elapsed:.2f}s")
    rids = {r[1].get("x-upstream-request-id") for r in conc}
    check("所有等待者拿到同一上游响应", len(rids) == 1, str(rids))

    section("7. 等待者断开不影响其他等待者")
    admin_post("/admin/config", {"notice_id": "1", "latency_ms": 2000})
    urllib.request.urlopen(urllib.request.Request(
        f"http://{PROXY}/proxy/purge/1", data=b"", method="POST"), timeout=10).read()
    got: list = []

    def slow_waiter():
        try:
            got.append(proxy_get("/notices/1",
                                 {"Accept-Language": "de-DE"}, timeout=20))
        except Exception as e:  # noqa: BLE001
            got.append(("ERR", {}, str(e).encode()))

    def disconnecting_waiter():
        # 极小超时，在 2s 上游延迟内主动断开
        try:
            proxy_get("/notices/1", {"Accept-Language": "de-DE"}, timeout=0.4)
        except Exception:
            pass

    t_a = threading.Thread(target=slow_waiter)
    t_b = threading.Thread(target=disconnecting_waiter)
    t_a.start(); time.sleep(0.15); t_b.start()
    t_b.join(); t_a.join()
    check("断开发生后其他等待者仍拿到 200",
          len(got) == 1 and got[0][0] == 200, str(got[:1]))
    n0 = upstream_count()
    _, h_after, _ = proxy_get("/notices/1", {"Accept-Language": "de-DE"})
    check("leader 结果已正常回填（后续 HIT）",
          h_after.get("x-cache") == "HIT", h_after.get("x-cache"))
    check("回填验证未再打上游", upstream_count() - n0 == 0)

    section("8. 过期不复用")
    admin_post("/admin/config", {"notice_id": "1", "latency_ms": 50,
                                 "profile": "public", "max_age": 2})
    urllib.request.urlopen(urllib.request.Request(
        f"http://{PROXY}/proxy/purge/1", data=b"", method="POST"), timeout=10).read()
    _, h_a, _ = proxy_get("/notices/1", {"Accept-Language": "it-IT"})
    check("短 max-age 首次 MISS", h_a.get("x-cache") == "MISS")
    _, h_b, _ = proxy_get("/notices/1", {"Accept-Language": "it-IT"})
    check("未过期 HIT", h_b.get("x-cache") == "HIT", h_b.get("x-cache"))
    time.sleep(2.3)
    n0 = upstream_count()
    _, h_c, _ = proxy_get("/notices/1", {"Accept-Language": "it-IT"})
    check("过期后重新 MISS（不复用）", h_c.get("x-cache") == "MISS",
          h_c.get("x-cache"))
    check("过期请求确实打了上游", upstream_count() - n0 == 1)

    section("9. 按公告清除：全语言失效 + 旧在途不回填 + 不复用旧在途")
    admin_post("/admin/config", {"notice_id": "1", "latency_ms": 2500,
                                 "profile": "public", "max_age": 30})
    # 先造两种语言缓存
    proxy_get("/notices/1", {"Accept-Language": "zh-CN"})
    proxy_get("/notices/1", {"Accept-Language": "en-US"})
    st = json.loads(proxy_get("/proxy/state")[2])
    cached_langs = {c["accept_language"] for c in st["cache"]
                    if c["notice_id"] == "1"}
    check("清除前两种语言均已缓存",
          {"zh-CN", "en-US"}.issubset(cached_langs), str(cached_langs))
    # 发起 3 个同键在途请求（新语言 ja），随后立即清除
    box: list = []
    threads = [
        threading.Thread(target=lambda: box.append(
            proxy_get("/notices/1", {"Accept-Language": "ja-JP"}, timeout=20)))
        for _ in range(3)
    ]
    for t in threads: t.start()
    time.sleep(0.4)
    st_mid = json.loads(proxy_get("/proxy/state")[2])
    check("清除前存在 1 个在途单元",
          any(i["notice_id"] == "1" for i in st_mid["inflight"]))
    purge = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"http://{PROXY}/proxy/purge/1", data=b"", method="POST"),
        timeout=10).read())
    check("清除接口报告全语言缓存条目删除",
          purge["purged_cache_entries"] >= 2, str(purge))
    check("清除接口标记了在途单元", purge["marked_inflight"] >= 1, str(purge))
    for t in threads: t.join()
    check("在途等待者均拿到当时响应（200）",
          all(r[0] == 200 for r in box), str([r[0] for r in box]))
    time.sleep(0.2)
    st_after = json.loads(proxy_get("/proxy/state")[2])
    check("旧在途完成后未回填缓存",
          all(c["notice_id"] != "1" for c in st_after["cache"]),
          str(st_after["cache"]))
    n0 = upstream_count()
    _, h_new, _ = proxy_get("/notices/1", {"Accept-Language": "ja-JP"})
    check("清除后新请求重新 MISS（不复用旧在途）",
          h_new.get("x-cache") == "MISS", h_new.get("x-cache"))
    check("新请求确实重新打了上游", upstream_count() - n0 == 1)
    _, h_zh, _ = proxy_get("/notices/1", {"Accept-Language": "zh-CN"})
    check("旧语言缓存同样已失效", h_zh.get("x-cache") == "MISS",
          h_zh.get("x-cache"))

    section("10. 非法 Cache-Control 不缓存（经上游自定义头）")
    bad_headers = [
        "max-age=30",                       # 无 public
        "public",                           # 无 max-age
        "public, max-age=-5",               # 负数
        "public, max-age=1.5",              # 小数
        "public, max-age=abc",              # 非数字
        "public, max-age=30, no-store",     # no-store
        "public, max-age=30, private",      # private
        "public, max-age=",                 # 空值
    ]
    for i, cc in enumerate(bad_headers):
        nid = str(100 + i)
        admin_post("/admin/config",
                   {"notice_id": nid, "cache_control": cc, "latency_ms": 0})
        _, hh1, _ = proxy_get(f"/notices/{nid}", {"Accept-Language": "zh-CN"})
        _, hh2, _ = proxy_get(f"/notices/{nid}", {"Accept-Language": "zh-CN"})
        check(f"不缓存 [{cc}]",
              hh1.get("x-cache") == "MISS-NO-CACHE"
              and hh2.get("x-cache") == "MISS-NO-CACHE",
              f"{hh1.get('x-cache')} / {hh2.get('x-cache')}")
    # 404 也不可缓存
    _, h404, _ = proxy_get("/notices/404-missing",
                           {"Accept-Language": "zh-CN"})
    check("404 不缓存", h404.get("x-cache") == "MISS-NO-CACHE",
          h404.get("x-cache"))

    print(f"\n========== 结果：{PASS} 通过 / {FAIL} 失败 ==========")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
