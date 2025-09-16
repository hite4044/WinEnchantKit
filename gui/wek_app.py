import sys

import wx

from gui.control_panel import ControlPanel


class WinEnchantKitApp(wx.App):
    # noinspection PyAttributeOutsideInit
    def OnInit(self):
        show_window = not (len(sys.argv) > 1 and "-startup" in sys.argv)
        self.control_panel = ControlPanel(None, show_window)
        self.Bind(wx.EVT_QUERY_END_SESSION, self.OnQueryEndSession)
        return True

    def OnQueryEndSession(self, event):
        self.control_panel.Destroy()
        event.Skip()  # 允许关闭
