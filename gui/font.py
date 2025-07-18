import wx

fonts_cache = {}


def ft(size: int) -> wx.Font:
    if size not in fonts_cache:
        sys_font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        sys_font.SetPointSize(size)
        fonts_cache[size] = sys_font
        return sys_font
    return fonts_cache[size]
