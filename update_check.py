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
import urllib.parse
import urllib.request

import config
import settings_store

_API_ROOT = "https://api.github.com/repos/{repo}"
_TAG_IN_URL = re.compile(r"/releases/tag/([^/?#]+)")


class UpdateInfo:
    """一次检查的结果。"""

    def __init__(self, has_update=False, remote_version="", current_version="",
                 download_url="", release_name="", error=None,
                 asset_url="", asset_name="", asset_size=0,
                 delta_url="", delta_name="", delta_size=0, delta_base_version="",
                 full_url="", full_name="", full_size=0):
        self.has_update = has_update
        self.remote_version = remote_version
        self.current_version = current_version
        self.download_url = download_url
        self.release_name = release_name
        self.error = error
        # 自动更新用：Release 里上传的打包 zip（没有就退化为打开发布页）
        self.asset_url = asset_url
        self.asset_name = asset_name
        self.asset_size = int(asset_size or 0)
        # 文件级增量包（有 base 匹配时才优先使用；否则 GUI 退回 full）
        self.delta_url = delta_url
        self.delta_name = delta_name
        self.delta_size = int(delta_size or 0)
        self.delta_base_version = delta_base_version
        self.full_url = full_url
        self.full_name = full_name
        self.full_size = int(full_size or 0)

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


def _user_proxy():
    """程序内「设置」里保存的更新代理（空串=未设置）。读取失败不影响检查。"""
    try:
        return settings_store.get_update_proxy()
    except Exception:
        return ""


def resolve_proxy():
    """按"显式配置 → 环境变量 → 系统代理"的顺序返回代理地址；都没有返回 None。"""
    explicit = (getattr(config, "UPDATE_PROXY", "") or "").strip()
    if explicit:
        return explicit
    # 程序内「设置」保存的代理（本次 L3）；优先于环境变量/系统代理
    user_proxy = _user_proxy()
    if user_proxy:
        return user_proxy
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


