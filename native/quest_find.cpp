// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Quest.find: look quests up by display name or editor ID, since the console cannot search quest names.
#include "methods.h"
#include "state_common.h"

#include <algorithm>
#include <cctype>

namespace
{
    using namespace devtools;
    using namespace devtools::view;

    std::string Lower(std::string_view text)
    {
        std::string out(text);
        std::ranges::transform(out, out.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return out;
    }
}

DEVTOOLS_MODULE(QuestFind)
{
    Register("Quest.find", [](const Json& params) {
        AllowOnly(params, { "text", "limit" });
        const auto text = Lower(Require<std::string>(params, "text"));
        if (text.size() < 2) {
            throw RpcError(code::invalidParams, "text needs at least two characters");
        }
        const auto limit = std::clamp(Optional<int>(params, "limit", 20), 1, 100);
        const auto handler = RE::TESDataHandler::GetSingleton();
        if (!handler) {
            throw RpcError(code::notReady, "Game data is not loaded yet");
        }
        auto found = Json::array();
        std::size_t total = 0;
        for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
            if (!quest) {
                continue;
            }
            const std::string name = quest->GetName() ? quest->GetName() : "";
            const std::string editor = quest->GetFormEditorID() ? quest->GetFormEditorID() : "";
            if (Lower(name).find(text) == std::string::npos && Lower(editor).find(text) == std::string::npos) {
                continue;
            }
            ++total;
            if (static_cast<int>(found.size()) < limit) {
                found.push_back({ { "formID", Hex(quest->GetFormID()) }, { "editorID", Name(editor.c_str()) },
                    { "name", Name(name.c_str()) }, { "running", quest->IsRunning() }, { "stage", quest->GetCurrentStageID() } });
            }
        }
        return Json{ { "total", total }, { "quests", std::move(found) } };
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(15) });
}
