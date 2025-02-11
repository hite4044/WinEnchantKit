from typing import Callable

import wx
from base import *

class ConfigLine(wx.Panel):
    def __init__(self, parent: wx.Window, param: ConfigParam, value: Any):
        super().__init__(parent=parent)
        self.param = param

        self.sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.label = wx.StaticText(self, label=param.desc)
        self.sizer.Add(self.label)
        if param.kind == ParamKind.BOOL:
            self.input = wx.CheckBox(self)
            self.input.SetValue(value)
        else:
            self.input = wx.TextCtrl(self, value=str(value))
        self.sizer.Add(self.input)
        self.sizer.SetMinSize((-1, 25))
        self.SetSizer(self.sizer)

    def get_value(self) -> Any:
        if self.param.kind == ParamKind.BOOL:
            return self.input.GetValue()
        else:
            return self.param.parse_value(self.input.GetValue())


class ConfigEditor(wx.Dialog):
    def __init__(self, parent: wx.Frame, config: ModuleConfig, cbk: Callable[[dict[str, Any]], None]):
        super().__init__(parent=parent, size=(300, 200))
        self.callback = cbk
        self.SetTitle("配置")

        self.sizer = wx.BoxSizer(wx.VERTICAL)
        self.lines: dict[str, ConfigLine] = {}
        for name, param in config.params.items():
            line = ConfigLine(self, param, config[name])
            self.sizer.Add(line)
            self.sizer.AddSpacer(2)
            self.lines[name] = line
        self.sizer.AddSpacer(3)

        self.ok_btn = wx.Button(self, label="确定")
        self.cancel_btn = wx.Button(self, label="取消")
        self.ok_btn.Bind(wx.EVT_BUTTON, self.on_ok)
        self.cancel_btn.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.sizer.Add(self.ok_btn)
        self.sizer.AddSpacer(5)
        self.sizer.Add(self.cancel_btn)
        self.sizer.Fit(self)

        self.SetSizer(self.sizer)

    def on_ok(self, _):
        self.callback({name: line.get_value() for name, line in self.lines.items()})
        self.Destroy()

    def on_cancel(self, _):
        self.Destroy()
