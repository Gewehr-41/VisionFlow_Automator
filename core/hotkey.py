# 全局快捷键：通过 Windows RegisterHotKey 在后台监听组合键，用于脚本抢占
# 键盘/鼠标时也能安全地停止脚本。仅使用标准库 ctypes，无需第三方依赖。
import ctypes
import ctypes.wintypes
import threading
import time

from diagnostics import warn

user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_REMOVE = 0x0001

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

_VK_SPECIAL = {
    "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "enter": 0x0D, "return": 0x0D,
    "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pgup": 0x21, "pageup": 0x21,
    "pgdn": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def _parse_vk(name):
    if name in _VK_SPECIAL:
        return _VK_SPECIAL[name]
    if len(name) == 1:
        char = name.upper()
        if "A" <= char <= "Z":
            return ord(char)
        if "0" <= char <= "9":
            return ord(char)
    return None


def parse_hotkey(combo):
    """把 'ctrl+alt+f9' 解析为 (modifiers, vk)。"""
    parts = [part.strip().lower() for part in str(combo).split("+") if part.strip()]
    if not parts:
        raise ValueError("热键不能为空")
    modifiers = 0
    vk = None
    for part in parts:
        if part in ("ctrl", "control"):
            modifiers |= MOD_CONTROL
        elif part == "alt":
            modifiers |= MOD_ALT
        elif part == "shift":
            modifiers |= MOD_SHIFT
        elif part in ("win", "meta"):
            modifiers |= MOD_WIN
        else:
            vk = _parse_vk(part)
    if vk is None:
        raise ValueError(f"无法解析热键按键: {combo}")
    return modifiers, vk


def _setup_types():
    user32.RegisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.RegisterHotKey.restype = ctypes.c_bool
    user32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = ctypes.c_bool
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(ctypes.wintypes.MSG),
        ctypes.wintypes.HWND,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    user32.PeekMessageW.restype = ctypes.c_bool
    user32.TranslateMessage.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG)]


_setup_types()


class GlobalHotkey:
    """注册一个系统级热键并在后台线程监听 WM_HOTKEY。

    通过 RegisterHotKey 注册，即使脚本正在抢占键盘/鼠标、窗口不在前台也能收到。
    """

    def __init__(self):
        self._thread = None
        self._callback = None
        self._quit = threading.Event()
        self._combo_text = ""
        self._thread_id = None

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, combo, callback):
        self.stop()
        self._combo_text = str(combo)
        self._callback = callback
        self._quit.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="global-hotkey")
        self._thread.start()

    def stop(self):
        self._quit.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None

    def _run(self):
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        try:
            modifiers, vk = parse_hotkey(self._combo_text)
        except ValueError as exc:
            warn(f"警告: 全局热键配置无效（{exc}），停止热键不可用。请检查配置里的 STOP_HOTKEY。")
            return
        hotkey_id = 1
        if not user32.RegisterHotKey(None, hotkey_id, modifiers, vk):
            warn(f"警告: 注册全局热键失败（{self._combo_text} 可能已被其它程序占用），"
                 "脚本运行期间该热键无法停止脚本，请改用界面上的「停止」按钮。")
            return
        try:
            msg = ctypes.wintypes.MSG()
            while not self._quit.is_set():
                # 轮询消息队列：收到 WM_HOTKEY 即触发回调，同时能响应 stop() 退出。
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY and msg.wParam == hotkey_id:
                        if self._callback is not None:
                            try:
                                self._callback()
                            except Exception:
                                pass
                    elif msg.message == WM_QUIT:
                        return
                    else:
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.05)
        finally:
            user32.UnregisterHotKey(None, hotkey_id)
