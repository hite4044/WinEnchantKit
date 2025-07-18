from typing import Callable

import wx

from base import *
from gui.center_text import CenteredText
from gui.ect_menu import EtcMenu


class ColorInputCtrl(wx.Panel):
    def __init__(self, parent: wx.Window, color: tuple[int, int, int]):
        super().__init__(parent=parent)
        self.color = color

        self.color_box = wx.Panel(self, size=(20, 20), style=wx.SUNKEN_BORDER)
        self.color_box.SetBackgroundColour(wx.Colour(*self.color))
        self.color_box.Bind(wx.EVT_LEFT_DOWN, self.on_color_box_click)

        self.r_label = CenteredText(self, label="R")
        self.g_label = CenteredText(self, label="G")
        self.b_label = CenteredText(self, label="B")

        self.r_input = wx.TextCtrl(self, value=str(self.color[0]), size=(30, -1))
        self.g_input = wx.TextCtrl(self, value=str(self.color[1]), size=(30, -1))
        self.b_input = wx.TextCtrl(self, value=str(self.color[2]), size=(30, -1))

        self.sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.sizer.Add(self.color_box, 0, wx.ALL, 2)
        self.sizer.Add(self.r_label, 0, wx.EXPAND)
        self.sizer.Add(self.r_input, 1, wx.EXPAND)
        self.sizer.Add(self.g_label, 0, wx.EXPAND)
        self.sizer.Add(self.g_input, 1, wx.EXPAND)
        self.sizer.Add(self.b_label, 0, wx.EXPAND)
        self.sizer.Add(self.b_input, 1, wx.EXPAND)

        self.SetSizer(self.sizer)

        self.Bind(wx.EVT_CHAR, self.on_char)

    def on_char(self, event: wx.Event):
        event.Skip()
        try:
            self.color = (int(self.r_input.GetValue()), int(self.g_input.GetValue()), int(self.b_input.GetValue()))
            self.color_box.SetBackgroundColour(wx.Colour(*self.color))
            self.color_box.Refresh()
        except ValueError:
            pass

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


class EditableListBox(wx.Panel):
    def __init__(self, parent: wx.Window, data: list, param: TableParam):
        super().__init__(parent=parent)
        self.param = param
        headers = param.headers if param.headers else [("NO_HEADER", 300)]
        style = wx.LC_REPORT | wx.LC_EDIT_LABELS | wx.LC_SINGLE_SEL
        if not param.headers:
            style |= wx.LC_NO_HEADER

        self.ctrl = wx.ListCtrl(self, style=style)
        self.add_btn = wx.Button(self.ctrl, label="+", size=(25, 25))
        self.remove_btn = wx.Button(self.ctrl, label="-", size=(25, 25))

        for head, width in headers:
            self.ctrl.AppendColumn(head, width=width)
        for i, item in enumerate(data):
            if not isinstance(item, tuple):
                self.ctrl.InsertItem(i, str(item))
            else:
                self.ctrl.InsertItem(i, str(item[0]))
                for j, content in enumerate(item[1:]):
                    self.ctrl.SetItem(i, j + 1, str(content))

        btn_sizer_ver = wx.BoxSizer(wx.VERTICAL)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.add_btn, 0, wx.EXPAND)
        btn_sizer.Add(self.remove_btn, 0, wx.EXPAND)
        btn_sizer_ver.AddStretchSpacer()
        btn_sizer_ver.Add(btn_sizer, 0, wx.EXPAND)
        self.ctrl.SetSizer(btn_sizer_ver)

        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.sizer.Add(self.ctrl, 1, wx.EXPAND)
        self.SetSizer(self.sizer)

        self.add_btn.Bind(wx.EVT_BUTTON, self.on_add)
        self.remove_btn.Bind(wx.EVT_BUTTON, self.on_remove)

    def on_menu(self, event: wx.ListEvent):
        menu = EtcMenu()
        index = event.GetIndex()
        menu.Append("添加", self.on_add)
        menu.Append("删除", self.on_remove, None, index)

    def on_add(self, _):
        self.ctrl.InsertItem(self.ctrl.GetItemCount(), "")
        self.ctrl.EditLabel(self.ctrl.GetItemCount() - 1)

    def on_remove(self, _, active_item: int = None):
        if self.ctrl.GetItemCount() > 0:
            active_item = self.ctrl.GetFirstSelected() if not active_item else active_item
            self.ctrl.DeleteItem(active_item)

    def get_value(self) -> list[str] | list[tuple[str, ...]]:
        if self.param.headers:
            return [
                tuple(self.ctrl.GetItemText(i, j) for j in range(self.ctrl.GetColumnCount()))
                for i in range(self.ctrl.GetItemCount())
            ]
        return [self.ctrl.GetItemText(i) for i in range(self.ctrl.GetItemCount())]


