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

DEVTOOLS_MODULE(Window)
{
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
