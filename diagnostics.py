"""运行期诊断消息的集中收集点。

为什么需要它
------------
启动脚本 `启动新界面.bat` 用**隐藏窗口**运行 python，stdout / stderr 用户都看不到
（全项目没有任何重定向）。所以"打印一行警告"并不等于"用户会知道"——唯一真正可见
的路径是把消息收集起来，由界面写进日志框。

谁在用
------
- `tasks.py` 的 4 个数据加载器（读取失败时不能静默回退默认值）
- `core/template_match.py`（模板文件读不了）
- `core/hotkey.py`（全局热键配置无效 / 注册失败——这条尤其重要：热键没注册上，
  用户按停止键不会有任何反应）

线程安全
--------
`core/hotkey.py` 在后台线程里产生警告、`core/template_match.py` 在 worker 线程里
产生警告，而界面只能在 GUI 线程里更新控件。所以这里只做"加锁收集 + 快照"，
界面侧用 `snapshot()` 取快照后自行搬到日志框（见 `gui_pyside6` 的
`_log_pending_warnings`），不在这里回调任何 UI。
"""
import sys
import threading

_WARNINGS = []
_LOCK = threading.Lock()


def warn(message):
    """记录一条用户应当看到的警告：写 stderr 并收集起来供界面显示。"""
    print(message, file=sys.stderr)
    with _LOCK:
        _WARNINGS.append(str(message))
    return message


def snapshot():
    """返回已收集警告的快照（线程安全，可安全跨线程调用）。"""
    with _LOCK:
        return list(_WARNINGS)


def clear():
    """清空已收集的警告（供测试使用）。"""
    with _LOCK:
        del _WARNINGS[:]
