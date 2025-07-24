from typing import cast as type_cast

import wx

from .animation_widget import AnimationWidget
from ..animation import KeyFrameAnimation, KeyFrame, KeyFrameWay, MutilKeyFrameAnimation, ColorGradationAnimation
from ..dpi import SCALE
from ..style import Style, BtnStyle

cwxEVT_BUTTON = wx.NewEventType()
EVT_BUTTON = wx.PyEventBinder(cwxEVT_BUTTON, 1)


class ButtonEvent(wx.PyCommandEvent):
    def __init__(self):
        super().__init__(cwxEVT_BUTTON, wx.ID_ANY)


class Button(AnimationWidget):
    style: BtnStyle
    bg_anim: MutilKeyFrameAnimation

    bg_brush: wx.Brush
    text_color: wx.Colour
    border_pen: wx.GraphicsPenInfo

    def __init__(self, parent: wx.Window, label: str, widget_style: BtnStyle = None):
        super().__init__(parent, widget_style=widget_style, fps=60)
        self.SetLabel(label)
        self.bg_anim = MutilKeyFrameAnimation \
            (0.2,
             {"float":
                  KeyFrameAnimation(0.2, [
                      KeyFrame(KeyFrameWay.SMOOTH, 0, 0.0),
                      KeyFrame(KeyFrameWay.SMOOTH, 1, -0.08)]),
              "click":
                  KeyFrameAnimation(0.1, [
                      KeyFrame(KeyFrameWay.SMOOTH, 0, -0.08),
                      KeyFrame(KeyFrameWay.SMOOTH, 1, -0.16)
                  ]),
              "disable":
                  ColorGradationAnimation \
                      (0.1,
                       self.style.bg.copy,
                       self.style.bg.copy.add_luminance(-0.02),
                       [
                           KeyFrame(KeyFrameWay.SMOOTH, 0,
                                    0),
                           KeyFrame(KeyFrameWay.SMOOTH, 1, 1)
                       ])})

        self.reg_animation("bg", self.bg_anim)

        self.Bind(wx.EVT_MOUSE_EVENTS, self.on_mouse_events)

    def animation_callback(self):
        if self.bg_anim.is_playing:
            data = self.bg_anim.value
        else:
            return
        print(f"Lum: {data}")
        self.style.bg.reset()
        self.style.bg.add_luminance(data)
        self.bg_brush = wx.Brush(self.style.bg)
        self.Refresh()

    def on_mouse_events(self, event: wx.MouseEvent):
        event.Skip()
        if event.Entering():
            self.bg_anim.set_sub_anim("float")
            self.bg_anim.set_invent(invent=False)
        elif event.Leaving():
            self.bg_anim.set_sub_anim("float")
            self.bg_anim.set_invent(invent=True)
        elif event.LeftDown():
            self.bg_anim.set_sub_anim("click")
            self.bg_anim.set_invent(invent=False)
            event = ButtonEvent()
            wx.PostEvent(self, event)
        elif event.LeftUp():
            self.bg_anim.set_sub_anim("click")
            self.bg_anim.set_invent(invent=True)
        else:
            return
        self.play_animation("bg")
        self.Refresh()

    def SetLabel(self, label: str):
        super().SetLabel(label)
        dc = wx.ClientDC(self)
        width, height = type_cast(tuple, dc.GetTextExtent(label))
        size = (int(width + 40 * SCALE), int(height + 15 * SCALE))
        self.RawSetSize(size)
        self.RawSetMinSize(size)

    @staticmethod
    def translate_style(style: Style):
        return BtnStyle.load(style)

    def load_widget_style(self, style: BtnStyle):
        super().load_widget_style(style)
        self.bg_brush = wx.Brush(style.bg)
        self.text_color = style.fg
        self.border_pen = wx.GraphicsPenInfo(style.border_color, style.border_width * SCALE, style.border_style)

    def draw_content(self, gc: wx.GraphicsContext):
        w, h = type_cast(tuple[int, int], self.GetSize())

        # 绘制背景
        border_width = self.style.border_width * SCALE
        self.border_pen = wx.GraphicsPenInfo(self.style.border_color, border_width, self.style.border_style)
        gc.SetPen(gc.CreatePen(self.border_pen))
        gc.SetBrush(gc.CreateBrush(self.bg_brush))
        gc.DrawRoundedRectangle(border_width / 2, border_width / 2,
                                w - border_width, h - border_width,
                                self.style.corner_radius)
        # 绘制文字
        gc.SetFont(gc.CreateFont(self.GetFont(), self.text_color))
        label = self.GetLabel()
        t_w, t_h, t_x, t_y = type_cast(tuple[int, int, int, int], gc.GetFullTextExtent(label))
        gc.DrawText(label, (w - t_w) / 2, (h - t_h) / 2)
