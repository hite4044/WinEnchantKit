from time import perf_counter
from typing import Union


def ms(n1: float, n2: float):
    return round((n2 - n1) * 1000, 4)


class Counter:
    def __init__(self, create_start: bool = False):
        self.timers: dict[str, float] = {}
        self.results: dict[str, float] = {}
        self.local_timer = perf_counter()
        if create_start:
            self.start()

    def start(self, *names: str) -> Union[None, 'Counter']:
        if names:
            for name in names:
                self.timers[name] = perf_counter()
        else:
            self.local_timer = perf_counter()
            return self

    def end_start(self, end: str, start: str):
        result = self.end(end)
        self.start(start)
        return result

    def end(self, name: str | None = None) -> float:
        if name in self.timers:
            self.results[name] = perf_counter() - self.timers.pop(name)
            return self.results[name]
        elif name in self.results:
            return self.results[name]
        elif name is None:
            temp = perf_counter() - self.local_timer
            self.local_timer = 0
            return temp
        else:
            raise KeyError(f"Timer {name} does not exist")

    def endT(self, name: str | None = None):
        ret = self.end(name)
        return f"{ret * 1000:.3f} ms"

    def __str__(self):
        return "\n".join(
            f"{n}: {v * 1000:.3f} ms" for n, v in {**self.results, "##Local##": self.local_timer}.items()
        )
