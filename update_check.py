# -*- coding: utf-8 -*-
"""自动更新检查（纯逻辑，不依赖 Qt）。

做什么
------
启动后在**后台线程**里查一次 GitHub 最新版本，和本地版本号比较；只做"提示"，
不自动下载、不自动替换文件（替换 exe 涉及权限与自解压，风险远大于收益）。

为什么优先用网页重定向而不是 API
--------------------------------
GitHub REST API 对未认证请求按 IP 限流（60 次/小时）。国内用户常走共享代理，
实测 `api.github.com/repos/.../releases/latest` 直接返回 **403**（限额被同 IP 用光）。
而 `https://github.com/<repo>/releases/latest` 是普通网页跳转：最终 URL 形如
`.../releases/tag/v0.4.1`，不消耗 API 限额。所以顺序是：

    ① 网页重定向（首选，无额度限制）
    ② API `/releases/latest`（网页失败时）
    ③ API `/tags` 取版本号最大的 tag（仓库还没发 Release 时）

任何失败（超时 / DNS / 403 / 404 / JSON 损坏）都**不抛异常**，只把原因放进结果里，
由界面决定是提示还是静默记日志。

代理探测顺序：`config.UPDATE_PROXY` → 环境变量 → Windows 系统代理（注册表）→ 直连。
"""
import json
import os
import re
import urllib.error
import urllib.request

import config

_API_ROOT = "https://api.github.com/repos/{repo}"
_TAG_IN_URL = re.compile(r"/releases/tag/([^/?#]+)")


class UpdateInfo:
    """一次检查的结果。"""

    def __init__(self, has_update=False, remote_version="", current_version="",
                 download_url="", release_name="", error=None):
        self.has_update = has_update
        self.remote_version = remote_version
        self.current_version = current_version
        self.download_url = download_url
        self.release_name = release_name
        self.error = error

    def __repr__(self):
        return (f"UpdateInfo(has_update={self.has_update}, remote={self.remote_version!r}, "
                f"current={self.current_version!r}, error={self.error!r})")


def parse_version(text):
    """把 'v0.4.1' / '0.3.1' / 'V1.2.3-rc1' 解析成 (0, 4, 1)。

    取不出数字时返回空元组（调用方据此视为"无法比较"）。
    """
    if text is None:
        return ()
    numbers = re.findall(r"\d+", str(text))
    if not numbers:
        return ()
    return tuple(int(value) for value in numbers)


def is_newer(remote_version, current_version):
    """远端版本是否比本地新；任一侧解析失败都返回 False（宁可不提示，也不要误报）。"""
    remote = parse_version(remote_version)
    current = parse_version(current_version)
    if not remote or not current:
        return False
    length = max(len(remote), len(current))
    remote = remote + (0,) * (length - len(remote))
    current = current + (0,) * (length - len(current))
    return remote > current


# ---------------------------------------------------------------------------
# 代理
# ---------------------------------------------------------------------------

def _registry_proxy():
    """读取 Windows 系统代理（Internet 选项）；非 Windows 或读取失败返回 None。"""
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None
    if not enabled or not server:
        return None
    server = str(server).strip()
    if not server:
        return None
    # 形如 "http=127.0.0.1:7892;https=127.0.0.1:7892" 时取 https 那一段
    if "=" in server:
        parts = dict(item.split("=", 1) for item in server.split(";") if "=" in item)
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return None
    return server if "://" in server else "http://" + server


def resolve_proxy():
    """按"显式配置 → 环境变量 → 系统代理"的顺序返回代理地址；都没有返回 None。"""
    explicit = (getattr(config, "UPDATE_PROXY", "") or "").strip()
    if explicit:
        return explicit
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if value:
            return value
    return _registry_proxy()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http_get(url, timeout, proxy=None):
    """GET 并返回 (最终 URL, 响应体 bytes)；单独提出来方便测试替换。"""
    request = urllib.request.Request(url, headers={
        "User-Agent": "VisionFlow-Automator-UpdateCheck",
        "Accept": "application/vnd.github+json",
    })
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": proxy,
            "https": proxy,
        }))
        handle = opener.open(request, timeout=timeout)
    else:
        handle = urllib.request.urlopen(request, timeout=timeout)
    try:
        return handle.geturl(), handle.read()
    finally:
        handle.close()


