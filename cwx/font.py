import wx

from cwx.dpi import X_SCALE

fonts_cache = {}


def ft(size: int) -> wx.Font:
    if size not in fonts_cache:
        sys_font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        sys_font.SetPixelSize(wx.Size(0, round(size / 0.75 * X_SCALE)))
        fonts_cache[size] = sys_font
        return sys_font
    return fonts_cache[size]
