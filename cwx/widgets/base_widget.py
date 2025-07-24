import wx

from ..dpi import translate_size
from ..style import Style, WidgetStyle
from lib.perf import Counter

cwxEVT_STYLE_UPDATE = wx.NewEventType()
EVT_STYLE_UPDATE = wx.PyEventBinder(cwxEVT_STYLE_UPDATE, 1)


class StyleUpdateEvent(wx.PyCommandEvent):
    def __init__(self, gen_style: Style):
        super().__init__(cwxEVT_STYLE_UPDATE, wx.ID_ANY)
        self.gen_style = gen_style


"""
实现Widget的主题
1. 重写translate_style方法, 负责将 主题(Style) 转换为 组件主题(WidgetStyle)
2. 继承load_widget_style方法, 加载 组件主题(WidgetStyle)
"""


class Widget(wx.Window):
    """
    CustomWxpython的基础控件类
    """
    gen_style: Style
    style: WidgetStyle

    def __init__(self, parent: wx.Window, style=0, widget_style: WidgetStyle = None):
        super().__init__(parent, style=wx.TRANSPARENT_WINDOW | style)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        super().SetBackgroundColour(wx.BLACK)
        self.SetDoubleBuffered(True)

        if isinstance(parent, Widget):
            self.gen_style = parent.gen_style
        else:
            self.gen_style = Style()
        if widget_style:
            self.style = widget_style
        else:
            self.style = WidgetStyle.load(self.gen_style)
        setattr(self, "init_style", None)
        self.load_style(self.gen_style)
        delattr(self, "init_style")
        self.Bind(wx.EVT_SIZE, self.on_size)

    def on_size(self, event: wx.SizeEvent):
        event.Skip()
        if event.GetSize() != self.GetSize():
            self.Refresh()

    def SetSize(self, size: tuple[int, int]):
        super().SetSize(translate_size(size))

    def SetMinSize(self, size: tuple[int, int]):
        super().SetMinSize(translate_size(size))

    def SetMaxSize(self, size: tuple[int, int]):
        super().SetMaxSize(translate_size(size))

    def CacheBestSize(self, size: tuple[int, int]):
        super().CacheBestSize(translate_size(size))

    def RawSetSize(self, size: tuple[int, int]):
        super().SetSize(size)

    def RawSetMinSize(self, size: tuple[int, int]):
        super().SetMinSize(size)

    def RawSetMaxSize(self, size: tuple[int, int]):
        super().SetMaxSize(size)

    def SetBackgroundColour(self, colour):
        super().SetBackgroundColour(colour)
        self.style.bg = colour
        self.load_widget_style(self.style)

    def SetForegroundColour(self, colour):
        super().SetForegroundColour(colour)
        self.style.fg = colour
        self.load_widget_style(self.style)

    def load_style(self, style: Style):
        self.gen_style = style
        self.load_widget_style(self.translate_style(style))

    @staticmethod
    def translate_style(style: Style) -> WidgetStyle:
        return style.default_style

    def on_style_update(self, event: StyleUpdateEvent):
        self.update_style(event.gen_style)
        for child in self.GetChildren():
            if hasattr(child, "update_style") and hasattr(child.update_style, "__call__"):
                child.update_style(event.gen_style)

    def update_style(self, gen_style: Style):
        style = self.translate_style(gen_style)
        self.load_widget_style(style)

    def load_widget_style(self, style: WidgetStyle):
        self.style = style

    def on_paint(self, _):
        dc = wx.PaintDC(self)

        timer = Counter(create_start=True)
        self.draw_content(wx.GraphicsContext.Create(dc))
        #print(f"{self.__class__.__name__}: {timer.endT()}")

    def draw_content(self, gc: wx.GraphicsContext):
        pass
