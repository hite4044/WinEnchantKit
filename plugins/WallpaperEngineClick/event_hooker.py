import win32gui
from ctypes import *


user32 = windll.user32

# 定义消息钩子函数
def window_message_hook(nCode, wParam, lParam):
    if nCode >= 0:
        print(f"消息类型: {wParam}, 参数: {lParam}")
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


def find_cls_desk():
    def on_enum_window(hwnd: int, extra: list):
        if win32gui.GetClassName(hwnd) == "WorkerW":
            extra.append(hwnd)
    worker_W_windows = []
    win32gui.EnumWindows(on_enum_window, worker_W_windows)
    for window in worker_W_windows:
        cls_desk = win32gui.FindWindowEx(window, None, "_cls_desk_", None)
        if cls_desk:
            return cls_desk
    return 0

window_handle = find_cls_desk()
if not window_handle:
    print("未找到目标窗口")
    exit()

# 注册钩子
HOOK_TYPE = 4  # WH_CALLWNDPROC
HOOKPROC = WINFUNCTYPE(c_int, c_int, c_int, POINTER(c_void_p))
hook_func = HOOKPROC(window_message_hook)

hooked = user32.SetWindowsHookExA(HOOK_TYPE, hook_func, None, 0)

if not hooked:
    print("钩子注册失败")
    exit()

# Hook WallpaperEngine的GetClassName函数
# 消息循环
msg = c_void_p()
while user32.GetMessageA(byref(msg), 0, 0, 0) > 0:
    user32.TranslateMessage(byref(msg))
    user32.DispatchMessageA(byref(msg))

# 卸载钩子
if hooked:
    user32.UnhookWindowsHookEx(hooked)
