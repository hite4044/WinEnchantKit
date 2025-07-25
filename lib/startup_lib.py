import os
import sys
import winreg
from os.path import expandvars

import win32con as con
from win32comext.shell.shell import ShellExecuteEx

from lib.env import IS_PACKAGE_ENV

cmd_temp = " ".join(
    [
        "schtasks",
        "/create",
        "/tn WinEnchantKit_Startup",  # 名称
        "/ru Administrators",  # 用户
        "/rl HIGHEST",  # 以最高权限运行
        "/sc ONLOGON",  # 登录时
        "/F",  # 覆盖
        "/tr \"{}\"",  # 程序
    ]
)


def get_auto_startup_cmd() -> str:
    if IS_PACKAGE_ENV:
        parent_dir = os.path.split(os.getcwd())[0]
        args = [os.path.join(parent_dir, "WinEnchantKit.exe"), *sys.argv[1:]]
    else:
        args = [sys.executable.replace("python.exe", "pythonw.exe")] + sys.argv
    if "-startup" not in args:
        args.append("-startup")
    return " ".join(args)


def create_task():
    """
    创建开机启动 - 任务计划
    """
    os.makedirs(expandvars("%APPDATA%\\WinEnchantKit"), exist_ok=True)
    if IS_PACKAGE_ENV:
        startup_file = get_auto_startup_cmd()
    else:
        startup_file = expandvars("%APPDATA%\\WinEnchantKit\\startup.bat")
        with open(startup_file, "w", encoding="gbk") as f:
            f.write("\n".join([
                f"cd /D {os.getcwd()}",
                get_auto_startup_cmd()
            ]))

    cmd = cmd_temp.format(startup_file)
    task_add_bat = expandvars("%TEMP%\\WinEnchantKit_task_add.bat")
    with open(task_add_bat, "w", encoding="gbk") as f:
        f.write(cmd)

    ShellExecuteEx(lpVerb="runas", lpFile=task_add_bat, nShow=con.SW_HIDE)


def remove_task():
    cmd = "schtasks /delete /tn WinEnchantKit_Startup /f"
    task_remove_bat = expandvars("%TEMP%\\WinEnchantKit_task_remove.bat")
    with open(task_remove_bat, "w", encoding="gbk") as f:
        f.write(cmd)

    ShellExecuteEx(lpVerb="runas", lpFile=task_remove_bat, nShow=con.SW_HIDE)


def create_reg():
    cmd = get_auto_startup_cmd()
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_ALL_ACCESS)
    winreg.SetValueEx(key, "WinEnchantKit", 0, winreg.REG_SZ, cmd)


def remove_reg():
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_ALL_ACCESS)
    winreg.DeleteValue(key, "WinEnchantKit")


if __name__ == "__main__":
    create_task()
