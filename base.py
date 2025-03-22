""""""
from copy import copy
from enum import Enum
from typing import Any, Type


class ParamKind(Enum):
    STRING = 0
    BOOL = 1
    INT = 2
    FLOAT = 3
    CHOICE = 4
    BUTTON = 5
    TIP = 6
    COLOR = 7


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


class ButtonParam(ConfigParam):
    def __init__(self, handler: callable, desc: str):
        super().__init__(ParamKind.BUTTON, True, bool, desc)
        self.handler = handler


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


class BasePlugin:
    config = ModuleConfig({})
    enable = False

    def start(self):
        pass

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        pass

    def stop(self):
        pass
