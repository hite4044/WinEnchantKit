from copy import copy
from enum import Enum
from typing import Any, Type


class ParamKind(Enum):
    STRING = 0
    BOOL = 1
    INT = 2
    FLOAT = 3


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


param_kind_map = {
    ParamKind.STRING: StringParam,
    ParamKind.INT: IntParam,
    ParamKind.BOOL: BoolParam,
    ParamKind.FLOAT: FloatParam,
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

    def start(self):
        pass

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        pass

    def stop(self):
        pass
