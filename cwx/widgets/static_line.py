from typing import cast as type_cast

import wx

from .base_widget import Widget
from ..style import StaticLineStyle, Style


class StaticLine(Widget):
    style: StaticLineStyle
    bg_brush: wx.Brush

    def __init__(self, parent: wx.Window, style: int = wx.HORIZONTAL, widget_style: StaticLineStyle = None):
        super().__init__(parent, widget_style=widget_style)
        if style in [wx.HORIZONTAL, wx.LI_HORIZONTAL]:
            wx.Window.SetSize(self, (-1, 1))
        elif style in [wx.VERTICAL, wx.LI_VERTICAL]:
            wx.Window.SetSize(self, (1, -1))
        wx.Window.SetMinSize(self, (1, 1))

    @staticmethod
    def translate_style(style: Style) -> StaticLineStyle:
        return style.static_line_style

    def load_widget_style(self, style: StaticLineStyle):
        super().load_widget_style(style)
        self.bg_brush = wx.Brush(style.bg)

    def draw_content(self, gc: wx.GraphicsContext):
        w, h = type_cast(tuple[int, int], self.GetSize())
        gc.SetBrush(gc.CreateBrush(self.bg_brush))
        gc.DrawRectangle(0, 0, w, h)
