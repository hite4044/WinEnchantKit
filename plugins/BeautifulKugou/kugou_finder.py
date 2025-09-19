import win32con
from win32api import GetWindowLong
from win32gui import GetClassName, EnumWindows, FindWindow

style_map: dict[int, str] = {
    getattr(win32con, name): name
    for name in dir(win32con)
    if name.startswith("WS_") and (not name.startswith("WS_EX_"))
}
ex_style_map: dict[int, str] = {
    getattr(win32con, name): name
    for name in dir(win32con)
    if name.startswith("WS_EX_")
}
rev_style_map: dict[str, int] = {v: k for k, v in style_map.items()}


class ProcType(int):
    KUGOU = 0
    QQ_MUSIC = 1


CLS_NAME_MAP = {
    ProcType.KUGOU: ("kugou_ui", "WS_THICKFRAME"),
    ProcType.QQ_MUSIC: ("TXGuiFoundation", "WS_DLGFRAME")
}


def get_kugou_windows(cls_name: str) -> list[int] | None:
    def callback(hwnd: int, extras: list):
        if GetClassName(hwnd) == cls_name:
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


def get_window_ex_style_strings(win_ex_style: int) -> list[str]:
    style_strings = []
    for style, name in ex_style_map.items():
        if win_ex_style & style:
            style_strings.append(name)
    return style_strings

def add_style(raw_style: int, new_style: int) -> int:
    style_strings = get_window_style_strings(raw_style)
    style_strings.append(style_map[new_style])
    new_style = 0x0
    for style_str in style_strings:
        new_style |= rev_style_map[style_str]
    return new_style


def filter_hwnd(windows: list[int], style_name: str) -> int | None:
    for hwnd in windows:
        style_strings = get_window_style_strings(GetWindowLong(hwnd, win32con.GWL_STYLE))
        if style_name in style_strings:
            return hwnd
    return None


def get_main_kugou_window(proc_type: int) -> int | None:
    cls_name, style_name = CLS_NAME_MAP[proc_type]
    hwnd = FindWindow(cls_name, None)
    if not hwnd:
        return None
    windows = get_kugou_windows(cls_name)
    if windows is None:
        return None
    return filter_hwnd(windows, style_name)
