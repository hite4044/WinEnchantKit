import logging

from base import *

name = "Hite404工具插件模版"
logger = logging.getLogger("WinEnchantKitLogger_plugin_temp")


class PluginConfig(ModuleConfigPlus):
    def __init__(self):
        super().__init__()
        self.tip: TipParam | str = TipParam("提示")
        self.int: IntParam | int = IntParam(0, "整数")
        self.float: FloatParam | float = FloatParam(0.0, "浮点数")
        self.bool: BoolParam | bool = BoolParam(False, "布尔值")
        self.string: StringParam | str = StringParam("", "字符串")
        self.choice_old: ChoiceParam | str = ChoiceParam(0, ["0", "1"], "旧版选择 (字符串)")
        self.choice: ChoiceParamPlus | int = ChoiceParamPlus(0, {0: "0", 1: "1"}, "选择")
        self.list: ListParam | list[str] = ListParam(["0", "1"], "列表")
        self.load()


class Plugin(BasePlugin):
    config = PluginConfig()

    def start(self):
        logger.info("Starting plugin")

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        pass

    def stop(self):
        pass
