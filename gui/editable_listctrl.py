import wx
from wx.lib.mixins.listctrl import TextEditMixin


class CustomMixin(TextEditMixin):
    """延迟500ms编辑框的打开"""
    def __init__(self):
        TextEditMixin.__init__(self)
        self.open_editor_call = None

    def OnLeftDown(self, evt=None):
        assert isinstance(self, wx.ListCtrl)
        evt.Skip()
        self.SetFocus()
        evt.Skip = lambda *args: None
        super().OnLeftDown(evt)

    def OpenEditor(self, col, row):
        self.open_editor_call = wx.CallLater(500, self.open_editor_warp, col, row)

    def open_editor_warp(self, col, row):
        assert isinstance(self, wx.ListCtrl)
        now_row, flags = self.HitTest(self.ScreenToClient(wx.GetMousePosition()))
        assert isinstance(self, CustomMixin)
        if now_row != row:
            return
        self.make_editor()
        super().OpenEditor(col, row)
        self.editor.SetFocus()

    def CloseEditor(self, evt=None):
        if self.open_editor_call:
            self.open_editor_call.Stop()
            self.open_editor_call = None
        TextEditMixin.CloseEditor(self, evt)


class EditableListCtrl(wx.ListCtrl, CustomMixin):
    """支持多列编辑的 ListCtrl 子类"""

    def __init__(self, parent, ID=wx.ID_ANY, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=wx.LC_REPORT):
        wx.ListCtrl.__init__(self, parent, ID, pos, size, style)
        self.editorBgColour = self.GetBackgroundColour()
        CustomMixin.__init__(self)

        # 绑定事件
        self.Bind(wx.EVT_LIST_BEGIN_LABEL_EDIT, self._onBeginEdit)
        self.Bind(wx.EVT_LIST_END_LABEL_EDIT, self._onEndEdit)

        # 存储每列的编辑状态
        self.editable_columns: set[int] = set()

    def EnableColumnEdit(self, col):
        """启用指定列的编辑功能"""
        self.editable_columns.add(col)

    def DisableColumnEdit(self, col):
        """禁用指定列的编辑功能"""
        if col in self.editable_columns:
            self.editable_columns.remove(col)

    def _onBeginEdit(self, event):
        """开始编辑前检查列是否可编辑"""
        col = event.GetColumn()
        if col not in self.editable_columns:
            event.Veto()  # 阻止编辑不可编辑的列
        else:
            event.Skip()

    def _onEndEdit(self, event):
        """结束编辑时处理数据"""
        if not event.IsEditCancelled():
            row = event.GetIndex()
            col = event.GetColumn()
            new_text = event.GetLabel()

            # 这里可以添加自定义数据验证
            # 例如：验证通过则更新数据，否则取消编辑
            if self.validate_cell(row, col, new_text):
                self.SetItem(row, col, new_text)
                # 可在此处添加保存到数据源的逻辑
            else:
                wx.Bell()  # 无效输入提示
                event.Veto()

    @staticmethod
    def validate_cell(row, col, value):
        """自定义数据验证（子类可重写此方法）"""
        return True
