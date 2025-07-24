import ctypes
from ctypes import wintypes

import colour
import wx

from ..dpi import SCALE

dwmapi = ctypes.WinDLL('dwmapi.dll')


def get_windows_theme_color():
    HRESULT = wintypes.LONG

    DwmGetColorizationColor = dwmapi.DwmGetColorizationColor
    DwmGetColorizationColor.argtypes = [ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL)]
    DwmGetColorizationColor.restype = HRESULT

    cr_colorization = wintypes.DWORD()
    f_opaque_blend = wintypes.BOOL()

    result = DwmGetColorizationColor(ctypes.byref(cr_colorization), ctypes.byref(f_opaque_blend))

    if result == 0:  # S_OK 的值为 0
        r = (cr_colorization.value >> 16) % 256
        g = (cr_colorization.value >> 8) % 256
        b = (cr_colorization.value >> 0) % 256
        return r, g, b
    else:
        return 0, 111, 196  # 如果获取颜色失败, 返回默认颜色


class LuminanceColor:
    def __init__(self, color: tuple[int, int, int] | wx.Colour):
        if isinstance(color, wx.Colour):
            color = color.GetRed(), color.GetGreen(), color.GetBlue()
        self.color = colour.Color(rgb=(color[0] / 255, color[1] / 255, color[2] / 255))
        self.base = self.color.get_luminance()

    def add_luminance(self, value: float):
        self.color.set_luminance(max(min(self.base + value, 1), 0))
        color: tuple[int, int, int] = self.color.get_rgb()
        return wx.Colour((int(color[0] * 255), int(color[1] * 255), int(color[2] * 255)))


class EasyColor(colour.Color):
    def __init__(self, color: tuple):
        super().__init__(rgb=(color[0] / 255, color[1] / 255, color[2] / 255))

    def add_luminance(self, value: float):
        self.set_luminance(max(min(self.get_luminance() + value, 1), 0))

    @property
    def rgb_tuple(self) -> tuple[int, int, int]:
        return int(self.get_red() * 255), int(self.get_green() * 255), int(self.get_blue() * 255)

    @property
    def int_rgb(self) -> int:
        color = self.rgb_tuple
        return (color[0] << 16) | (color[1] << 8) | color[2]


class TransformableColor(wx.Colour):
    color: EasyColor

    def __init__(self, color: tuple[int, int, int] | tuple[int, int, int, int] | wx.Colour):
        super().__init__(color)
        if isinstance(color, wx.Colour):
            color = (color.GetRed(), color.GetGreen(), color.GetBlue(), color.GetAlpha())
        self.base_rgba = color
        self.color = EasyColor(color[:3])
        self.base_color = self.color.get_rgb()

    def reset(self):
        self.color.set_rgb(self.base_color)
        self.Set(*self.base_rgba)
        return self

    def add_luminance(self, value: float):
        self.color.add_luminance(value)
        self.Set(*self.color.rgb_tuple, self.GetAlpha())
        return self

    @property
    def copy(self):
        return TransformableColor(wx.Colour(self.Get()))

    def set_alpha(self, alpha: int):
        rgb: int = self.GetRGB()
        rgba = (rgb << 8) | alpha
        self.SetRGBA(rgba)


class TC(TransformableColor):
    """TransformableColor的简写"""
    pass


COLOR_LEVEL = 0.04


class ColorTransformer:
    @staticmethod
    def with_alpha(color: wx.Colour, alpha: int):
        return wx.Colour(color.Red(), color.Green(), color.Blue(), alpha)

    @staticmethod
    def add_luminance(color: wx.Colour, luminance: float):
        color = LuminanceColor(color)
        return color.add_luminance(luminance)

    @staticmethod
    def highlight(color: wx.Colour):
        return ColorTransformer.add_luminance(color, COLOR_LEVEL)

    # light level

    @staticmethod
    def light1(color: wx.Colour):
        return ColorTransformer.add_luminance(color, COLOR_LEVEL)

    @staticmethod
    def light2(color: wx.Colour):
        return ColorTransformer.add_luminance(color, COLOR_LEVEL * 2)

    @staticmethod
    def light3(color: wx.Colour):
        return ColorTransformer.add_luminance(color, COLOR_LEVEL * 3)

    @staticmethod
    def dark1(color: wx.Colour):
        return ColorTransformer.add_luminance(color, -COLOR_LEVEL)

    @staticmethod
    def dark2(color: wx.Colour):
        return ColorTransformer.add_luminance(color, -COLOR_LEVEL * 2)

    @staticmethod
    def dark3(color: wx.Colour):
        return ColorTransformer.add_luminance(color, -COLOR_LEVEL * 3)


class CT(ColorTransformer):
    pass


class DefaultColors:
    SYSTEM_COLOR = get_windows_theme_color()
    def __init__(self):
        self.PRIMARY = wx.Colour(self.SYSTEM_COLOR)



