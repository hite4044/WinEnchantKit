import typing

import wx

from base import *
from gui.center_text import CenteredText
from gui.ect_menu import EtcMenu
from gui.editable_listctrl import EditableListCtrl
from gui.font import ft


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


class EditableTable(wx.Panel):
    def __init__(self, parent: wx.Window, data: list, param: TableParam):
        super().__init__(parent=parent)
        self.param = param
        headers = param.headers if param.headers else [("NO_HEADER", 300)]
        style = wx.LC_REPORT | wx.LC_EDIT_LABELS | wx.LC_SINGLE_SEL
        if not param.headers:
            style |= wx.LC_NO_HEADER

        saved_font = self.GetFont()
        self.SetFont(ft(9))
        self.ctrl = EditableListCtrl(self, style=style)
        self.SetFont(saved_font)
        self.add_btn = wx.Button(self.ctrl, label="+", size=(25, 25))
        self.remove_btn = wx.Button(self.ctrl, label="-", size=(25, 25))

        for head, width in headers:
            self.ctrl.AppendColumn(head, width=width)
            self.ctrl.EnableColumnEdit(self.ctrl.GetColumnCount() - 1)
        self.update_data(data)
        param.update_handler = self.update_data

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
        self.ctrl.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.on_menu)
        self.ctrl.Bind(wx.EVT_RIGHT_DOWN, self.on_menu)

    def update_data(self, data: Any):
        self.ctrl.DeleteAllItems()
        for i, item in enumerate(data):
            if not isinstance(item, list):
                self.ctrl.InsertItem(i, str(item))
            else:
                self.ctrl.InsertItem(i, str(item[0]))
                for j, content in enumerate(item[1:] + list(self.param.default_line)[len(item):]):
                    if isinstance(content, bool):
                        self.ctrl.SetItem(i, j + 1, "T" if content else "F")
                    else:
                        self.ctrl.SetItem(i, j + 1, str(content))

    def on_menu(self, event: wx.ListEvent | wx.MouseEvent):
        if isinstance(event, wx.MouseEvent):
            row, flag = self.ctrl.HitTest(event.GetPosition())
            if row != -1:
                event.Skip()
                return
        menu = EtcMenu()
        menu.Append("添加", self.on_add)
        try:
            index = event.GetIndex()
            menu.Append("删除", self.on_remove, None, index)
        except AttributeError:
            pass
        if self.param.pre_def_data:
            menu.AppendSeparator()
            for text, data in self.param.pre_def_data.items():
                menu.Append(text, self.add_pre_data, data)
        self.PopupMenu(menu)

    def add_pre_data(self, data: list[Any]):
        self.ctrl.InsertItem(self.ctrl.GetItemCount(), data[0])
        for i, item in enumerate(data[1:] + list(self.param.default_line)[len(data):]):
            self.ctrl.SetItem(self.ctrl.GetItemCount() - 1, i + 1, str(item))

    def on_add(self):
        self.ctrl.InsertItem(self.ctrl.GetItemCount(), "")
        if self.param.default_line:
            for i, item in enumerate(self.param.default_line):
                self.ctrl.SetItem(self.ctrl.GetItemCount() - 1, i, str(item))
        self.ctrl.EditLabel(self.ctrl.GetItemCount() - 1)

    def on_remove(self, _, active_item: int = None):
        if self.ctrl.GetItemCount() > 0:
            active_item = self.ctrl.GetFirstSelected() if not active_item else active_item
            self.ctrl.DeleteItem(active_item)

    def get_value(self) -> list[Any] | list[list[Any]]:
        data: list[list[str | int | float | bool | None]] = [
            [typing.cast(str, self.ctrl.GetItemText(i, j)) for j in range(self.ctrl.GetColumnCount())]
            for i in range(self.ctrl.GetItemCount())
        ]
        for row in data:
            for i, item in enumerate(row):
                item_type = self.param.item_types[i]
                if item_type in [int, float]:
                    try:
                        row[i] = item_type(item)
                    except ValueError:
                        row[i] = None
                elif item_type == bool:
                    row[i] = item in ["T", "True", "true"]
        if not self.param.headers:
            return [item[0] for item in data]
        return data


