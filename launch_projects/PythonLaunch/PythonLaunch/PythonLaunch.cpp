#include <Windows.h>
#include <string>
#include <vector>
#include <Shlwapi.h>
#include <shellapi.h>

#pragma comment(lib, "Shlwapi.lib")

// 错误处理宏，显示消息框并退出程序
#define ERROR_EXIT(msg) { \
    MessageBoxW(NULL, msg, L"错误", MB_OK | MB_ICONERROR); \
    ExitProcess(1); \
}

int CALLBACK WinMain(
    _In_ HINSTANCE hInstance,
    _In_ HINSTANCE hPrevInstance,
    _In_ LPSTR     lpCmdLine,
    _In_ int       nCmdShow
) {
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

    // 获取命令行参数（跳过程序名）
    int argc;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv || argc < 1) {
        ERROR_EXIT(L"命令行参数解析失败");
    }

    // 构建Python命令: "runtime\\python.exe" "main.py" [args...]
    std::wstring command = L"\"runtime\\python.exe\" \"main.py\"";

    // 添加原始参数（跳过程序名）
    for (int i = 1; i < argc; i++) {
        command += L" \"";
        command += argv[i];
        command += L"\"";
    }

    // 创建管道
    HANDLE hReadPipe, hWritePipe;
    SECURITY_ATTRIBUTES sa = { sizeof(sa), NULL, TRUE };
    CreatePipe(&hReadPipe, &hWritePipe, &sa, 0);

    // 准备CreateProcess参数
    STARTUPINFOW si = { sizeof(si) };
    si.dwFlags = STARTF_USESTDHANDLES;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi;
    std::vector<wchar_t> cmdLine(command.begin(), command.end());
    cmdLine.push_back(L'\0');

    // 创建子进程
    if (!CreateProcessW(
        NULL,                   // 可执行文件路径（包含在命令行中）
        cmdLine.data(),         // 命令行字符串
        NULL,                   // 进程安全属性
        NULL,                   // 线程安全属性
        FALSE,                  // 不继承句柄
        CREATE_NO_WINDOW,       // 无特殊标志
        NULL,                   // 使用父进程环境
        NULL,                   // 使用父进程工作目录
        &si,
        &pi
    )) {
        std::wstring errorMsg = L"无法启动子进程:\n" + command;
        ERROR_EXIT(errorMsg.c_str());
    }

    // 清理命令行参数内存
    LocalFree(argv);

    // 等待子进程退出
    WaitForSingleObject(pi.hProcess, INFINITE);

    // 获取退出码
    DWORD exitCode;
    GetExitCodeProcess(pi.hProcess, &exitCode);

    // 清理句柄
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);

    return (int)exitCode;
}