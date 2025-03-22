import logging

from base import *

name = "Hite404工具插件模版"
logger = logging.getLogger("WinEnchantKitLogger_plugin_temp")


class Plugin(BasePlugin):
    config = ModuleConfig({
        "test": StringParam("test", "test"),
        "test2": IntParam(1, "test2"),
        "test3": FloatParam(1.0, "test3"),
        "test4": BoolParam(True, "test4"),
    })

    def start(self):
        logger.info("Starting plugin")

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        pass

    def stop(self):
        pass
