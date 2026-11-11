# 内网门户公告 · 单进程 HTTP 缓存代理

FastAPI + httpx 实现的单进程 HTTP 缓存代理与配套演示环境。代理只服务
`GET /notices/{id}` 一个业务接口，上游固定为随环境启动的本地上游服务。

## 目录结构

```
.
├── proxy/
│   ├── app.py            # 代理主应用：路由、转发、合并执行、管理接口
│   ├── cachestore.py     # 内存缓存 + 同键在途合并 + 代际（epoch）清除
│   ├── policy.py         # Cache-Control 解析与缓存资格判定
│   └── static/index.html # 演示页（浏览器入口，原生 JS，无外链依赖）
├── upstream/app.py       # 本地上游：公开/个人切换、延迟、500 失败、请求审计
├── scripts/
│   ├── run-local.sh      # 无 Docker 时本地启动（uvicorn 双进程）
│   ├── stop-local.sh
│   ├── verify.py         # 端到端行为验证（50 项断言，仅用 stdlib）
│   └── test_units.py     # 单元测试（策略解析 + 存储/代际逻辑，17 项）
├── Dockerfile            # 代理与上游共用同一镜像
├── docker-compose.yml    # 一键环境（代理 :8000，上游 :8001）
└── requirements.txt
```

## 快速启动

### 方式一：Docker Compose（推荐交付形态）

```bash
docker compose up --build
# 浏览器入口：http://localhost:8000/
```

- `proxy`：对外服务，端口 `8000`，上游固定指向 `http://upstream:8001`；
- `upstream`：模拟上游，端口 `8001`，仅在演示网络中使用；
- 两个服务均为单 uvicorn 进程、内存态，重启即复位；均带 `/healthz` 健康检查。

### 方式二：本地直接运行（无 Docker）

```bash
pip install -r requirements.txt
scripts/run-local.sh            # 默认 :8000 代理 / :8001 上游
# 浏览器入口：http://127.0.0.1:8000/
scripts/stop-local.sh           # 停止
```

### 验证

```bash
# 单元测试（无需启动服务）
python3 -I scripts/test_units.py

# 端到端（先启动服务，端口可用 PROXY_ADDR / UPSTREAM_ADDR 覆盖）
PROXY_ADDR=127.0.0.1:8000 UPSTREAM_ADDR=127.0.0.1:8001 \
  python3 -I scripts/verify.py
```

## 行为规则与实现位置

| 需求 | 实现 |
| --- | --- |
| 仅代理 `GET /notices/{id}`，上游固定 | `proxy/app.py` 单一路由；`UPSTREAM_BASE` 环境变量，compose 内固定为 `http://upstream:8001`；其他路径 404、非 GET 405 |
| 缓存按公告 ID + Accept-Language 区分 | `CacheStore.make_key`，语言取请求头原始值，缺失记为 `<none>` |
| 带 Cookie / Authorization 直连，不读写缓存、不参与合并 | `proxy_notice` 入口 `_is_personal` 判定，响应头标记 `X-Cache: BYPASS` |
| 仅 200 可缓存 | `policy.cache_eligibility` |
| 必须含 `public`，含 `private`/`no-store` 即拒绝 | 同上，逐指令大小写不敏感解析 |
| 必须含合法非负整数 `max-age` | 正则 `^\d+$`；负数、小数、空值、裸指令、重复 max-age 一律拒绝 |
| 有 `Set-Cookie` 不缓存 | 资格判定硬性拒绝；缓存条目写入前再过滤一次 Set-Cookie |
| 过期不复用 | `CachedResponse.expires_at` 单调时钟判定；`lookup` 命中过期条目立即剔除并走上游 |
| 同键并发未命中只请求上游一次 | `CacheStore.lookup` 返回 leader/wait；wait 等待同一个 `asyncio.Future` |
| 一个等待者断开不影响其他人 | 上游请求在独立 `asyncio.Task`（`_leader_work`）中执行；路由任务被取消不会取消它，future 照常回填 |
| 失败不缓存且结束所有等待 | 传输异常与上游 5xx 均走 `fail_all`：移除在途单元、future 以异常结束，所有等待者收到 502 `X-Cache: ERROR` |
| 按公告清除全部语言缓存 | `POST /proxy/purge/{id}` 删除该公告所有语言条目 |
| 清除后旧在途不得回填 | 每个公告维护 generation（epoch），清除即 +1 并把存活在途标记 `purged`；`store_if_current` 代际不符/已标记则拒绝写入 |
| 清除后不得复用旧在途结果 | 新请求 `lookup` 看到新一代际，创建全新在途单元并重新请求上游 |

### 响应头观测口径

- `X-Cache`：`HIT` / `MISS`（未命中且已缓存）/ `MISS-NO-CACHE`（未命中但不可缓存）
  / `MISS-WAIT`（合并到他人在途请求）/ `BYPASS`（带身份直连）/ `ERROR`（上游失败）；
- `X-Cache-Reason`：不可缓存或绕过的具体原因；
- `X-Cache-Key`、`Age`：仅 HIT 时返回；
- `X-Upstream-Request-Id`：透传上游请求 ID，同一次合并的所有等待者该值相同。

## 演示页使用指引（http://localhost:8000/）

1. **发起请求**：选择公告 ID、语言（Accept-Language）与演示身份（匿名 / Cookie /
   Authorization / 两者），可一键发起 6 个同键并发请求验证合并；
2. **按公告清除**：输入公告 ID 清除其全部语言缓存；“清除中并发演示”会自动制造
   2.5s 在途延迟并在在途期间清除，可观察 `purged` 标记、无回填、新请求重新打上游；
3. **上游控制**：切换 public/personal、人工延迟、500 失败、max-age，可只对单个公告
   生效；四个预设按钮覆盖典型场景；
4. **代理内部状态**：实时展示缓存条目（含剩余寿命、代际）、在途合并单元、generation；
5. **上游请求审计**：展示上游实际收到的每一个请求及 Cookie/Auth 携带情况，用于核对
   “HIT 不打上游、合并只打一次、绕过每次都打”。

### 推荐手工验证路径

- 匿名连续请求同一公告同一语言：第一次 `MISS`，之后 `HIT`，上游审计只增 1 条；
- 切换语言：各自独立缓存；
- 选择“携带 Cookie”：始终 `BYPASS`，上游审计每次都新增，且之后匿名请求仍 `HIT`；
- 上游切 personal：匿名也返回 `MISS-NO-CACHE`，原因是含 Set-Cookie 且 `private,no-store`；
- 上游切 500：并发 6 个请求全部 502/`ERROR`，恢复后重新请求正常，失败从未被缓存；
- 高延迟下并发 6 请求：仅 1 个 `MISS`、5 个 `MISS-WAIT`，总耗时≈一次上游延迟；
- 在途清除演示：旧等待者拿到当时响应，但缓存不回填；清除后的新请求是全新 `MISS`。

## 设计说明与边界

- 单进程内存缓存，不做跨进程共享；进程重启缓存即清空，符合运维缓存定位；
- 缓存只保存白名单响应头并剔除逐跳头与 `Set-Cookie`，body 完整缓冲后再释放上游连接；
- 上游 5xx 按失败处理（502 交给客户端、不缓存、结束所有等待）；4xx（如 404）为成功
  送达的响应，因非 200 不可缓存，标记 `MISS-NO-CACHE`；
- 演示管理接口（`/proxy/*`、上游 `/admin/*`）仅用于内网演示环境，不应暴露到公网。
