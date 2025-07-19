import win32con
from win32api import GetWindowLong
from win32gui import GetClassName, EnumWindows, GetWindowText, FindWindow

style_map: dict[int, str] = {
    getattr(win32con, name): name
    for name in dir(win32con)
    if name.startswith("WS_") and (not name.startswith("WS_EX_"))
}
rev_style_map: dict[str, int] = {v: k for k, v in style_map.items()}


def get_kugou_windows() -> list[int] | None:
    def callback(hwnd: int, extras: list):
        if GetClassName(hwnd) == "kugou_ui":
            extras.append(hwnd)
        return True

    hwnds = []
    EnumWindows(callback, hwnds)
    if len(hwnds) == 0:
        return None
    return hwnds


def get_window_style_strings(win_style: int) -> list[str]:
    style_strings = []
    for style, name in style_map.items():
        if win_style & style:
            style_strings.append(name)
    return style_strings


def add_style(raw_style: int, new_style: int) -> int:
    style_strings = get_window_style_strings(raw_style)
    style_strings.append(style_map[new_style])
    new_style = 0x0
    for style_str in style_strings:
        new_style |= rev_style_map[style_str]
    return new_style


def filter_hwnd(hwnds: list[int]) -> int | None:
    for hwnd in hwnds:
        title = GetWindowText(hwnd)
        if all((c in title) for c in "酷狗音乐"):
            style_strings = get_window_style_strings(GetWindowLong(hwnd, win32con.GWL_STYLE))
            if "WS_MINIMIZEBOX" in style_strings:
                return hwnd
    return None

def get_main_kugou_window() -> int | None:
    hwnd = FindWindow("kugou_ui", None)
    if not hwnd:
        return None
    hwnds = get_kugou_windows()
    if hwnds is None:
        return None
    return filter_hwnd(hwnds)