class ConfigLine(wx.Panel):
    def __init__(self, parent: wx.Window, param: ConfigParam, value: Any, use_sizer: bool = True):
        if use_sizer:
            super().__init__(parent=parent)
            parent = self
        self.parent = parent
        self.param = param

        self.label = CenteredText(parent, label=param.desc, x_center=False)
        self.label.SetSize((100, -1))
        if param.kind == ParamKind.BOOL:
            self.input = wx.CheckBox(parent)
            self.input.SetValue(value)
        elif param.kind == ParamKind.CHOICE:
            if isinstance(param, ChoiceParamPlus):
                assert isinstance(param, ChoiceParamPlus)
                self.input = wx.Choice(parent, choices=param.choices)
                self.input.Select(param.choices_values.index(value))
            else:
                assert isinstance(param, ChoiceParam)
                self.input = wx.ComboBox(parent, choices=param.choices)
                self.input.SetStringSelection(value)
        elif param.kind == ParamKind.BUTTON:
            assert isinstance(param, ButtonParam)
            self.input = wx.Button(parent, label=param.desc)
            # noinspection PyUnresolvedReferences
            self.input.Bind(wx.EVT_BUTTON, lambda _: param.handler())
            self.label.SetLabel("")
        elif param.kind == ParamKind.COLOR:  # 新增颜色类型处理
            self.input = ColorInputCtrl(parent, value)
        elif param.kind == ParamKind.LIST:
            assert isinstance(param, TableParam)
            self.input = EditableTable(parent, value, param)
        else:
            self.input = wx.TextCtrl(parent, value=str(value))
        wx.ToolTip.Enable(True)
        wx.ToolTip.SetAutoPop(1000 * 60)  # 持续时间
        wx.ToolTip.SetMaxWidth(114514)  # 最大宽度

        tooltip = wx.ToolTip(param.help_string)
        tooltip2 = wx.ToolTip(param.help_string)

        self.label.Bind(wx.EVT_LEFT_DOWN, self.pop_help_string_wnd)
        self.label.SetToolTip(tooltip)
        self.input.SetToolTip(tooltip2)
        if use_sizer:
            self.sizer = wx.BoxSizer(wx.HORIZONTAL)
            self.sizer.Add(self.label, 0, wx.EXPAND)
            self.sizer.Add(self.input, 1, wx.EXPAND)
            self.sizer.SetMinSize((-1, 25))
            self.SetSizer(self.sizer)

    def pop_help_string_wnd(self, event: wx.MouseEvent):
        event.Skip()
        if not self.param.help_string:
            return
        wnd = wx.Frame(self.parent, title="参数提示")
        wnd.SetFont(self.parent.GetFont())
        wnd.SetBackgroundColour(self.parent.GetBackgroundColour())
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(wnd, label=self.param.help_string))
        wnd.SetSizer(sizer)
        wnd.Fit()
        wnd.Show()

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
            assert isinstance(self.input, EditableTable)
            return self.input.get_value()
        elif self.param.kind == ParamKind.CHOICE:
            assert isinstance(self.param, ChoiceParam) or isinstance(self.param, ChoiceParamPlus)
            assert isinstance(self.input, wx.ComboBox) or isinstance(self.input, wx.Choice)
            if isinstance(self.param, ChoiceParamPlus):
                return self.param.choices_values[self.input.GetSelection()]
            return self.input.GetValue()
        else:
            return self.param.parse_value(self.input.GetValue())


def get_line_height(parent: wx.Window):
    entry = wx.TextCtrl(parent)
    entry.Show()
    height = entry.GetSize()[1]
    entry.Destroy()
    return height


class ConfigEditor(wx.Dialog):
    def __init__(self, parent: wx.Frame, name: str, config: ModuleConfig, cbk: Callable[[dict[str, Any]], None]):
        super().__init__(parent=parent, size=(400, 200), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.callback = cbk
        self.SetTitle(f"插件配置 - {name}")
        self.SetFont(parent.GetFont())
        CFG_LINE_HEIGHT = get_line_height(self)
        self.sizer = wx.BoxSizer(wx.VERTICAL)
        for name, param in config.params.items():
            if param.kind == ParamKind.TIP:
                self.sizer.Add(CenteredText(self, label=param.desc, x_center=False), 0, wx.EXPAND)
                self.sizer.AddSpacer(5)
        self.cfg_sizer = wx.FlexGridSizer(len(config), 2, 5, 5)
        self.cfg_sizer.AddGrowableCol(1, 1)
        self.lines: dict[str, ConfigLine] = {}
        current_row = 0
        for name, param in config.params.items():
            if param.kind == ParamKind.TIP:
                continue
            line = ConfigLine(self, param, config[name], use_sizer=False)
            if param.kind != ParamKind.LIST:
                line.input.SetMinSize((-1, CFG_LINE_HEIGHT))
            self.cfg_sizer.Add(line.label, 0, wx.EXPAND)
            self.cfg_sizer.Add(line.input, 1, wx.EXPAND)
            if isinstance(line.input, EditableTable):
                self.cfg_sizer.AddGrowableRow(current_row, 1)
            self.lines[name] = line
            current_row += 1
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
