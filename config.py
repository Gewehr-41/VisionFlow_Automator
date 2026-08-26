# 全局配置：集中管理模板识别、屏幕捕获和输入操作参数。
import os


# ==================== 全局配置 ====================
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
THRESHOLD = 0.8                    # 模板匹配阈值
USE_MULTI_SCALE = True              # 是否启用多尺度匹配
SCALE_RANGE = (0.8, 1.2)            # 缩放范围
SCALE_STEP = 0.05                   # 缩放步长
MONITOR_INDEX = 1                   # 截屏监视器编号（0=全部，1=主屏）
SHOW_PREVIEW = False                # 是否显示 OpenCV 预览窗口
CLICK_DELAY = 0.05                  # 点击前后的移动延迟，尽量更快
DEFAULT_TIMEOUT = 50.0               # 默认等待超时时间
DEFAULT_POLL_INTERVAL = 0.1        # 扫描间隔，减少反应延迟

# 后台窗口模式
USE_WINDOW_MODE = True              # 是否优先按指定窗口区域识别
TARGET_WINDOW_TITLE = None          # 例如: "Your Game"，None 表示使用前台窗口或屏幕
# =================================================
