// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Robustness options: keep the game running while its window is in the background, hide the menus an
// unattended session would otherwise stall on, and the settings of the clean-transition and stall-watchdog
// features. Options.set changes them live; the host config applies them at connect.
#include "options.h"

#include "events.h"
#include "methods.h"

#include <Windows.h>

#include <algorithm>
#include <mutex>
#include <set>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    // Menus the session cannot work without, and message boxes, which hold a decision for the player.
    const std::set<std::string, std::less<>> protectedMenus{ "MessageBoxMenu", "Main Menu", "Loading Menu", "Fader Menu",
        "HUD Menu", "Cursor Menu" };

    std::mutex mutex;
    std::set<std::string, std::less<>> dismiss;
    bool cleanTransition = false;
    std::string transitionCell = "QASmoke";
    int stallThresholdMs = 6000;

    std::atomic_bool alwaysActive = false;
    std::atomic<std::uint64_t> blocked = 0;
    std::atomic<std::uint64_t> dismissed = 0;
    std::atomic_bool subclassed = false;
    WNDPROC originalProc = nullptr;

    // Swallows the notices that tell the game it lost focus. The game keeps its own flag for "I am the active
    // application", so never delivering the deactivation keeps it running without the window ever being focused.
    LRESULT CALLBACK ActiveProc(HWND window, UINT message, WPARAM wParam, LPARAM lParam)
    {
        if (alwaysActive.load(std::memory_order_relaxed)) {
            const bool deactivating = (message == WM_ACTIVATEAPP && wParam == FALSE) ||
                                      (message == WM_ACTIVATE && LOWORD(wParam) == WA_INACTIVE) ||
                                      (message == WM_KILLFOCUS);
            if (deactivating) {
                ++blocked;
                return 0;
            }
            if (message == WM_NCACTIVATE && wParam == FALSE) {
                ++blocked;
                return DefWindowProcW(window, message, TRUE, lParam);
            }
        }
        return CallWindowProcW(originalProc, window, message, wParam, lParam);
    }

    void SetIniBool(const char* name, bool value)
    {
        if (const auto ini = RE::INISettingCollection::GetSingleton()) {
            if (const auto setting = ini->GetSetting(name); setting && setting->GetType() == RE::Setting::Type::kBool) {
                setting->SetBool(value);
            }
        }
    }

    void ApplyAlwaysActive(bool enable)
    {
        const auto window = MainWindow();
        if (enable && window && !subclassed.exchange(true)) {
            originalProc = reinterpret_cast<WNDPROC>(
                SetWindowLongPtrW(window, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(&ActiveProc)));
        }
        alwaysActive = enable;
        SetIniBool("bAlwaysActive:General", enable);
        if (enable && window && !GameIsForeground()) {
            // A window that lost focus before the option was turned on already told the game it is inactive.
            DWORD_PTR result = 0;
            SendMessageTimeoutW(window, WM_ACTIVATEAPP, TRUE, 0, SMTO_ABORTIFHUNG, 2000, &result);
        }
    }

    Json Current()
    {
        std::lock_guard lock(mutex);
        return { { "alwaysActive", alwaysActive.load() }, { "autoDismissMenus", dismiss },
            { "cleanTransition", cleanTransition }, { "cleanTransitionCell", transitionCell },
            { "stallThresholdMs", stallThresholdMs }, { "foreground", GameIsForeground() },
            { "deactivationsBlocked", blocked.load() }, { "menusDismissed", dismissed.load() } };
    }

    Json Apply(const Json& params)
    {
        AllowOnly(params, { "alwaysActive", "autoDismissMenus", "cleanTransition", "cleanTransitionCell", "stallThresholdMs" });
        Json ignored = Json::array();
        std::set<std::string, std::less<>> menus;
        const bool setMenus = params.contains("autoDismissMenus");
        if (setMenus) {
            for (const auto& name : Require<std::vector<std::string>>(params, "autoDismissMenus")) {
                if (protectedMenus.contains(name)) {
                    ignored.push_back(name);
                } else {
                    menus.insert(name);
                }
            }
        }
        if (params.contains("cleanTransitionCell")) {
            const auto cell = Require<std::string>(params, "cleanTransitionCell");
            const auto form = RE::TESForm::LookupByEditorID<RE::TESObjectCELL>(cell);
            if (!form || !form->IsInteriorCell()) {
                throw RpcError(code::notFound, "cleanTransitionCell must be the editor ID of an interior cell");
            }
        }
        int previousThreshold = 0;
        {
            std::lock_guard lock(mutex);
            previousThreshold = stallThresholdMs;
        }
        const auto threshold = Optional<int>(params, "stallThresholdMs", previousThreshold);
        if (threshold < 1000 || threshold > 600000) {
            throw RpcError(code::invalidParams, "stallThresholdMs is 1000 to 600000");
        }
        {
            std::lock_guard lock(mutex);
            if (setMenus) {
                dismiss = std::move(menus);
            }
            cleanTransition = Optional(params, "cleanTransition", cleanTransition);
            transitionCell = Optional<std::string>(params, "cleanTransitionCell", transitionCell);
            stallThresholdMs = threshold;
        }
        if (params.contains("alwaysActive")) {
            ApplyAlwaysActive(Require<bool>(params, "alwaysActive"));
        }
        auto result = Current();
        if (!ignored.empty()) {
            result["ignored"] = std::move(ignored);
        }
        return result;
    }
}

namespace devtools::options
{
    void OnMenuOpened(const std::string& menu)
    {
        {
            std::lock_guard lock(mutex);
            if (!dismiss.contains(menu)) {
                return;
            }
        }
        // The menu is still initialising when its open event fires; hiding it now races its own setup.
        std::thread([menu] {
            std::this_thread::sleep_for(150ms);
            if (const auto queue = RE::UIMessageQueue::GetSingleton()) {
                queue->AddMessage(RE::BSFixedString(menu), RE::UI_MESSAGE_TYPE::kHide, nullptr);
                ++dismissed;
                events::Emit("menuAutoDismissed", { { "menu", menu } });
            }
        }).detach();
    }

    bool CleanTransition()
    {
        std::lock_guard lock(mutex);
        return cleanTransition;
    }

    std::string CleanTransitionCell()
    {
        std::lock_guard lock(mutex);
        return transitionCell;
    }

    int StallThresholdMs()
    {
        std::lock_guard lock(mutex);
        return stallThresholdMs;
    }
}

DEVTOOLS_MODULE(Options)
{
    events::Declare("menuAutoDismissed", "A menu on the autoDismissMenus list was hidden: {menu}");
    SetCapability("options", true);
    Register("Options.get", [](const Json& params) {
        AllowOnly(params, {});
        return Current();
    }, { .thread = Thread::Any });
    Register("Options.set", [](const Json& params) { return Apply(params); }, { .thread = Thread::Main });
}
