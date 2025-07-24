from bisect import bisect_left
from dataclasses import dataclass
from enum import Enum
from time import perf_counter

import wx

Num = int | float


class Animation:
    def __init__(self, during: float, range_: tuple[Num, Num] = None):
        self.during = during
        self.range = range_
        self.is_invent = False
        self.has_start = False
        self.has_finish = False

        self.playing_start = -1

    def set_invent(self, invent: bool):
        self.is_invent = invent

    def play(self):
        self.playing_start = perf_counter()
        self.has_finish = False

    def stop(self):
        self.playing_start = -1
        self.has_finish = True

    @property
    def is_playing(self) -> bool:
        return self.playing_start != -1 and not self.has_finish

    @property
    def raw_percent(self):
        return (perf_counter() - self.playing_start) / self.during

    @property
    def value(self) -> float:
        if self.is_playing:
            percent = self.raw_percent
            if not self.has_finish and percent > 1:
                self.has_finish = True
                percent = 1
            else:
                percent = min(percent, 1)
        elif self.has_finish:
            percent = 1
        else:
            percent = 0
        return self.raw_get_value(percent)

    def get_next_frame_time(self, fps: float):
        return self.during

    @property
    def int_value(self) -> int:
        return int(self.value)

    def raw_get_value(self, percent: float) -> float:
        raise NotImplementedError()


class BlinkAnimation(Animation):
    def __init__(self, range_: tuple[Num, Num], threshold: float):
        super().__init__(0, range_)
        self.threshold = threshold

    def raw_get_value(self, percent: float) -> Num:
        return self.range[1] if percent > self.threshold else self.range[0]


class KeyFrameWay(Enum):
    BLINK = 0  # 突然闪现
    SMOOTH = 1  # 平滑匀速运动

    EASE_IN = 2  # 缓入
    QUADRATIC_EASE_IN = 3  # 二次方缓入
    CUBE_EASE_IN = 4  # 三次方缓入

    EASE_OUT = 5  # 缓出
    QUADRATIC_EASE_OUT = 6  # 二次方缓出
    CUBE_EASE_OUT = 7  # 三次方缓出

    QUADRATIC_EASE = 8  # 二次方缓动
    CUBE_EASE = 9  # 三次方缓动


@dataclass
class KeyFrame:
    way: KeyFrameWay
    percent: float
    data: float


class KeyFrameAnimation(Animation):
    def __init__(self, during: float, key_frames: list[KeyFrame]):
        super().__init__(during, None)
        self.percents: list[float] = sorted((key_frame.percent for key_frame in key_frames))
        self.key_frames: list[KeyFrame] = sorted((key_frame for key_frame in key_frames), key=lambda x: x.percent)
        if 0 not in self.percents:
            self.key_frames.insert(0, KeyFrame(KeyFrameWay.BLINK, 0, self.key_frames[0].data))
            self.percents.insert(0, 0)
        if 1 not in self.percents:
            self.key_frames.append(KeyFrame(KeyFrameWay.BLINK, 1, self.key_frames[-1].data))
            self.percents.append(1)

        self.raw_range = (self.key_frames[0].data, self.key_frames[-1].data)
        self.percent_offset = 0
        self.raw_during = float(self.during)

    def play(self):
        super().play()
        print(f"Playing Animation: During: {self.during}, percent_offset: {self.percent_offset}")

    def get_next_frame_time(self, fps: float):
        frame_time = 1 / fps
        crt_time = perf_counter()
        if crt_time + frame_time > self.playing_start + self.during:
            return self.playing_start + self.during - crt_time
        return frame_time

    def raw_get_value(self, percent: float) -> float:
        if self.is_invent:
            percent = 1 - percent
        percent = min(max(percent * (1 - self.percent_offset), 0), 1)
        index = bisect_left(self.percents, percent) - 1
        frame = self.key_frames[index]

        start = frame.data

        if index >= len(self.percents) - 1:
            size = 0
            local_percent = 1
        else:
            next_frame = self.key_frames[index + 1]
            size = next_frame.data - frame.data
            local_percent = (percent - frame.percent) / (next_frame.percent - frame.percent)

        match frame.way:
            case KeyFrameWay.BLINK:
                return start
            case KeyFrameWay.SMOOTH:
                return start + size * local_percent
            case KeyFrameWay.QUADRATIC_EASE:
                if local_percent < 0.5:
                    eased = 2 * (local_percent ** 2)
                else:
                    eased = -1 + 4 * local_percent - 2 * (local_percent ** 2)
                return start + size * eased
            case KeyFrameWay.CUBE_EASE:
                if local_percent < 0.5:
                    eased = 4 * (local_percent ** 3)
                else:
                    eased = 1 - ((-2 * local_percent + 2) ** 3) / 2
                return start + size * eased
        raise NotImplementedError()

    def set_invent(self, invent: bool):
        super().set_invent(invent)
        if not self.is_playing:
            return

        raw_percent = (perf_counter() - self.playing_start) / self.raw_during
        self.percent_offset = 1 - raw_percent
        if invent:
            self.during = self.raw_during * raw_percent
        else:
            self.during = self.raw_during * (1 - raw_percent)
        self.playing_start = perf_counter()
        # print(f"Set Animation Invent {invent}: percent: {raw_percent},\n during: {self.during},\n percent_offset: {self.percent_offset}")

    def stop(self):
        self.during = float(self.raw_during)
        self.percent_offset = 0
        super().stop()


