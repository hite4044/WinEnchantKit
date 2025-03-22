from typing import Callable

import wx

from base import *


class ColorInputCtrl(wx.Panel):
    def __init__(self, parent: wx.Window, color: tuple[int, int, int]):
        super().__init__(parent=parent)
        self.color = color

        self.color_box = wx.Panel(self, size=(20, 20), style=wx.SUNKEN_BORDER)
        self.color_box.SetBackgroundColour(wx.Colour(*self.color))
        self.color_box.Bind(wx.EVT_LEFT_DOWN, self.on_color_box_click)

        self.r_label = wx.StaticText(self, label="R")
        self.g_label = wx.StaticText(self, label="G")
        self.b_label = wx.StaticText(self, label="B")

        self.r_input = wx.TextCtrl(self, value=str(self.color[0]), size=(50, -1))
        self.g_input = wx.TextCtrl(self, value=str(self.color[1]), size=(50, -1))
        self.b_input = wx.TextCtrl(self, value=str(self.color[2]), size=(50, -1))

        self.sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.sizer.Add(self.color_box, 0, wx.ALL, 5)
        self.sizer.Add(self.r_label, 0, wx.EXPAND)
        self.sizer.Add(self.r_input, 0, wx.EXPAND)
        self.sizer.Add(self.g_label, 0, wx.EXPAND)
        self.sizer.Add(self.g_input, 0, wx.EXPAND)
        self.sizer.Add(self.b_label, 0, wx.EXPAND)
        self.sizer.Add(self.b_input, 0, wx.EXPAND)

        self.SetSizer(self.sizer)

    def on_color_box_click(self, _):
        color_dialog = wx.ColourDialog(self)
        color_dialog.GetColourData().SetColour(wx.Colour(*self.color))
        if color_dialog.ShowModal() == wx.ID_OK:
            new_color = color_dialog.GetColourData().GetColour()
            self.color_box.SetBackgroundColour(new_color)
            self.color_box.Refresh()
            self.r_input.SetValue(str(new_color.Red()))
            self.g_input.SetValue(str(new_color.Green()))
            self.b_input.SetValue(str(new_color.Blue()))
            self.color = (new_color.Red(), new_color.Green(), new_color.Blue())
        color_dialog.Destroy()

    def get_value(self) -> tuple[int, int, int]:
        return int(self.r_input.GetValue()), int(self.g_input.GetValue()), int(self.b_input.GetValue())


class ConfigLine(wx.Panel):
    def __init__(self, parent: wx.Window, param: ConfigParam, value: Any, use_sizer: bool = True):
        if use_sizer:
            super().__init__(parent=parent)
            parent = self
        self.param = param

        self.label = wx.StaticText(parent, label=param.desc)
        if param.kind == ParamKind.BOOL:
            self.input = wx.CheckBox(parent)
            self.input.SetValue(value)
        elif param.kind == ParamKind.CHOICE:
            assert isinstance(param, ChoiceParam)
            self.input = wx.ComboBox(parent, value=value, choices=param.choices)
        elif param.kind == ParamKind.BUTTON:
            assert isinstance(param, ButtonParam)
            self.input = wx.Button(parent, label=param.desc)
            self.input.Bind(wx.EVT_BUTTON, param.handler)
            self.label.SetLabel("")
        elif param.kind == ParamKind.COLOR:  # 新增颜色类型处理
            self.input = ColorInputCtrl(parent, value)
        else:
            self.input = wx.TextCtrl(parent, value=str(value))
        if use_sizer:
            self.sizer = wx.BoxSizer(wx.HORIZONTAL)
            self.sizer.Add(self.label, 0, wx.EXPAND)
            self.sizer.Add(self.input, 1, wx.EXPAND)
            self.sizer.SetMinSize((-1, 25))
            self.SetSizer(self.sizer)

    def get_value(self) -> Any:
        if self.param.kind in [ParamKind.BOOL, ParamKind.STRING]:
            return self.input.GetValue()
        elif self.param.kind == ParamKind.BUTTON:
            assert isinstance(self.param, ButtonParam)
            return self.param.handler
        elif self.param.kind == ParamKind.COLOR:  # 新增颜色类型处理
            assert isinstance(self.input, ColorInputCtrl)
            return self.input.get_value()
        else:
            return self.param.parse_value(self.input.GetValue())


class ConfigEditor(wx.Dialog):
    def __init__(self, parent: wx.Frame, config: ModuleConfig, cbk: Callable[[dict[str, Any]], None]):
        super().__init__(parent=parent, size=(400, 200), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.callback = cbk
        self.SetTitle("配置")
        self.out_sizer = wx.BoxSizer(wx.VERTICAL)
        for name, param in config.params.items():
            if param.kind == ParamKind.TIP:
                self.out_sizer.Add(wx.StaticText(self, label=param.desc), 0, wx.EXPAND)
                self.out_sizer.AddSpacer(5)
        self.sizer = wx.FlexGridSizer(len(config), 2, 5, 5)
        self.lines: dict[str, ConfigLine] = {}
        for name, param in config.params.items():
            if param.kind == ParamKind.TIP:
                continue
            line = ConfigLine(self, param, config[name], use_sizer=False)
            line.input.SetMinSize((200, 29))
            self.sizer.Add(line.label, 0, wx.EXPAND)
            self.sizer.Add(line.input, 1, wx.EXPAND)
            self.lines[name] = line
        self.out_sizer.Add(self.sizer, 1, wx.EXPAND)
        self.out_sizer.Add(wx.StaticLine(self, style=wx.LI_HORIZONTAL), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 10)

        self.ok_btn = wx.Button(self, label="确定")
        self.cancel_btn = wx.Button(self, label="取消")
        self.ok_btn.Bind(wx.EVT_BUTTON, self.on_ok)
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel)

        self.out_sizer.Add(self.cancel_btn, 0, wx.ALIGN_RIGHT)
        self.out_sizer.AddSpacer(5)
        self.out_sizer.Add(self.ok_btn, 0, wx.ALIGN_RIGHT)
        self.out_sizer.Fit(self)

        self.SetSizer(self.out_sizer)

    def on_ok(self, _):
        self.callback({name: line.get_value() for name, line in self.lines.items()})
        self.Destroy()

    def on_cancel(self, _):
        self.Destroy()
