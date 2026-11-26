// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Generic menu control: any menu the game registered can be opened, closed and observed.
#include "events.h"
#include "game.h"
#include "input.h"
#include "methods.h"

#include <algorithm>
#include <mutex>
#include <vector>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    struct Waiter
    {
        std::string menu;
        bool opening;
        ReplyPtr reply;
    };

    std::mutex waitersMutex;
    std::vector<Waiter> waiters;

    class MenuSink final : public RE::BSTEventSink<RE::MenuOpenCloseEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::MenuOpenCloseEvent* event,
            RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
        {
            if (!event) {
                return RE::BSEventNotifyControl::kContinue;
            }
            const std::string menu = event->menuName.c_str();
            events::Emit("menuOpenClose", { { "menu", menu }, { "opening", event->opening } });
            std::vector<ReplyPtr> ready;
            {
                std::lock_guard lock(waitersMutex);
                std::erase_if(waiters, [&](const Waiter& waiter) {
                    if (waiter.reply->Done()) {
                        return true;
                    }
                    if (waiter.menu == menu && waiter.opening == event->opening) {
                        ready.push_back(waiter.reply);
                        return true;
                    }
                    return false;
                });
            }
            for (const auto& reply : ready) {
                reply->Ok({ { "menu", menu }, { "open", event->opening }, { "changed", true } });
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };

    // These menus are driven by game state that only the engine's own flow sets up (a trainer, a
    // container, a conversation). Showing one without it leaves a half-initialised menu, and hiding one
    // behind the engine's back left the game frozen, so they are opened by activating the right actor
    // and closed with the Cancel control the way a player leaves them.
    bool ContextBound(const std::string& name)
    {
        static const std::initializer_list<std::string_view> names{ RE::TrainingMenu::MENU_NAME, RE::BarterMenu::MENU_NAME,
            RE::GiftMenu::MENU_NAME, RE::ContainerMenu::MENU_NAME, RE::LockpickingMenu::MENU_NAME,
            RE::CraftingMenu::MENU_NAME, RE::DialogueMenu::MENU_NAME, RE::BookMenu::MENU_NAME };
        return std::ranges::find(names, std::string_view(name)) != names.end();
    }

    bool Registered(RE::UI& ui, const std::string& name)
    {
        return ui.menuMap.find(RE::BSFixedString(name)) != ui.menuMap.end();
    }

    void SetMenu(const Json& params, const ReplyPtr& reply, bool opening)
    {
        AllowOnly(params, { "name", "wait", "anyState", "loadEpoch", "force" });
        auto& ui = RequireUI();
        const auto name = Require<std::string>(params, "name");
        if (!Registered(ui, name)) {
            throw RpcError(code::notFound, "No menu is registered as " + name);
        }
        const bool bound = ContextBound(name);
        if (opening && bound && !ui.IsMenuOpen(name) && !Optional(params, "force", false)) {
            throw RpcError(code::invalidParams,
                name + " needs the game's own context (activate the trainer, container or actor instead); pass force=true to show it anyway");
        }
        // Most menus assume a loaded world; anyState is for menus that are valid at the main menu.
        if (!Optional(params, "anyState", false)) {
            RequirePlayable(params);
        }
        const bool wait = Optional(params, "wait", false);
        if (ui.IsMenuOpen(name) == opening) {
            reply->Ok({ { "menu", name }, { "open", opening }, { "changed", false } });
            return;
        }
        const auto queue = RE::UIMessageQueue::GetSingleton();
        if (!queue) {
            throw RpcError(code::notReady, "The menu queue is unavailable");
        }
        const bool cancel = !opening && bound && !Optional(params, "force", false);
        if (cancel && !input::Hooked()) {
            throw RpcError(code::inputUnavailable, name + " must be left with the Cancel control, which needs the input hook");
        }
        if (wait) {
            std::lock_guard lock(waitersMutex);
            waiters.push_back({ name, opening, reply });
        }
        if (cancel && TapControl("Cancel")) {
            if (!wait) {
                reply->Ok({ { "menu", name }, { "accepted", true }, { "via", "cancel" } });
            }
            return;
        }
        queue->AddMessage(RE::BSFixedString(name), opening ? RE::UI_MESSAGE_TYPE::kShow : RE::UI_MESSAGE_TYPE::kHide,
            nullptr);
        if (!wait) {
            reply->Ok({ { "menu", name }, { "accepted", true } });
        }
    }
}

DEVTOOLS_MODULE(UI)
{
    events::Declare("menuOpenClose", "A menu opened or closed: {menu, opening}");
    SetCapability("menus", true);
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            static MenuSink sink;
            if (const auto ui = RE::UI::GetSingleton()) {
                ui->AddEventSink<RE::MenuOpenCloseEvent>(&sink);
            }
        }
    });

    RegisterDeferred("UI.openMenu", [](const Json& params, ReplyPtr reply) { SetMenu(params, reply, true); },
        { .thread = Thread::UI, .timeout = 5s });
    RegisterDeferred("UI.closeMenu", [](const Json& params, ReplyPtr reply) { SetMenu(params, reply, false); },
        { .thread = Thread::UI, .timeout = 5s });
    Register("UI.listMenus", [](const Json& params) {
        AllowOnly(params, {});
        auto& ui = RequireUI();
        auto menus = Json::array();
        for (const auto& item : ui.menuMap) {
            const auto name = std::string(item.first.c_str());
            Json entry{ { "name", name }, { "open", ui.IsMenuOpen(name) } };
            // the flags tell a menu that stops the game or takes input from a HUD widget, which mods add by the dozen
            if (const auto* menu = item.second.menu.get()) {
                entry["pausesGame"] = menu->PausesGame();
                entry["usesMenuContext"] = menu->UsesMenuContext();
            }
            menus.push_back(std::move(entry));
        }
        std::sort(menus.begin(), menus.end(), [](const Json& a, const Json& b) { return a["name"] < b["name"]; });
        return menus;
    }, { .thread = Thread::UI });
    Register("UI.isMenuOpen", [](const Json& params) {
        AllowOnly(params, { "name" });
        auto& ui = RequireUI();
        const auto name = Require<std::string>(params, "name");
        return Json{ { "menu", name }, { "open", ui.IsMenuOpen(name) }, { "ready", MovieReady(ui, name) } };
    }, { .thread = Thread::UI });
}
