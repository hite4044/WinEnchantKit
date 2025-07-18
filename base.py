""""""
from copy import copy
from enum import Enum
from typing import Any, Type, Callable


class ParamKind(Enum):
    STRING = 0
    BOOL = 1
    INT = 2
    FLOAT = 3
    CHOICE = 4
    BUTTON = 5
    TIP = 6
    COLOR = 7
    LIST = 8


class ConfigParam:
    def __init__(self, kind: ParamKind, default: Any, type_: Type[Any], desc: str):
        self.kind = kind
        self.desc = desc
        self.default = default
        self.type = type_

    def parse_value(self, value: Any) -> Any | None:
        try:
            return self.type(value)
        except ValueError:
            return None


class TipParam(ConfigParam):
    def __init__(self, desc: str):
        super().__init__(ParamKind.TIP, True, bool, desc)


class ColorParam(ConfigParam):
    def __init__(self, default: tuple[int, int, int], desc: str):
        super().__init__(ParamKind.COLOR, default, tuple, desc)


class StringParam(ConfigParam):
    def __init__(self, default: str, desc: str):
        super().__init__(ParamKind.STRING, default, str, desc)


class IntParam(ConfigParam):
    def __init__(self, default: int, desc: str):
        super().__init__(ParamKind.INT, default, int, desc)


class BoolParam(ConfigParam):
    def __init__(self, default: bool, desc: str):
        super().__init__(ParamKind.BOOL, default, bool, desc)


class FloatParam(ConfigParam):
    def __init__(self, default: float, desc: str):
        super().__init__(ParamKind.FLOAT, default, float, desc)


class ChoiceParam(ConfigParam):
    def __init__(self, default: Any, choices: list[Any], desc: str):
        super().__init__(ParamKind.CHOICE, default, str, desc)
        self.choices = choices


class ChoiceParamPlus(ConfigParam):
    def __init__(self, default: Any, choices: dict[Any, str], desc: str):
        super().__init__(ParamKind.CHOICE, default, str, desc)
        self.choices = list(choices.values())
        self.choices_values = list(choices.keys())


class ButtonParam(ConfigParam):
    def __init__(self, handler: Callable[[], Any] = lambda: None, desc: str = "", help_string: str = ""):
        super().__init__(ParamKind.BUTTON, True, bool, desc)
        self.handler: Callable[[], Any] = handler
        self.help_string = help_string


class TableParam(ConfigParam):
    def __init__(self, default: list[tuple] = None, desc: str = "", item_type: Type[Any] = str,
                 headers: list[tuple[str, int]] = None):
        if default is None:
            default = []
        super().__init__(ParamKind.LIST, default, list, desc)
        self.headers = headers
        self.item_type = item_type


class ListParam(TableParam):
    def __init__(self, default: list = None, desc: str = "", item_type: Type[Any] = str):
        if default is None:
            default = []
        super().__init__(default, desc, item_type)


param_kind_map = {
    ParamKind.STRING: StringParam,
    ParamKind.INT: IntParam,
    ParamKind.BOOL: BoolParam,
    ParamKind.FLOAT: FloatParam,
    ParamKind.CHOICE: ChoiceParam,
}


class ModuleConfig(dict):
    def __init__(self, params: dict[str, ConfigParam]):
        super().__init__()
        self.params: dict[str, ConfigParam] = params
        self.update({copy(key): copy(param.default) for key, param in params.items()})

    def load_values(self, data: dict[str, Any]):
        self.update({copy(key): copy(data[key]) for key in data if key in self.params})


class ModuleConfigPlus(ModuleConfig):
    # noinspection PyMissingConstructor
    def __init__(self):
        dict.__init__(self)
        self.names = []
        self.end_collection = False

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        if hasattr(self, "end_collection") and not self.end_collection and isinstance(value, ConfigParam):
            self.names.append(key)

    def load(self):
        self.end_collection = True
        params = self.find_params()
        self.params = params
        self.update({copy(key): copy(param.default) for key, param in params.items()})

    def update(self, m, /, **kwargs):
        super().update(m, **kwargs)
        for key, value in m.items():
            setattr(self, key, value)

    def find_params(self):
        return {name: getattr(self, name) for name in self.names}


class BasePlugin:
    config = ModuleConfig({})
    enable = False

    def start(self):
        pass

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        self.config.load_values(new_config)

    def stop(self):
        pass
