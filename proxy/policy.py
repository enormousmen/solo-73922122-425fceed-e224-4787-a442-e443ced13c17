"""匿名响应是否允许缓存的判定规则。

仅当以下条件全部满足时才可缓存：
1. 状态码为 200；
2. 存在 Cache-Control 响应头，其中含 public；
3. 不含 private、no-store；
4. 含 max-age，且值为非负整数（拒绝负数、小数、空值、多值等非法写法）；
5. 响应没有 Set-Cookie。

过期时间严格按 max-age 计算，过期不复用。
"""

from __future__ import annotations

import re

_MAX_AGE_RE = re.compile(r"^\d+$")


def parse_cache_control(
    header_value: str | None,
) -> dict[str, str | bool | None]:
    """解析 Cache-Control 为指令字典。

    - 无值指令存 True；带值指令存小写键对应的字符串值；
    - 去掉数值两端的引号，如 max-age="60"；
    - 带值指令（如 max-age）重复出现时标记为 None，按 RFC 7234 视为
      非法缓存指令，从而拒绝缓存；无值指令重复无副作用。
    """
    directives: dict[str, str | bool | None] = {}
    if not header_value:
        return directives
    for raw in header_value.split(","):
        token = raw.strip()
        if not token:
            continue
        if "=" in token:
            name, _, value = token.partition("=")
            name = name.strip().lower()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            if name in directives:
                directives[name] = None  # 重复的带值指令 -> 非法
            else:
                directives[name] = value
        else:
            directives[token.lower()] = True
    return directives


def cache_eligibility(
    status_code: int,
    headers,
) -> tuple[bool, str, int | None]:
    """返回 (是否可缓存, 原因说明, max_age)。

    headers 支持 httpx.Headers / 映射 / [(name, value)] 等可迭代键值形式，
    名称匹配大小写不敏感。
    """
    # 保留同名头的多个值（如多个 Cache-Control），不能用 dict 折叠
    lower = [(str(k).lower(), str(v)) for k, v in headers]

    if status_code != 200:
        return False, f"status {status_code} is not 200", None

    if any(k == "set-cookie" for k, _ in lower):
        return False, "response contains Set-Cookie", None

    cc_values = [v for k, v in lower if k == "cache-control"]
    if not cc_values:
        return False, "missing Cache-Control header", None
    # 多个 Cache-Control 头按 RFC 合并为逗号列表统一解析
    directives = parse_cache_control(", ".join(cc_values))

    if directives.get("no-store") is not None:
        return False, "Cache-Control contains no-store", None
    if directives.get("private") is not None:
        return False, "Cache-Control contains private", None
    if "public" not in directives:
        return False, "Cache-Control lacks public", None

    max_age_raw = directives.get("max-age")
    # None: 重复的 max-age（非法）；True: 裸 max-age 无值
    if max_age_raw is None or max_age_raw is True:
        return False, "Cache-Control lacks a valid max-age", None
    if not _MAX_AGE_RE.match(str(max_age_raw)):
        return False, f"invalid max-age: {max_age_raw!r}", None
    max_age = int(max_age_raw)  # 正则已保证非负整数
    return True, f"public, max-age={max_age}", max_age
