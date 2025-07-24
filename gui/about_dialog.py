import ctypes
import webbrowser

import wx

import cwx
from cwx import DefaultColors
from gui.font import ft
from plugins.BeautifulKugou.dwm import ACCENT_POLICY, ACCENT_STATE, WINDOWCOMPOSITIONATTRIBDATA, \
    WINDOWCOMPOSITIONATTRIB, SetWindowCompositionAttribute, DwmExtendFrameIntoClientArea, MARGINS, DWM_BLURBEHIND, \
    DWM_BB_ENABLE, DwmEnableBlurBehindWindow

DwmSetWindowAttribute = ctypes.windll.dwmapi.DwmSetWindowAttribute


class AboutDialog(wx.Frame):
    def __init__(self, parent: wx.Window | None):
        super().__init__(parent, title="关于", size=(650, 600),
                         style=wx.CAPTION | wx.SYSTEM_MENU | wx.RESIZE_BORDER | wx.MINIMIZE_BOX | wx.CLOSE_BOX | wx.MAXIMIZE_BOX)
        self.blur()
        self.SetIcon(wx.Icon("assets/icon.ico", wx.BITMAP_TYPE_ICO))
        self.SetFont(ft(12))

        self.sizer = wx.BoxSizer(wx.VERTICAL)

        self.sizer.Add(wx.StaticBitmap(self, bitmap=wx.Image("assets/icon.ico")), 0, wx.ALIGN_CENTER_HORIZONTAL)

        text_sizer = wx.BoxSizer(wx.HORIZONTAL)
        text_sizer.AddStretchSpacer()
        texts = [("Win", (37, 182, 233)), ("Enchant", (202, 90, 204)), ("Kit", (255, 127, 39))]
        for text, color in texts:
            st = cwx.StaticText(self, label=text)
            st.SetFont(ft(18))
            st.SetForegroundColour(wx.Colour(color))
            text_sizer.Add(st)
        text_sizer.AddStretchSpacer()
        self.sizer.Add(text_sizer, 1, wx.EXPAND)

        lines: list[tuple[int, str]] = [
            (0, "Version 1.0"),
            (0, "© 2025 hite4044, All rights reserved.")
        ]
        for font_size, line in lines:
            text = cwx.StaticText(self, label=line)
            if font_size != 0:
                text.SetFont(ft(font_size))
            self.sizer.Add(text, 0, wx.ALIGN_CENTER_HORIZONTAL)

        btn = cwx.Button(self, label="查看BiliBili宣传视频")
        btn.Bind(wx.EVT_BUTTON, self.open_bilibili_video)
        self.sizer.Add(btn)
        self.sizer.AddSpacer(5)

        text_ctrl = cwx.TextCtrl(self, "输入pwd查看内容")
        text_ctrl.SetMinSize((-1, 50))
        text_ctrl.load_widget_style(text_ctrl.style.桃子)
        text_ctrl.Bind(cwx.EVT_TEXT, self.check_pwd)
        self.sizer.Add(text_ctrl, 0, wx.EXPAND)
        self.sizer.AddSpacer(5)
        self.tc = text_ctrl

        progress = cwx.ProgressBar(self)
        progress.SetValue(30)
        self.sizer.Add(progress, 0, wx.EXPAND)
        self.sizer.AddSpacer(5)

        self.SetSizer(self.sizer)

    def open_bilibili_video(self, _):
        wx.MessageBox("还没有做好呢qwq", "提示", style=wx.OK | wx.ICON_INFORMATION)
        # webbrowser.open("")

    def check_pwd(self, _):
        if self.tc.text == "pwd":
            self.open_bilibili_video2()

    @staticmethod
    def open_bilibili_video2():
        webbrowser.open("https://www.bilibili.com/video/BV1GJ411x7h7")

    def blur(self):
        self.SetBackgroundColour(wx.BLACK)
        color = DefaultColors.SYSTEM_COLOR + (30,)

        DwmSetWindowAttribute(self.GetHandle(), 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
        bb = DWM_BLURBEHIND(
            dwFlags=DWM_BB_ENABLE,
            fEnable=True,
            hRgnBlur=0,
            fTransitionOnMaximized=False,
        )
        DwmEnableBlurBehindWindow(self.GetHandle(), ctypes.byref(bb))

        hex_color = color[2] << 16 | color[1] << 8 | color[0]
        accent = ACCENT_POLICY(AccentState=ACCENT_STATE.ACCENT_ENABLE_ACRYLICBLURBEHIND,
                               GradientColor=(color[3] << 24) | (hex_color & 0xFFFFFF))
        attrib = WINDOWCOMPOSITIONATTRIBDATA(
            Attrib=WINDOWCOMPOSITIONATTRIB.WCA_ACCENT_POLICY,
            pvData=ctypes.byref(accent),
            cbData=ctypes.sizeof(accent),
        )
        SetWindowCompositionAttribute(self.GetHandle(), ctypes.byref(attrib))
        margins = MARGINS(-1, -1, -1, -1)
        DwmExtendFrameIntoClientArea(self.GetHandle(), ctypes.byref(margins))


if __name__ == "__main__":
    app = wx.App()
    AboutDialog(None).Show()
    app.MainLoop()