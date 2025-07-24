from dataclasses import dataclass

from ..dpi import SCALE
from .color import *


class Style:
    def __init__(self):
        self.colors = Colors.default()

        self.default_style = EmptyStyle.load(self)
        self.btn_style = BtnStyle.load(self)
        self.textctrl_style = TextCtrlStyle.load(self)
        self.static_line_style = StaticLineStyle.load(self)
        self.progress_bar_style = ProgressBarStyle.load(self)


class WidgetStyle:
    def __init__(self, fg: wx.Colour = wx.WHITE, bg: wx.Colour = wx.BLACK):
        self.fg = fg
        self.bg = bg

    @staticmethod
    def load(style: Style) -> 'WidgetStyle':
        return WidgetStyle(
            style.colors.fg,
            style.colors.bg
        )


class EmptyStyle(WidgetStyle):
    pass


@dataclass
class BorderStyle:
    color: TransformableColor
    corner_radius: float
    width: float
    style: int

    active_color: GradientColor


class BtnStyle(WidgetStyle):
    fg: TransformableColor
    bg: TransformableColor

    def __init__(self,
                 fg: TransformableColor,
                 bg: TransformableColor,
                 border_color: wx.Colour,
                 corner_radius: float,
                 border_width: float,
                 border_style: int,
                 ):
        """
        :param fg: 按钮文字颜色
        :param bg: 按钮背景
        :param border_color: 边框颜色
        :param corner_radius: 边框圆角半径
        :param border_width: 边框宽度
        :param border_style: 边框样式 (wx.GraphicsPenInfo的样式)
        """
        super().__init__(fg, bg)
        self.border_color = border_color
        self.corner_radius = corner_radius
        self.border_width = border_width
        self.border_style = border_style

    @staticmethod
    def load(style: Style) -> 'BtnStyle':
        colors = style.colors

        return BtnStyle(
            fg=TC(colors.fg),
            bg=TC(ColorTransformer.light1(colors.primary)),
            border_color=TC(ColorTransformer.with_alpha(ColorTransformer.light1(colors.primary), 128)),
            corner_radius=6,
            border_width=2,
            border_style=wx.PENSTYLE_SOLID
        )


class TextCtrlStyle(WidgetStyle):
    def __init__(self,
                 input_fg: wx.Colour,
                 input_bg: wx.Colour,
                 border: wx.Colour,
                 active_tl_border: wx.Colour,
                 active_br_border: wx.Colour,
                 cursor: wx.Colour,
                 select_fg: wx.Colour,
                 select_bg: wx.Colour,

                 corner_radius: float,
                 select_corder_radius: float,
                 border_width: float,
                 active_border_width: float,
                 border_style: int):
        super().__init__(input_fg, input_bg)
        self.border = border
        self.active_tl_border = active_tl_border
        self.active_br_border = active_br_border
        self.cursor = cursor
        self.select_fg = select_fg
        self.select_bg = select_bg

        self.corner_radius = corner_radius
        self.select_corder_radius = select_corder_radius
        self.border_width = border_width
        self.active_border_width = active_border_width
        self.border_style = border_style

    @staticmethod
    def load(style: Style) -> 'TextCtrlStyle':
        colors = style.colors

        return TextCtrlStyle(
            input_fg=colors.input_fg,
            input_bg=colors.input_bg,
            border=colors.border,
            active_tl_border=colors.primary,
            active_br_border=colors.primary,
            cursor=colors.input_fg,
            select_fg=colors.input_fg,
            select_bg=colors.primary,

            corner_radius=4,
            select_corder_radius=5,
            border_width=1,
            active_border_width=2,
            border_style=wx.PENSTYLE_SOLID
        )

    @property
    def 桃子(self) -> 'TextCtrlStyle':
        self.active_tl_border = wx.Colour(0xfc, 0xcb, 0x90)
        self.active_br_border = wx.Colour(0xd5, 0x7e, 0xeb)
        return self


class StaticLineStyle(WidgetStyle):
    @staticmethod
    def load(style: Style) -> 'StaticLineStyle':
        return StaticLineStyle(
            bg=style.colors.border,
        )


class ProgressBarStyle(WidgetStyle):
    def __init__(self,
                 bg: wx.Colour, bar: GradientBrush,
                 border: GradientPen, corner_radius: float,
                 full_gradient: bool):
        super().__init__(bg=bg)
        self.corner_radius = corner_radius
        self.bar = bar
        self.border = border
        self.full_gradient = full_gradient

    @staticmethod
    def load(style: Style) -> 'ProgressBarStyle':
        return ProgressBarStyle(
            bg=ColorTransformer.with_alpha(style.colors.bg, 40),
            bar=GradientBrush(CT.dark1(style.colors.primary), CT.light1(style.colors.primary)),
            border=GradientPen(style.colors.border, width=1),
            corner_radius=5,
            full_gradient=True
        )

    @property
    def 赛博朋克(self) -> 'ProgressBarStyle':
        self.bar.gradient_stops.SetStartColour(wx.Colour(0x00, 0xdb, 0xde))
        self.bar.gradient_stops.SetEndColour(wx.Colour(0xfc, 0x00, 0xff))
        return self