class Colors:
    def __init__(self,
                 primary: wx.Colour,
                 secondary: wx.Colour,
                 fg: wx.Colour,
                 bg: wx.Colour,
                 border: wx.Colour,
                 input_fg: wx.Colour,
                 input_bg: wx.Colour):
        self.primary = primary
        self.secondary = secondary
        self.fg = fg
        self.bg = bg
        self.border = border
        self.input_fg = input_fg
        self.input_bg = input_bg

    @staticmethod
    def default():
        return Colors(
            primary=DefaultColors().PRIMARY,
            secondary=wx.Colour(85, 85, 85, 128),
            fg=wx.Colour(255, 255, 255),
            bg=wx.BLACK,
            border=wx.Colour(85, 85, 85),
            input_fg=wx.Colour(255, 255, 255),
            input_bg=wx.Colour(0, 0, 0, 40)
        )


class GradientColor(wx.Colour):
    def __init__(self,
                 color: tuple[int, int, int] | wx.Colour,
                 stop_color: tuple[int, int, int] | wx.Colour = None,
                 gradient_type: int = wx.GRADIENT_LINEAR,
                 direction: int = wx.HORIZONTAL,
                 stops: dict[float, tuple[int, int, int] | wx.Colour] = None):
        super().__init__(color)
        stop_color = stop_color if stop_color else color
        self.stop_color = stop_color
        self.gradient_type = gradient_type
        self.direction = direction
        self.stops = stops

        self.gradient_stops = wx.GraphicsGradientStops(color, stop_color)
        if stops:
            for stop in stops:
                self.gradient_stops.Add(wx.Colour(stop), stops[stop])

    def create_brush(self, gc: wx.GraphicsContext,
                     xy1: tuple[float, float], xy2: tuple[float, float] = None,
                     radius: float = 100):
        if self.gradient_type == wx.GRADIENT_LINEAR:
            if xy2 is None:
                if self.direction == wx.VERTICAL:
                    xy1, xy2 = (0, 0), (0, xy1[1])
                elif self.direction == wx.HORIZONTAL:
                    xy1, xy2 = (0, 0), (xy1[0], 0)
            return gc.CreateLinearGradientBrush(xy1[0], xy1[1], xy2[0], xy2[1], self.gradient_stops)
        elif self.gradient_type == wx.GRADIENT_RADIAL:
            return gc.CreateRadialGradientBrush(xy1[0], xy1[1], xy2[0], xy2[1], radius, self.gradient_stops)
        return gc.CreateBrush(wx.Brush(self))

    def create_pen(self, gc: wx.GraphicsContext,
                   xy1: tuple[float, float], xy2: tuple[float, float] = None,
                   width: float = 1, style: int = wx.PENSTYLE_SOLID, radius: float = 100):
        pen = wx.GraphicsPenInfo(self, width * SCALE, style)
        if self.gradient_type == wx.GRADIENT_LINEAR:
            if xy2 is None:
                if self.direction == wx.VERTICAL:
                    xy1, xy2 = (0, 0), (0, xy1[1])
                elif self.direction == wx.HORIZONTAL:
                    xy1, xy2 = (0, 0), (xy1[0], 0)

            last_color = self
            if self.stops:
                for percent, color in self.stops.items():
                    t1 = xy1[0] + (xy2[0] - xy1[0]) * percent
                    t2 = xy1[1] + (xy2[1] - xy1[1]) * percent
                    pen = pen.LinearGradient(xy1[0], xy1[1], t1, t2, last_color, color)
                    last_color = color
            if self.stop_color:
                pen = pen.LinearGradient(xy1[0], xy1[1], xy2[0], xy2[1], last_color, self.stop_color)
        elif self.gradient_type == wx.GRADIENT_RADIAL:
            pen = pen.RadialGradient(xy1[0], xy1[1], xy2[0], xy2[1], radius, self.stops)
        return gc.CreatePen(pen)


class GradientPen(GradientColor):
    def __init__(self,
                 color: tuple[int, int, int] | wx.Colour,
                 stop_color: tuple[int, int, int] | wx.Colour = None,
                 gradient_type: int = wx.GRADIENT_LINEAR,
                 direction: int = wx.HORIZONTAL,
                 stops: dict[float, tuple[int, int, int] | wx.Colour] = None,
                 width: float = 1, pen_style: int = wx.PENSTYLE_SOLID, radius: float = 100):
        super().__init__(color, stop_color, gradient_type, direction, stops)
        self.width = width
        self.pen_style = pen_style
        self.radius = radius

    def create_pen(self, gc: wx.GraphicsContext,
                   xy1: tuple[float, float], xy2: tuple[float, float] = None,
                   *args):
        return super().create_pen(gc, xy1, xy2, self.width, self.pen_style, self.radius)


class GradientBrush(GradientColor):
    pass
