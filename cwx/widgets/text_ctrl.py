import bisect
from typing import cast as type_cast

import wx

from .animation_widget import AnimationWidget
from .. import AnimationGroup, ColorGradationAnimation
from ..animation import KeyFrameWay, EZKeyFrameAnimation, full_keyframe
from ..dpi import SCALE
from ..style import Style, TextCtrlStyle

TC_X_PAD = TC_Y_PAD = 6

cwxEVT_TEXT = wx.NewEventType()
EVT_TEXT = wx.PyEventBinder(cwxEVT_TEXT, 1)


class TextEvent(wx.PyCommandEvent):
    def __init__(self):
        super().__init__(cwxEVT_TEXT)


class TextCtrl(AnimationWidget):
    style: TextCtrlStyle
    bg_brush: wx.Brush
    select_bg_brush: wx.Brush
    text_color: wx.Colour
    select_text_color: wx.Colour
    cursor_pen: wx.GraphicsPenInfo
    border_pen: wx.GraphicsPenInfo
    active_border_pen: wx.GraphicsPenInfo

    def __init__(self, parent: wx.Window, text: str, widget_style: TextCtrlStyle = None):
        super().__init__(parent, widget_style=widget_style, fps=60)
        self.text = text  # 文本
        self.cursor_char = 6  # 当前光标位置
        self.select_start: int | None = 3  # 当前选择的文本的起始位置
        self.calc_size()

        self.box_extent: tuple[int, int, int, int] | None = None  # 文本框外框
        self.text_extents: list[float] | None = None  # 文本长度缓存
        self.selecting = False  # 是否正在选择
        self.last_mouse_pos = wx.Point()  # 最后鼠标位置

        self.cursor_pos_anim = EZKeyFrameAnimation(0.15, KeyFrameWay.QUADRATIC_EASE, -1, -1)
        self.border_width = EZKeyFrameAnimation(0.15, KeyFrameWay.SMOOTH, self.style.border_width,
                                                self.style.active_border_width)
        self.border_tl_color = ColorGradationAnimation(0.15, self.style.border, self.style.active_tl_border,
                                                       full_keyframe(KeyFrameWay.SMOOTH))
        self.border_br_color = ColorGradationAnimation(0.15, self.style.border, self.style.active_br_border,
                                                       full_keyframe(KeyFrameWay.SMOOTH))
        self.border_anim = AnimationGroup({
            "width": self.border_width,
            "tl_color": self.border_tl_color,
            "br_color": self.border_br_color
        })
        self.reg_animation("cursor", self.cursor_pos_anim)
        self.reg_animation_group("border", self.border_anim)

        self.Bind(wx.EVT_CHAR, self.on_key)
        self.Bind(wx.EVT_MOUSE_EVENTS, self.on_mouse_event)
        self.Bind(wx.EVT_SET_FOCUS, self.OnFocus)
        self.Bind(wx.EVT_KILL_FOCUS, self.OnKillFocus)

    def SetFont(self, font: wx.Font):
        super().SetFont(font)
        self.calc_size()

    def SetOwnFont(self, font: wx.Font):
        super().SetOwnFont(font)
        self.calc_size()

    @staticmethod
    def translate_style(style: Style) -> TextCtrlStyle:
        return style.textctrl_style

    def load_widget_style(self, style: TextCtrlStyle):
        super().load_widget_style(style)
        self.text_color = style.fg
        self.bg_brush = wx.Brush(style.bg)
        self.select_text_color = style.select_fg
        self.select_bg_brush = wx.Brush(style.select_bg)
        self.cursor_pen = wx.GraphicsPenInfo(style.cursor, SCALE)
        self.border_pen = wx.GraphicsPenInfo(style.border, style.border_width, style.border_style)
        if not hasattr(self, "init_style"):
            self.border_width.set_range(style.border_width, style.active_border_width)
            self.border_tl_color.set_color(style.border, style.active_tl_border)
            self.border_br_color.set_color(style.border, style.active_br_border)

    def SetValue(self, text: str):
        self.text = text
        self.text_extents.clear()
        self.Refresh()

    # 内部方法

    def OnFocus(self, event: wx.FocusEvent):
        event.Skip()
        self.border_anim.set_invent(False)
        self.play_animation("border")

    def OnKillFocus(self, event: wx.FocusEvent):
        event.Skip()
        self.border_anim.set_invent(True)
        self.play_animation("border")

    def update_cursor_pos_target(self, cursor_char: int = None):
        if self.cursor_char == cursor_char and cursor_char is not None:
            return
        if cursor_char is not None:
            self.cursor_char = cursor_char
        target_x = self.text_extents[self.cursor_char]
        self.cursor_pos_anim.set_range(self.cursor_pos_anim.value, target_x)
        self.play_animation("cursor")

    def animation_callback(self):
        self.Refresh()

    def load_text_extends(self, gc: wx.GraphicsContext = None):
        if not gc:
            gc = wx.GraphicsContext.Create(self)
            font = gc.CreateFont(self.GetFont(), self.text_color)
            gc.SetFont(font)
        self.text_extents = gc.GetPartialTextExtents(self.text)
        self.text_extents.insert(0, 0)

    def on_key(self, event: wx.KeyEvent):
        if event.ControlDown():
            if event.KeyCode == wx.WXK_CONTROL_C:
                self.OnCopy()
                return
            elif event.KeyCode == wx.WXK_CONTROL_V:
                self.OnPaste()
                return
            elif event.KeyCode == wx.WXK_CONTROL_X:  # 新增Ctrl+X处理
                self.OnCut()
                return
        event.Skip()
        char = chr(event.UnicodeKey)
        if char.isprintable():
            # 处理字符输入
            if self.select_start is not None and self.select_start != self.cursor_char:
                # 替换选中文本
                self.DeleteValue(self.select_start, self.cursor_char)
                self.InsertValue(min(self.cursor_char, self.select_start), char)
            else:
                # 插入新字符
                self.InsertValue(self.cursor_char, char)
        elif event.KeyCode == wx.WXK_LEFT and self.cursor_char > 0:
            self.cursor_char -= 1
            self.select_start = None
        elif event.KeyCode == wx.WXK_RIGHT and self.cursor_char < len(self.text):
            self.cursor_char += 1
            self.select_start = None
        elif event.KeyCode == wx.WXK_BACK and self.cursor_char > 0:
            # 处理退格键
            if self.select_start is not None and self.select_start != self.cursor_char:
                self.DeleteValue(self.select_start, self.cursor_char)
            else:
                self.DeleteValue(self.cursor_char - 1, self.cursor_char)
        elif event.KeyCode == wx.WXK_DELETE and self.cursor_char < len(self.text):
            # 处理删除键
            if self.select_start is not None and self.select_start != self.cursor_char:
                self.DeleteValue(self.select_start, self.cursor_char)
            else:
                self.DeleteValue(self.cursor_char, self.cursor_char + 1)
        elif event.KeyCode == wx.WXK_HOME:  # 新增Home键处理
            self.cursor_char = 0
        elif event.KeyCode == wx.WXK_END:  # 新增End键处理
            self.cursor_char = len(self.text)
        else:
            return
        self.update_cursor_pos_target()
        t_event = TextEvent()
        self.ProcessEvent(t_event)
        self.Refresh()

    def OnCopy(self):
        if self.select_start is not None and self.select_start != self.cursor_char:
            start = min(self.select_start, self.cursor_char)
            end = max(self.select_start, self.cursor_char)
            text = self.text[start:end]
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(text))
                wx.TheClipboard.Close()

    def OnPaste(self):
        if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_TEXT)):
            wx.TheClipboard.Open()
            data = wx.TextDataObject()
            success = wx.TheClipboard.GetData(data)
            wx.TheClipboard.Close()
            if success:
                text = data.GetText()
                if self.select_start is not None and self.select_start != self.cursor_char:
                    start = min(self.select_start, self.cursor_char)
                    end = max(self.select_start, self.cursor_char)
                    self.DeleteValue(start, end)
                self.InsertValue(self.cursor_char, text)
                self.update_cursor_pos_target()
                self.Refresh()

    def OnCut(self):
        """实现剪切功能：复制选中内容到剪贴板并删除"""
        if self.select_start is not None and self.select_start != self.cursor_char:
            start = min(self.select_start, self.cursor_char)
            end = max(self.select_start, self.cursor_char)
            text = self.text[start:end]
            # 复制到剪贴板
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(text))
                wx.TheClipboard.Close()
            # 删除选中内容
            self.DeleteValue(start, end)
            self.update_cursor_pos_target()
            self.Refresh()

    def DeleteValue(self, from_pos: int, to_pos: int):
        """删除指定范围的内容"""
        from_pos, to_pos = min(from_pos, to_pos), max(from_pos, to_pos)
        self.text = self.text[:from_pos] + self.text[to_pos:]
        length = to_pos - from_pos
        if self.cursor_char >= to_pos:
            self.cursor_char -= length
        self.select_start = None
        self.load_text_extends()
        self.Refresh()

    def InsertValue(self, pos: int, value: str):
        """在指定位置前插入内容"""
        self.text = self.text[:pos] + value + self.text[pos:]
        length = len(value)
        if self.cursor_char >= pos:
            self.cursor_char += length
        self.select_start = None
        self.load_text_extends()
        self.Refresh()

    def on_mouse_event(self, event: wx.MouseEvent):
        event.Skip()
        if event.LeftDown():
            self.selecting = True
            self.select_start = None
            self.update_cursor_pos_target(self.get_cursor_pos_at_point(event.Position))
            self.CaptureMouse()
        elif event.Dragging() and self.selecting:
            char_pos = self.get_cursor_pos_at_point(event.Position)
            if self.cursor_char != char_pos and self.select_start is None:
                self.select_start = self.cursor_char
            self.update_cursor_pos_target(char_pos)
        elif event.LeftUp() and self.selecting:
            self.selecting = False
            self.update_cursor_pos_target(self.get_cursor_pos_at_point(event.Position))
            self.ReleaseMouse()
        else:
            return
        self.Refresh()

    def get_cursor_pos_at_point(self, point: wx.Point) -> int:
        """获取指定坐标对应的字符位置"""
        if not self.text_extents:
            self.Refresh()

        text_x = TC_X_PAD
        rel_x = point.x - text_x

        # 使用二分法优化查找（bisect_right）
        i = min(bisect.bisect_right(self.text_extents, rel_x), len(self.text))
        if self.text_extents[i] - rel_x > (self.text_extents[i] - self.text_extents[i - 1]) / 2:
            i -= 1
        return max(0, i)  # 确保不超过文本长度

    def calc_size(self):
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        w, h = type_cast(tuple, dc.GetTextExtent(self.text))
        pad_x = TC_X_PAD * 2
        pad_y = TC_Y_PAD * 2
        self.RawSetSize((w + pad_x, h + pad_y))
        self.RawSetMinSize((w + pad_x, h + pad_y))

    def draw_content(self, gc: wx.GraphicsContext):
        w, h = type_cast(tuple[int, int], self.GetSize())
        font = gc.CreateFont(self.GetFont(), self.text_color)
        gc.SetFont(font)
        if not self.box_extent:
            self.box_extent = t_w, t_h, t_x, t_y = type_cast(tuple[int, int, int, int], gc.GetFullTextExtent(self.text))
        else:
            t_w, t_h, t_x, t_y = self.box_extent
        if not self.text_extents:
            self.load_text_extends(gc)

        # 绘制背景
        border_width = self.border_width.value * SCALE
        self.border_pen = wx.GraphicsPenInfo(self.style.border, border_width, self.style.border_style) \
            .LinearGradient(0, 0, w, h, self.border_tl_color.value, self.border_br_color.value)
        gc.SetPen(gc.CreatePen(self.border_pen))
        gc.SetBrush(gc.CreateBrush(self.bg_brush))
        gc.DrawRoundedRectangle(border_width, border_width, w - border_width * 2, h - border_width * 2,
                                self.style.corner_radius)
        text_x = TC_X_PAD
        text_y = TC_Y_PAD
        if self.cursor_pos_anim.start == self.cursor_pos_anim.end == -1:
            cursor_x = self.text_extents[max(0, self.cursor_char)]
            self.cursor_pos_anim.set_range(cursor_x, cursor_x)
        else:
            cursor_x = self.cursor_pos_anim.value
        # 如果有选中范围，则绘制覆盖层
        if self.select_start is not None and self.cursor_pos_anim.value != self.text_extents[self.select_start]:
            offset_start = self.text_extents[self.select_start]
            offset_end = self.cursor_pos_anim.value
            offset_start, offset_end = min(offset_start, offset_end), max(offset_start, offset_end)
            offset_delta = offset_end - offset_start

            gc.SetPen(gc.CreatePen(wx.Pen((0, 0, 0, 0))))
            gc.SetBrush(gc.CreateBrush(self.select_bg_brush))
            gc.DrawRoundedRectangle(text_x + offset_start, text_y - 1,
                                    offset_delta, int(t_h + 2), self.style.select_corder_radius)

        # 绘制文字
        gc.DrawText(self.text, text_x, text_y)

        # 绘制光标
        gc.SetPen(gc.CreatePen(self.cursor_pen))
        gc.DrawLines([wx.Point2D(round(text_x + cursor_x), text_y), wx.Point2D(round(text_x + cursor_x), text_y + t_h)])
