from typing import Callable

import wx


class EtcMenu(wx.Menu):
    """绑定事件如同过ETC一样快！"""
    def __init__(self):
        super().__init__()

    def Append(self, label: str, handler: Callable = lambda : None, *args):
        line_id = super().Append(wx.ID_ANY, label).GetId()
        self.Bind(wx.EVT_MENU, lambda _: handler(*args), id=line_id)
