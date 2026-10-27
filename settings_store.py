# -*- coding: utf-8 -*-
"""用户级设置（持久化在数据目录 saved_settings.json）。

目前只有「检查更新的代理地址」：留空 = 自动探测（环境变量 / Windows 系统代理 / 直连）。
写入沿用与 saved_presets.json 相同的"同目录临时文件 + os.replace"原子写法。
"""
import json
import os
import tempfile

import paths

SETTINGS_FILE = os.path.join(paths.data_dir(), "saved_settings.json")


def load_settings():
    """读取设置；文件不存在/损坏时返回空 dict（不影响启动）。"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data):
    """原子写入设置文件。"""
    directory = os.path.dirname(SETTINGS_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, SETTINGS_FILE)
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        finally:
            raise


def get_update_proxy():
    """返回用户设置的更新代理（已 strip；空串表示自动探测）。"""
    return str(load_settings().get("update_proxy") or "").strip()


def set_update_proxy(value):
    """保存更新代理；空串表示恢复自动探测。"""
    data = load_settings()
    data["update_proxy"] = str(value or "").strip()
    save_settings(data)