class EZKeyFrameAnimation(KeyFrameAnimation):
    def __init__(self, during: float, way: KeyFrameWay, start: float, end: float):
        super().__init__(during, [KeyFrame(way, 0, 0.0), KeyFrame(way, 1, 1.0)])
        self.start = start
        self.end = end

    def set_range(self, start: float, end: float):
        self.start = start
        self.end = end

    @property
    def value(self) -> float:
        return super().value * (self.end - self.start) + self.start


class ColorGradationAnimation(KeyFrameAnimation):
    def __init__(self, during: float, color1: wx.Colour, color2: wx.Colour, key_frames: list[KeyFrame]):
        super().__init__(during, key_frames)
        self.color1 = color1
        self.color2 = color2

    def set_color(self, color1: wx.Colour, color2: wx.Colour):
        self.color1 = color1
        self.color2 = color2

    @property
    def value(self) -> wx.Colour:
        percent = super().value
        new_rgba = (self.color1.Red() * (1 - percent) + self.color2.Red() * percent,
                    self.color1.Green() * (1 - percent) + self.color2.Green() * percent,
                    self.color1.Blue() * (1 - percent) + self.color2.Blue() * percent,
                    self.color1.Alpha() * (1 - percent) + self.color2.Alpha() * percent)
        new_rgba = tuple(int(x) for x in new_rgba)
        return wx.Colour(new_rgba)


class MutilKeyFrameAnimation(Animation):
    def __init__(self, during: float, animations: dict[str, KeyFrameAnimation]):
        super().__init__(during)
        self.animations = animations
        self.playing_anim: KeyFrameAnimation | None = None

    @property
    def is_playing(self) -> bool:
        return self.playing_anim.is_playing

    def get_next_frame_time(self, fps: float):
        return self.playing_anim.get_next_frame_time(fps)

    def set_invent(self, invent: bool):
        super().set_invent(invent)
        self.playing_anim.set_invent(invent)

    def set_sub_anim(self, name: str):
        if self.playing_anim != self.animations[name]:
            for anim in self.animations.values():
                anim.stop()
        self.playing_anim = self.animations[name]

    def play(self):
        return self.playing_anim.play()

    def stop(self):
        return self.playing_anim.stop()

    @property
    def value(self):
        return self.playing_anim.value


class AnimationGroup(Animation):
    def __init__(self, group: dict[str, Animation]):
        super().__init__(1.0)
        self.animations = group

    def play(self):
        for animation in self.animations.values():
            animation.play()
        self.during = max(animation.during for animation in self.animations.values())
        super().play()

    def stop(self):
        for animation in self.animations.values():
            animation.stop()
        super().stop()

    def set_invent(self, invent: bool):
        for animation in self.animations.values():
            animation.is_invent = invent
        super().set_invent(invent)

    @property
    def is_playing(self) -> bool:
        return any(animation.is_playing for animation in self.animations.values())

    @property
    def value(self) -> object:
        raise NotImplementedError


def full_keyframe(way: KeyFrameWay):
    return [
        KeyFrame(way, 0, 0.0),
        KeyFrame(way, 1, 1.0)
    ]
