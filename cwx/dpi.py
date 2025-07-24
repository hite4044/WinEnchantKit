import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)

# 定义Windows API函数
SetProcessDPIAware = user32.SetProcessDPIAware
SetProcessDPIAware.argtypes = []
SetProcessDPIAware.restype = wintypes.BOOL

GetDC = user32.GetDC
GetDC.argtypes = [wintypes.HWND]
GetDC.restype = wintypes.HDC

GetDeviceCaps = gdi32.GetDeviceCaps
GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
GetDeviceCaps.restype = ctypes.c_int

LOG_PIXEL_SX = 88
LOG_PIXEL_SY = 90


def get_screen_scale():
    user32.SetProcessDPIAware()
    hDC = GetDC(0)
    x_dpi = GetDeviceCaps(hDC, LOG_PIXEL_SX)
    y_dpi = GetDeviceCaps(hDC, LOG_PIXEL_SY)
    return x_dpi / 96, y_dpi / 96


def translate_size(size: tuple[int, int]):
    return int(size[0] * X_SCALE), int(size[1] * Y_SCALE)


X_SCALE, Y_SCALE = get_screen_scale()
SCALE = X_SCALE