# -*- coding: utf-8 -*-
"""更新清单与增量包：文件级 manifest / delta zip 的公共实现。

约定：
- 完整包和增量包都带 `update_manifest.json`；
- 完整包 manifest: kind=full；
- 增量包结构：`update_manifest.json` + `payload/<相对路径>`，manifest 里带 base_version/deleted；
- manifest 只记录程序文件，绝不包含 `saved_*` / `captured_*` / 更新备份等用户数据。
"""
import fnmatch
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import paths

MANIFEST_NAME = "update_manifest.json"
PAYLOAD_DIR = "payload"
KIND_FULL = "full"
KIND_DELTA = "delta"

_EXCLUDED_NAMES = tuple(paths.DATA_FILE_NAMES) + ("saved_settings.json",)
_EXCLUDED_PREFIXES = ("_backup_update_", "_data_backup_", "captured_")
_EXCLUDED_SUFFIXES = (".part",)


class ManifestError(RuntimeError):
    """manifest / 增量包格式或校验失败。"""


def is_excluded(relative_path):
    """用户数据、备份、临时文件不进入更新清单。"""
    text = str(relative_path).replace("\\", "/")
    if text == MANIFEST_NAME:
        return True
    parts = text.split("/")
    if any(part.startswith(_EXCLUDED_PREFIXES) for part in parts):
        return True
    if parts and parts[-1] in _EXCLUDED_NAMES:
        return True
    if fnmatch.fnmatch(parts[-1], "captured_*"):
        return True
    return any(text.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES)


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def iter_program_files(root):
    root = Path(root)
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(root).as_posix()
        if is_excluded(relative):
            continue
        yield relative, item


def build_manifest(program_dir, version, kind=KIND_FULL, base_version="", deleted=None):
    """扫描程序目录，生成 manifest（不含 manifest 自身和用户数据）。"""
    files = []
    for relative, path in iter_program_files(program_dir):
        files.append({
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return {
        "format": 1,
        "kind": kind,
        "version": str(version or ""),
        "base_version": str(base_version or ""),
        "deleted": list(deleted or []),
        "files": files,
    }


def write_manifest(program_dir, manifest):
    target = Path(program_dir) / MANIFEST_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".manifest_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise
    return target


def read_manifest(source):
    """从 manifest/json/目录/zip 读取 manifest。"""
    source = Path(source)
    if source.is_dir():
        source = source / MANIFEST_NAME
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            members = [name for name in archive.namelist()
                       if name.rsplit("/", 1)[-1] == MANIFEST_NAME]
            if not members:
                raise ManifestError("压缩包内没有 update_manifest.json")
            member = sorted(members, key=lambda name: name.count("/"))[0]
            raw = archive.read(member)
    else:
        raw = source.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"update_manifest.json 无法解析：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ManifestError("update_manifest.json 格式不正确")
    return payload


def load_local_manifest(install_dir):
    """读取已安装目录的 manifest；没有/损坏返回 None。"""
    path = Path(install_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        return read_manifest(path)
    except (ManifestError, OSError):
        return None


def _manifest_hashes(manifest):
    result = {}
    for item in manifest.get("files") or []:
        if isinstance(item, dict) and item.get("path"):
            result[str(item["path"])] = str(item.get("sha256") or "")
    return result


def compare_manifests(previous, current):
    """返回 (changed_files, deleted_files)。

    changed_files: 新增/内容变化/大小变化的相对路径列表；
    deleted_files: 上一版有、这一版没有的相对路径列表。
    """
    previous_hashes = _manifest_hashes(previous)
    current_hashes = _manifest_hashes(current)
    changed = [path for path, digest in current_hashes.items()
               if previous_hashes.get(path) != digest]
    deleted = [path for path in previous_hashes if path not in current_hashes]
    return sorted(changed), sorted(deleted)


def make_delta_zip(program_dir, current_manifest, previous_manifest, output_path):
    """只把变化文件放进增量包；返回 (zip 路径, changed, deleted, 大小)。"""
    changed, deleted = compare_manifests(previous_manifest, current_manifest)
    program_dir = Path(program_dir)
    staging_root = Path(tempfile.mkdtemp(prefix="vf_delta_"))
    try:
        # 增量包根目录直接放 manifest + payload，客户端解压后无需再找一层
        staging = staging_root
        payload = staging / PAYLOAD_DIR
        delta_manifest = dict(current_manifest)
        delta_manifest.update({
            "kind": KIND_DELTA,
            "base_version": str(previous_manifest.get("version") or ""),
            "deleted": deleted,
            "changed": changed,
        })
        write_manifest(staging, delta_manifest)
        for relative in changed:
            source = program_dir / relative
            if not source.is_file():
                raise ManifestError(f"增量文件缺失：{relative}")
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        archive = shutil.make_archive(str(output.with_suffix("")), "zip",
                                      root_dir=str(staging_root), base_dir=".")
        return Path(archive), changed, deleted, Path(archive).stat().st_size
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _safe_relative(relative):
    text = str(relative or "").replace("\\", "/")
    if not text or text.startswith("/") or ".." in text.split("/"):
        raise ManifestError(f"非法文件路径：{relative!r}")
    return text


def verify_payload(staged_root, manifest):
    """校验增量包 payload 中每个变化文件的 SHA256。返回错误列表。"""
    root = Path(staged_root) / PAYLOAD_DIR
    errors = []
    hashes = _manifest_hashes(manifest)
    for relative in manifest.get("changed") or []:
        relative = _safe_relative(relative)
        path = root / relative
        if not path.is_file():
            errors.append(f"增量包缺文件：{relative}")
            continue
        if hashes.get(relative) and sha256_file(path) != hashes[relative]:
            errors.append(f"增量文件校验失败：{relative}")
    return errors


def verify_installed(install_dir, manifest):
    """校验安装目录是否与 manifest 一致（只校验清单内文件）。"""
    root = Path(install_dir)
    errors = []
    for item in manifest.get("files") or []:
        relative = _safe_relative(item.get("path"))
        path = root / relative
        if not path.is_file():
            errors.append(f"缺少文件：{relative}")
        elif sha256_file(path) != str(item.get("sha256") or ""):
            errors.append(f"文件内容不一致：{relative}")
    return errors
