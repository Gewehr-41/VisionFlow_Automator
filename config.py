# 全局配置：集中管理模板识别、屏幕捕获和输入操作参数。
import os

from paths import ICON_DIR   # 路径只在 paths.py 里算（见那里的说明）


# ==================== 全局配置 ====================
THRESHOLD = 0.8                    # 模板匹配阈值
USE_MULTI_SCALE = True              # 是否启用多尺度匹配
SCALE_RANGE = (0.8, 1.2)            # 缩放范围
SCALE_STEP = 0.05                   # 缩放步长
MONITOR_INDEX = 1                   # 截屏监视器编号（0=全部，1=主屏）
SHOW_PREVIEW = False                # 是否显示 OpenCV 预览窗口
CLICK_DELAY = 0.05                  # 点击前后的移动延迟，尽量更快
DEFAULT_TIMEOUT = 50.0               # 默认等待超时时间
DEFAULT_POLL_INTERVAL = 0.1        # 扫描间隔，减少反应延迟
DEFAULT_RETRY_INTERVAL = 1.0       # 步骤级重试的默认间隔（秒）；重试次数默认 0 表示不启用
OCR_MIN_CONFIDENCE = 0.5           # OCR 结果的最低置信度（低于它的文字不算命中）

# ==================== 识别算法增强（2026-10-28） ====================
# 1) 上次命中位置优先搜索：先在上次位置附近找，未命中再退回全屏
PREFERRED_SEARCH_RADIUS = 220
# 2) 透明 PNG 模板使用 alpha 掩膜匹配；命中后做颜色二次校验
USE_TEMPLATE_MASK = True
COLOR_CHECK_ENABLED = True
COLOR_CHECK_TOLERANCE = 48.0
# 3) 多帧确认：大于 1 时连续命中同样位置 N 帧才点击（1=旧行为）
RECOGNITION_CONFIRM_FRAMES = 1
# 4) OCR：区域裁剪预处理 + 编辑距离模糊匹配
OCR_PREPROCESS = True
OCR_FUZZY = True
OCR_FUZZY_RATIO = 0.86
# 5) 模板匹配失败后 ORB 特征匹配兜底（只在 execute_task 识别流程启用）
FEATURE_FALLBACK = True
FEATURE_MIN_MATCHES = 10
# ============================================================

# ==================== 版本与自动更新（F11） ====================
# 程序版本号：打包版与源码版同源。发版时改这里 + 打同号 tag。
APP_VERSION = "0.5.1"
UPDATE_REPO = "Gewehr-41/VisionFlow_Automator"   # GitHub 仓库（查 Releases）
CHECK_UPDATE_ON_START = True       # 启动后在后台检查一次更新（只提示，不自动下载）
UPDATE_CHECK_TIMEOUT = 4.0         # 单次请求超时（秒）；超时/断网时静默跳过
UPDATE_PROXY = ""                  # 留空 = 自动探测（环境变量 → Windows 系统代理 → 直连）
# ============================================================

# 后台窗口模式
USE_WINDOW_MODE = True              # 是否优先按指定窗口区域识别
TARGET_WINDOW_TITLE = None          # 例如: "Your Game"，None 表示使用前台窗口或屏幕
STOP_HOTKEY = "ctrl+c"              # 全局停止快捷键。注意：RegisterHotKey 是系统级拦截，
                                    # 脚本运行期间按 ctrl+c 会直接停止脚本（不会触发复制）；
                                    # 脚本停止后热键自动注销，ctrl+c 恢复正常。若不需要 ctrl+c
                                    # 可改为 ctrl+alt+f9 等不易冲突的组合。
# =================================================