_DELTA_FROM = re.compile(r"from[_-]?v?([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)


def delta_base_version_from_name(name):
    """从增量包文件名解析 base 版本，例如 delta_from_v0.5.2_to_v0.5.3.zip。"""
    match = _DELTA_FROM.search(str(name or ""))
    return match.group(1) if match else ""


def _asset_entry(item):
    return {
        "url": str(item.get("browser_download_url") or ""),
        "name": str(item.get("name") or "update.zip"),
        "size": int(item.get("size") or 0),
    }


def pick_update_assets(assets, current_version=""):
    """从 Release assets 中选出 full 和 delta 候选。"""
    zips = [item for item in (assets or [])
            if isinstance(item, dict)
            and str(item.get("name") or "").lower().endswith(".zip")
            and str(item.get("browser_download_url") or "")]
    delta_candidates = [item for item in zips if "delta" in str(item.get("name") or "").lower()]
    full_candidates = [item for item in zips if item not in delta_candidates]
    named_full = [item for item in full_candidates
                  if "visionflow" in str(item.get("name") or "").lower()]
    full = (named_full or full_candidates or [None])[0]
    current = parse_version(current_version)
    delta = None
    for item in delta_candidates:
        base = parse_version(delta_base_version_from_name(item.get("name")))
        if current and base and base == current:
            delta = item
            break
    return {
        "full": _asset_entry(full) if full else None,
        "delta": _asset_entry(delta) if delta else None,
    }


def pick_update_asset(assets):
    """从 GitHub Release 的 assets 里挑自动更新用的 zip。

    规则：只看 `.zip`；优先名字带 VisionFlow 的；没有就取第一个 zip。
    找不到返回 None。
    """
    zips = [item for item in (assets or [])
            if isinstance(item, dict)
            and str(item.get("name") or "").lower().endswith(".zip")
            and str(item.get("browser_download_url") or "")]
    if not zips:
        return None
    named = [item for item in zips if "visionflow" in str(item.get("name") or "").lower()]
    chosen = (named or zips)[0]
    return {
        "url": str(chosen.get("browser_download_url") or ""),
        "name": str(chosen.get("name") or "update.zip"),
        "size": int(chosen.get("size") or 0),
    }


def find_release_assets(tag, repo=None, timeout=None, proxy=None, opener=None,
                        current_version=""):
    """查询某个 tag 的 Release 资产，返回 {full, delta}（失败返回空 dict 值）。"""
    repo = repo or getattr(config, "UPDATE_REPO", "")
    timeout = timeout if timeout is not None else getattr(config, "UPDATE_CHECK_TIMEOUT", 4.0)
    proxy = resolve_proxy() if proxy is None else proxy
    opener = opener or _http_get
    tag_text = str(tag or "").strip()
    candidates = []
    if tag_text:
        candidates.append(_api_url(repo, "/releases/tags/" + urllib.parse.quote(tag_text)))
    candidates.append(_api_url(repo, "/releases/latest"))
    for url in candidates:
        try:
            _final, body = _split_response(opener(url, timeout, proxy))
            payload = json.loads(body.decode("utf-8"))
            assets = payload.get("assets") if isinstance(payload, dict) else None
            selected = pick_update_assets(assets, current_version=current_version)
            if selected.get("full") or selected.get("delta"):
                return selected
        except Exception:
            continue
    return {"full": None, "delta": None}


def find_release_asset(tag, repo=None, timeout=None, proxy=None, opener=None):
    """查询某个 tag 的 Release 资产，返回 {url,name,size} 或 None（失败不抛异常）。"""
    repo = repo or getattr(config, "UPDATE_REPO", "")
    timeout = timeout if timeout is not None else getattr(config, "UPDATE_CHECK_TIMEOUT", 4.0)
    proxy = resolve_proxy() if proxy is None else proxy
    opener = opener or _http_get
    tag_text = str(tag or "").strip()
    candidates = []
    if tag_text:
        candidates.append(_api_url(repo, "/releases/tags/" + urllib.parse.quote(tag_text)))
    candidates.append(_api_url(repo, "/releases/latest"))
    for url in candidates:
        try:
            _final, body = _split_response(opener(url, timeout, proxy))
            payload = json.loads(body.decode("utf-8"))
            asset = pick_update_asset(payload.get("assets") if isinstance(payload, dict) else None)
            if asset:
                return asset
        except Exception:
            continue
    return None


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


_DOWNLOAD_CHUNK = 1 << 20
_DOWNLOAD_TIMEOUT = 60


def download_asset(url, destination, proxy=None, timeout=_DOWNLOAD_TIMEOUT,
                   progress=None, max_bytes=None):
    """把 Release 资产下载到 destination（先写 .part，成功后原子替换）。

    progress(downloaded, total) 用于界面显示；total 未知时为 0。
    失败抛 RuntimeError（中文原因），并清理临时文件。
    """
    import os
    target = os.path.abspath(destination)
    part = target + ".part"
    proxy = resolve_proxy() if proxy is None else proxy
    request = urllib.request.Request(url, headers={
        "User-Agent": "VisionFlow-Automator-UpdateCheck",
        "Accept": "application/octet-stream",
    })
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({
            "http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    downloaded = 0
    try:
        with opener.open(request, timeout=timeout) as handle:
            total = int(handle.headers.get("Content-Length") or 0)
            if max_bytes and total and total > max_bytes:
                raise RuntimeError(f"更新包过大（{total / 1024 / 1024:.0f} MB），已取消")
            with open(part, "wb") as output:
                while True:
                    block = handle.read(_DOWNLOAD_CHUNK)
                    if not block:
                        break
                    output.write(block)
                    downloaded += len(block)
                    if max_bytes and downloaded > max_bytes:
                        raise RuntimeError("更新包超过大小限制，已取消")
                    if progress:
                        progress(downloaded, total)
        if total and downloaded != total:
            raise RuntimeError(f"下载不完整（{downloaded}/{total} 字节）")
        os.replace(part, target)
        return target
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"下载失败：{exc}") from exc
    finally:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass


def check_for_update(current=None, repo=None, timeout=None, proxy=None, opener=None):
    """检查是否有新版本，返回 UpdateInfo（永不抛异常）。"""
    current = current or getattr(config, "APP_VERSION", "")
    remote, url, name, error = fetch_latest_release(
        repo=repo, timeout=timeout, proxy=proxy, opener=opener)
    if error:
        return UpdateInfo(False, "", current, "", "", error)
    info = UpdateInfo(is_newer(remote, current), remote, current, url, name, None)
    if info.has_update:
        assets = find_release_assets(remote, repo=repo, timeout=timeout,
                                     proxy=proxy, opener=opener,
                                     current_version=current)
        full = assets.get("full")
        delta = assets.get("delta")
        if full:
            info.full_url = full["url"]
            info.full_name = full["name"]
            info.full_size = full["size"]
        if delta:
            info.delta_url = delta["url"]
            info.delta_name = delta["name"]
            info.delta_size = delta["size"]
            info.delta_base_version = delta_base_version_from_name(delta["name"])
        # 默认选择：有匹配当前版本的 delta 就用 delta，否则 full；GUI 会再结合本地 manifest 校验
        chosen = delta or full
        if chosen:
            info.asset_url = chosen["url"]
            info.asset_name = chosen["name"]
            info.asset_size = chosen["size"]
    return info
