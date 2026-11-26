// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
#include "transition.h"

#include "console.h"
#include "methods.h"
#include "options.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    constexpr auto bounceLimit = 45s;
    constexpr auto settle = 500ms;

    bool IsJump(std::string_view command)
    {
        while (!command.empty() && std::isspace(static_cast<unsigned char>(command.front()))) {
            command.remove_prefix(1);
        }
        const auto end = command.find_first_of(" \t");
        std::string word(command.substr(0, end));
        std::ranges::transform(word, word.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return word == "coc" || word == "cow" || word == "centeroncell" || word == "centeronworld";
    }

    bool InCell(const std::string& editorId)
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        const auto cell = player ? player->GetParentCell() : nullptr;
        const auto id = cell ? cell->GetFormEditorID() : nullptr;
        return id && _stricmp(id, editorId.c_str()) == 0;
    }

    bool Reached(const std::string& editorId)
    {
        const auto ui = RE::UI::GetSingleton();
        return ui && InCell(editorId) && !ui->IsMenuOpen(RE::LoadingMenu::MENU_NAME) && Playable(*ui);
    }
}

namespace devtools::transition
{
    bool Wanted(std::string_view command)
    {
        return options::CleanTransition() && IsJump(command) && !InCell(options::CleanTransitionCell());
    }

    void Bounce(const ReplyPtr& reply, std::function<void()> proceed)
    {
        const auto cell = options::CleanTransitionCell();
        console::Run("coc " + cell, nullptr);
        // The jump finishes over several frames and this call runs on the main thread, so a helper thread polls
        // the game state through queued tasks instead of blocking the very thread that has to load the cell.
        std::thread([reply, cell, proceed = std::move(proceed)] {
            const auto began = std::chrono::steady_clock::now();
            while (!reply->Done() && std::chrono::steady_clock::now() - began < bounceLimit) {
                std::this_thread::sleep_for(100ms);
                const auto tasks = SKSE::GetTaskInterface();
                if (!tasks) {
                    continue;
                }
                const auto answer = std::make_shared<std::atomic_int>(0);
                tasks->AddTask([answer, cell] { *answer = Reached(cell) ? 1 : 2; });
                for (int wait = 0; wait < 40 && *answer == 0; ++wait) {
                    std::this_thread::sleep_for(50ms);
                }
                if (*answer != 1) {
                    continue;
                }
                std::this_thread::sleep_for(settle);
                tasks->AddTask([reply, proceed] {
                    if (reply->Done()) {
                        return;
                    }
                    try {
                        proceed();
                    } catch (const RpcError& error) {
                        reply->Fail(error.code, error.message, error.data);
                    } catch (const std::exception& error) {
                        reply->Fail(code::internal, error.what());
                    }
                });
                return;
            }
            reply->Fail(code::timeout, "The clean-transition bounce cell never finished loading", { { "cell", cell } });
        }).detach();
    }
}