class ConfigLine(wx.Panel):
    def __init__(self, parent: wx.Window, param: ConfigParam, value: Any, use_sizer: bool = True):
        if use_sizer:
            super().__init__(parent=parent)
            parent = self
        self.param = param

        self.label = CenteredText(parent, label=param.desc, x_center=False)
        self.label.SetSize((100, -1))
        if param.kind == ParamKind.BOOL:
            self.input = wx.CheckBox(parent)
            self.input.SetValue(value)
        elif param.kind == ParamKind.CHOICE:
            assert isinstance(param, ChoiceParam) or isinstance(param, ChoiceParamPlus)
            if isinstance(param, ChoiceParamPlus):
                self.input = wx.Choice(parent, choices=param.choices)
                self.input.Select(param.choices_values.index(value))
            else:
                self.input = wx.ComboBox(parent, choices=param.choices)
                self.input.SetStringSelection(value)
        elif param.kind == ParamKind.BUTTON:
            assert isinstance(param, ButtonParam)
            self.input = wx.Button(parent, label=param.desc)
            # noinspection PyUnresolvedReferences
            self.input.Bind(wx.EVT_BUTTON, lambda _: param.handler())
            self.label.SetLabel("")
            self.input.SetToolTip(param.help_string)
        elif param.kind == ParamKind.COLOR:  # 新增颜色类型处理
            self.input = ColorInputCtrl(parent, value)
        elif param.kind == ParamKind.LIST:
            assert isinstance(param, TableParam)
            self.input = EditableListBox(parent, value, param)
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
            return self.param.default
        elif self.param.kind == ParamKind.COLOR:  # 新增颜色类型处理
            assert isinstance(self.input, ColorInputCtrl)
            return self.input.get_value()
        elif self.param.kind == ParamKind.LIST:
            assert isinstance(self.param, TableParam)
            assert isinstance(self.input, EditableListBox)
            if self.param.headers:
                return self.input.get_value()
            return list(map(self.param.item_type, self.input.get_value()))
        elif self.param.kind == ParamKind.CHOICE:
            assert isinstance(self.param, ChoiceParam) or isinstance(self.param, ChoiceParamPlus)
            assert isinstance(self.input, wx.ComboBox) or isinstance(self.input, wx.Choice)
            if isinstance(self.param, ChoiceParamPlus):
                return self.param.choices_values[self.input.GetSelection()]
            return self.input.GetValue()
        else:
            return self.param.parse_value(self.input.GetValue())


class ConfigEditor(wx.Dialog):
    def __init__(self, parent: wx.Frame, name: str, config: ModuleConfig, cbk: Callable[[dict[str, Any]], None]):
        super().__init__(parent=parent, size=(400, 200), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.callback = cbk
        self.SetTitle(f"插件配置 - {name}")
        self.SetFont(parent.GetFont())
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        for name, param in config.params.items():
            if param.kind == ParamKind.TIP:
                self.sizer.Add(CenteredText(self, label=param.desc, x_center=False), 0, wx.EXPAND)
                self.sizer.AddSpacer(5)
        self.cfg_sizer = wx.FlexGridSizer(len(config), 2, 5, 5)
        self.cfg_sizer.AddGrowableCol(1, 1)
        #self.cfg_sizer.SetFlexibleDirection(wx.HORIZONTAL) #所有水平行高度相等
        self.lines: dict[str, ConfigLine] = {}
        for name, param in config.params.items():
            if param.kind == ParamKind.TIP:
                continue
            line = ConfigLine(self, param, config[name], use_sizer=False)
            if param.kind != ParamKind.LIST:
                line.input.SetMinSize((-1, 28))
            self.cfg_sizer.Add(line.label, 0, wx.EXPAND)
            self.cfg_sizer.Add(line.input, 1, wx.EXPAND)
            self.lines[name] = line
        self.sizer.Add(self.cfg_sizer, 1, wx.EXPAND)
        self.sizer.Add(wx.StaticLine(self, style=wx.LI_HORIZONTAL), 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, 10)

        self.ok_btn = wx.Button(self, label="确定")
        self.cancel_btn = wx.Button(self, label="取消")
        self.apply_btn = wx.Button(self, label="应用")
        self.ok_btn.Bind(wx.EVT_BUTTON, self.on_ok)
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.apply_btn.Bind(wx.EVT_BUTTON, self.on_apply)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.ok_btn, 0)
        btn_sizer.Add(self.cancel_btn, 0)
        btn_sizer.Add(self.apply_btn, 0)
        self.sizer.Add(btn_sizer, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 5)
        out_sizer = wx.BoxSizer(wx.VERTICAL)
        out_sizer.Add(self.sizer, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        self.SetSizer(out_sizer)
        self.Fit()

    def on_ok(self, _):
        self.on_apply(None)
        self.Destroy()

    def on_apply(self, _):
        self.callback({name: line.get_value() for name, line in self.lines.items()})

    def on_cancel(self, _):
        self.Destroy()
