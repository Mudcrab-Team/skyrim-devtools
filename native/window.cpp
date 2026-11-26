// SPDX-License-Identifier: GPL-3.0-or-later
// Headless placement of the game window. Off-screen keeps the swap chain presenting (the window
// is still visible to DWM), whereas a hidden or minimised window may stop the game rendering.

#include "game.h"
#include "methods.h"

#include <Windows.h>

#include <mutex>
#include <optional>
#include <thread>

namespace
{
    using namespace devtools;

    std::mutex placementMutex;
    std::optional<RECT> original;

    Json Place(const std::string& mode)
    {
        const auto window = MainWindow();
        if (!window) {
            throw RpcError(code::notReady, "The game window does not exist yet");
        }
        std::lock_guard lock(placementMutex);
        RECT rect{};
        GetWindowRect(window, &rect);
        if (mode == "restore") {
            ShowWindow(window, SW_SHOWNOACTIVATE);
            if (original) {
                SetWindowPos(window, HWND_TOP, original->left, original->top, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
                original.reset();
            }
        } else {
            if (!original && rect.left > -10000) {
                original = rect;
            }
            if (mode == "offscreen") {
                ShowWindow(window, SW_SHOWNOACTIVATE);
                SetWindowPos(window, HWND_BOTTOM, -32000, -32000, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
            } else if (mode == "hide") {
                ShowWindow(window, SW_HIDE);
            } else if (mode == "minimize") {
                ShowWindow(window, SW_SHOWMINNOACTIVE);
            } else {
                throw RpcError(code::invalidParams, "mode is offscreen, hide, minimize or restore");
            }
            // Hand the foreground back to the desktop so nothing depends on the game holding it.
            if (GameIsForeground()) {
                SetForegroundWindow(GetShellWindow());
            }
        }
        GetWindowRect(window, &rect);
        return { { "mode", mode }, { "left", rect.left }, { "top", rect.top }, { "width", rect.right - rect.left },
            { "height", rect.bottom - rect.top }, { "visible", IsWindowVisible(window) != FALSE },
            { "foreground", GameIsForeground() } };
    }

    void ApplyHeadless()
    {
        if (GetConfig().headless && MainWindow()) {
            Place("offscreen");
        }
    }
}

namespace
{
    // Where the game thinks it renders versus where Windows puts it: a swap chain smaller than the monitor,
    // or a window smaller than the swap chain, shows up as a game drawn in a corner of the screen.
    devtools::Json Info()
    {
        using devtools::Json;
        const auto window = devtools::MainWindow();
        if (!window) {
            throw devtools::RpcError(devtools::code::notReady, "The game window does not exist yet");
        }
        RECT outer{}, client{};
        GetWindowRect(window, &outer);
        GetClientRect(window, &client);
        const auto monitor = MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST);
        MONITORINFO monitorInfo{ sizeof(monitorInfo) };
        GetMonitorInfoW(monitor, &monitorInfo);
        Json result{
            { "window", { { "left", outer.left }, { "top", outer.top }, { "width", outer.right - outer.left }, { "height", outer.bottom - outer.top } } },
            { "client", { { "width", client.right - client.left }, { "height", client.bottom - client.top } } },
            { "monitor", { { "width", monitorInfo.rcMonitor.right - monitorInfo.rcMonitor.left },
                             { "height", monitorInfo.rcMonitor.bottom - monitorInfo.rcMonitor.top } } },
            { "style", static_cast<std::uint64_t>(GetWindowLongPtrW(window, GWL_STYLE)) },
        };
        const auto user32 = GetModuleHandleW(L"user32.dll");
        using DpiFn = UINT(WINAPI*)(HWND);
        using ContextFn = HANDLE(WINAPI*)(HWND);
        using AwarenessFn = int(WINAPI*)(HANDLE);
        if (const auto dpi = reinterpret_cast<DpiFn>(GetProcAddress(user32, "GetDpiForWindow"))) {
            result["dpi"] = dpi(window);
        }
        const auto context = reinterpret_cast<ContextFn>(GetProcAddress(user32, "GetWindowDpiAwarenessContext"));
        const auto awareness = reinterpret_cast<AwarenessFn>(GetProcAddress(user32, "GetAwarenessFromDpiAwarenessContext"));
        if (context && awareness) {
            result["dpiAwareness"] = awareness(context(window));  // 0 unaware, 1 system, 2 per-monitor
        }
        if (const auto render = RE::BSGraphics::Renderer::GetCurrentRenderWindow(); render && render->swapChain) {
            REX::W32::DXGI_SWAP_CHAIN_DESC desc{};
            if (SUCCEEDED(render->swapChain->GetDesc(&desc))) {
                result["swapChain"] = { { "width", desc.bufferDesc.width }, { "height", desc.bufferDesc.height },
                    { "windowed", desc.windowed != FALSE }, { "buffers", desc.bufferCount }, { "swapEffect", static_cast<int>(desc.swapEffect) },
                    { "refreshNum", desc.bufferDesc.refreshRate.numerator }, { "refreshDen", desc.bufferDesc.refreshRate.denominator } };
            }
        }
        return result;
    }
}

DEVTOOLS_MODULE(Window)
{
    devtools::Register("Window.info", [](const devtools::Json& params) {
        devtools::AllowOnly(params, {});
        return Info();
    }, { .thread = devtools::Thread::UI });
    SetCapability("headless", GetConfig().headless);
    if (GetConfig().headless) {
        // The window appears while the plugin is still loading; move it as soon as it exists so
        // the loading screen never takes over the desktop.
        std::thread([] {
            for (int attempt = 0; attempt < 600 && !MainWindow(); ++attempt) {
                std::this_thread::sleep_for(std::chrono::milliseconds(200));
            }
            ApplyHeadless();
        }).detach();
    }
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded ||
            message.type == SKSE::MessagingInterface::kPostLoadGame) {
            ApplyHeadless();
        }
    });
    Register("Window.place", [](const Json& params) {
        AllowOnly(params, { "mode" });
        return Place(Require<std::string>(params, "mode"));
    }, { .thread = Thread::UI });
}