def _split_response(raw):
    """把替换用的 opener 返回值规整成 (最终 URL, bytes)。

    允许 opener 返回 `(url, bytes)`、纯 bytes、str，或带 `.read()` 的响应对象——
    测试里怎么方便怎么造，边界都在这里收口。
    """
    if isinstance(raw, tuple) and len(raw) == 2:
        url, body = raw
    else:
        url, body = "", raw
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, str):
        body = body.encode("utf-8")
    return str(url or ""), body or b""


def _tag_from_redirect(final_url):
    match = _TAG_IN_URL.search(final_url or "")
    return match.group(1) if match else ""


def _http_error_message(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403:
            return "HTTP 403（GitHub 接口限流，稍后再试或改用代理）"
        return f"HTTP {exc.code}"
    return f"检查更新失败：{exc}"


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _api_url(repo, suffix):
    return _API_ROOT.format(repo=repo) + suffix


def fetch_latest_release(repo=None, timeout=None, proxy=None, opener=None):
    """查询最新版本，返回 (版本号, 下载/发布页 URL, 名称, 错误)。

    成功时错误为 None；失败时版本号为 ""，错误为可读中文原因。
    """
    repo = repo or getattr(config, "UPDATE_REPO", "")
    timeout = timeout if timeout is not None else getattr(config, "UPDATE_CHECK_TIMEOUT", 4.0)
    proxy = resolve_proxy() if proxy is None else proxy
    opener = opener or _http_get
    releases_page = f"https://github.com/{repo}/releases"

    # ① 网页重定向：不消耗 API 配额
    try:
        final_url, _body = _split_response(opener(releases_page + "/latest", timeout, proxy))
        tag = _tag_from_redirect(final_url)
        if tag:
            return tag, final_url, tag, None
    except Exception as first_error:      # 交给后续 API 兜底
        redirect_error = _http_error_message(first_error)
    else:
        redirect_error = "网页未给出跳转目标"

    # ② API：最新 Release
    try:
        final_url, body = _split_response(
            opener(_api_url(repo, "/releases/latest"), timeout, proxy))
        payload = json.loads(body.decode("utf-8"))
        tag = str(payload.get("tag_name") or "").strip()
        if tag:
            return (tag,
                    str(payload.get("html_url") or releases_page),
                    str(payload.get("name") or tag),
                    None)
        api_error = "Release 数据里没有版本号"
    except Exception as exc:
        api_error = _http_error_message(exc)

    # ③ API：tags 里挑版本号最大的（仓库还没发 Release 时）
    try:
        final_url, body = _split_response(
            opener(_api_url(repo, "/tags?per_page=100"), timeout, proxy))
        payload = json.loads(body.decode("utf-8"))
        best_tag, best_version = "", ()
        for item in payload if isinstance(payload, list) else []:
            tag = str(item.get("name") or "").strip()
            version = parse_version(tag)
            if version and (not best_version or version > best_version):
                best_tag, best_version = tag, version
        if best_tag:
            return best_tag, releases_page, best_tag, None
        return "", releases_page, "", f"未找到版本信息（{redirect_error} / {api_error} / 仓库无 tag）"
    except Exception as exc:
        return "", releases_page, "", (
            f"未能获取版本信息（{redirect_error} / {api_error} / {_http_error_message(exc)}）")


def check_for_update(current=None, repo=None, timeout=None, proxy=None, opener=None):
    """检查是否有新版本，返回 UpdateInfo（永不抛异常）。"""
    current = current or getattr(config, "APP_VERSION", "")
    remote, url, name, error = fetch_latest_release(
        repo=repo, timeout=timeout, proxy=proxy, opener=opener)
    if error:
        return UpdateInfo(False, "", current, "", "", error)
    return UpdateInfo(is_newer(remote, current), remote, current, url, name, None)
