from typing import cast as type_cast

import wx

from .base_widget import Widget
from ..style.__init__ import WidgetStyle


class StaticText(Widget):
    style: WidgetStyle
    text_color: wx.Colour

    def __init__(self, parent: wx.Window, label: str, widget_style: WidgetStyle = None):
        super().__init__(parent, widget_style=widget_style)
        self.SetLabel(label)

    def SetLabel(self, label: str):
        super().SetLabel(label)
        self.load_size()

    def load_size(self):
        dc = wx.ClientDC(self)
        width, height, _, _ = type_cast(tuple, dc.GetFullTextExtent(self.GetLabel(), self.GetFont()))
        self.SetSize((width, height))
        self.SetMinSize((width, height))
        self.CacheBestSize(self.GetSize())
        print(self.GetSize())

    def SetFont(self, font):
        super().SetFont(font)
        self.load_size()

    def load_widget_style(self, style: WidgetStyle):
        super().load_widget_style(style)
        self.text_color = style.fg

    def draw_content(self, gc: wx.GraphicsContext):
        gc.SetFont(gc.CreateFont(self.GetFont(), self.text_color))
        gc.DrawText(self.GetLabel(), 0, 0)
