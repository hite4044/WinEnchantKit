import atexit
import logging
from collections import namedtuple
from threading import Thread
from time import sleep, perf_counter

import psutil
import pynvml
from win32con import MB_OK, MB_ICONWARNING
from win32gui import GetForegroundWindow, MessageBox, GetClassName
from win32process import GetWindowThreadProcessId

from base import *

name = "MC录屏提示"
logger = logging.getLogger("WinEnchantKitLogger_mc_record_alert")
try:
    pynvml.nvmlInit()
    atexit.register(pynvml.nvmlShutdown)
except pynvml.NVMLError:
    pass

ProcessGPUsage = namedtuple("ProcessGPUsage", ["pid", "smUtil", "memUtil", "encUtil", "decUtil"])


def get_proc_gpu_perf(pid: int):
    handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    for info in pynvml.nvmlDeviceGetProcessUtilization(handle, 0):
        if info.pid == pid:
            return ProcessGPUsage(
                pid=pid,
                smUtil=info.smUtil,
                memUtil=info.memUtil,
                encUtil=info.encUtil,
                decUtil=info.decUtil,
            )


def is_minecraft_window(hwnd: int) -> bool:
    if GetClassName(hwnd) == "GLFW30":
        _, window_proc_id = GetWindowThreadProcessId(hwnd)
        window_proc = psutil.Process(window_proc_id)
        if window_proc.name() == "javaw.exe":
            return True
    return False


class Plugin(BasePlugin):
    config = ModuleConfig(
        {
            "alert_time": IntParam(60, "提醒启动OBS录屏的时间，单位为秒: "),
            "check_inv": IntParam(10, "检查窗口的间隔时间，单位为秒: "),
            "alert_always": BoolParam(False, "提醒过后是否继续提醒"),
            "usage_thr": IntParam(2, "OBS录屏GPU占用阈值(包含)，单位为百分比: "),
            "obs_name": StringParam("obs64.exe", "OBS进程名 (不建议改动): "),
        }
    )
    thread = None
    running_flag = False
    enable = True

    def start(self):
        self.running_flag = True
        self.thread = Thread(target=self.thread_func, daemon=True)
        self.thread.start()

    def update_config(self, old_config: dict[str, Any], new_config: dict[str, Any]):
        self.config.load_values(new_config)
        if self.enable:
            self.stop()
            self.start()

    def stop(self):
        self.running_flag = False
        self.thread.join()
        self.enable = False

    def check_obs_recorded(self) -> bool:
        """检查OBS是否正在录屏"""
        for proc in psutil.process_iter(["name"]):
            if proc.name() == self.config["obs_name"]:
                usage = get_proc_gpu_perf(proc.pid)
                if usage is None:
                    return False
                if usage.encUtil >= self.config["usage_thr"]:
                    return True
                break
        return False

    def thread_func(self):
        minecraft_running = False
        obs_non_launch_timer = 0
        alerted = False
        try:
            logger.info(f"[{name}]: " + "线程已启动")
            while self.running_flag:
                for _ in range(int(self.config["check_inv"] / 0.5)):
                    sleep(0.5)
                    if not self.running_flag:
                        break
                if not self.running_flag:
                    break

                hwnd = GetForegroundWindow()
                if hwnd == 0:
                    continue
                if is_minecraft_window(hwnd):  # 正在游玩MC
                    if not minecraft_running:  # MC刚刚启动
                        logger.info(f"[{name}]: " + "正在游玩MC")
                        minecraft_running = True
                        obs_non_launch_timer = perf_counter()
                        alerted = False
                    else:  # MC已经启动
                        if alerted:
                            continue
                        logger.info(f"[{name}]: " + f"MC已游玩 {round(perf_counter() - obs_non_launch_timer, 2)} 秒")
                        if perf_counter() - obs_non_launch_timer > self.config["alert_time"]:
                            if not self.check_obs_recorded():
                                logger.info(f"[{name}]: " + "检测到OBS仍未启动，弹出警告窗口...")
                                MessageBox(hwnd, "检测到OBS未启动，请启动OBS", "警告", MB_OK | MB_ICONWARNING)
                                sleep(2)
                                obs_non_launch_timer = perf_counter()
                                if not self.config["alert_always"]:
                                    alerted = True
                            else:
                                logger.info(f"[{name}]: " + "检测到OBS已启动，不弹出警告窗口")
                                alerted = True

                elif minecraft_running:  # 没有在游玩MC
                    logger.info(f"[{name}]: " + "没有在游玩MC")
                    minecraft_running = False
                    obs_non_launch_timer = 0
                    alerted = False
        except KeyboardInterrupt:
            pass
        logger.info(f"[{name}]: " + "线程已退出")
