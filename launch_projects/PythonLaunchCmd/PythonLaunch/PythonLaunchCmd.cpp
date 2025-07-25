#include <Windows.h>
#include <string>
#include <vector>
#include <Shlwapi.h>
#include <shellapi.h>

#pragma comment(lib, "Shlwapi.lib")
#pragma comment(lib, "Shell32.lib")

// 错误处理宏，显示消息框并退出程序
#define ERROR_EXIT(msg) { \
    MessageBoxW(NULL, msg, L"错误", MB_OK | MB_ICONERROR); \
    return 1; \
}

int main() {
    // 获取当前可执行文件路径
    wchar_t exePath[MAX_PATH];
    if (!GetModuleFileNameW(NULL, exePath, MAX_PATH)) {
        ERROR_EXIT(L"无法获取可执行文件路径");
    }

    // 提取目录并切换到program子目录
    PathRemoveFileSpecW(exePath);
    std::wstring programDir = std::wstring(exePath) + L"\\program";

    if (!SetCurrentDirectoryW(programDir.c_str())) {
        std::wstring errorMsg = L"无法切换到工作目录:\n" + programDir;
        ERROR_EXIT(errorMsg.c_str());
    }

    // 获取当前工作目录
    wchar_t currentDir[MAX_PATH];
    GetCurrentDirectoryW(MAX_PATH, currentDir);

    // 获取命令行参数
    int argc;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv || argc < 1) {
        ERROR_EXIT(L"命令行参数解析失败");
    }

    // 构建Python可执行文件路径
    std::wstring pythonExe = L"runtime\\python.exe";

    // 检查Python可执行文件是否存在
    if (GetFileAttributesW(pythonExe.c_str()) == INVALID_FILE_ATTRIBUTES) {
        std::wstring errorMsg = L"找不到Python解释器:\n" + pythonExe;
        ERROR_EXIT(errorMsg.c_str());
    }

    // 构建完整参数：main.py + 程序参数
    std::wstring parameters = L"\"main.py\"";
    for (int i = 1; i < argc; i++) {
        parameters += L" \"";
        parameters += argv[i];
        parameters += L"\"";
    }

    // 准备进程启动信息 - 关键修复点
    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi;
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_SHOW;  // 显示控制台窗口（共享父控制台）

    // 创建可写缓冲区
    std::wstring commandLine = L"\"" + pythonExe + L"\" " + parameters;
    std::vector<wchar_t> cmdLineBuffer(commandLine.begin(), commandLine.end());
    cmdLineBuffer.push_back(L'\0');

    // 使用CreateProcess创建进程并继承控制台 - 解决新窗口问题
    if (!CreateProcessW(
        pythonExe.c_str(),      // 可执行文件路径
        cmdLineBuffer.data(),   // 命令行参数
        NULL,                   // 进程安全属性
        NULL,                   // 线程安全属性
        TRUE,                   // 继承句柄，关键参数！使控制台输出可见
        NORMAL_PRIORITY_CLASS,  // 默认优先级
        NULL,                   // 使用父进程环境
        currentDir,             // 工作目录
        &si,                    // 启动信息
        &pi                     // 进程信息
    )) {
        std::wstring errorMsg = L"无法启动子进程:\n" + pythonExe + L" " + parameters;
        ERROR_EXIT(errorMsg.c_str());
    }

    // 等待子进程退出
    WaitForSingleObject(pi.hProcess, INFINITE);

    // 获取退出码
    DWORD exitCode;
    GetExitCodeProcess(pi.hProcess, &exitCode);

    // 清理资源
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    LocalFree(argv);

    return (int)exitCode;
}