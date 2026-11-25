// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Quest progress: stage, flags and objective states.
#include "methods.h"
#include "state_common.h"

#include <algorithm>

namespace
{
    using namespace devtools;
    using namespace devtools::view;

    const char* ObjectiveState(RE::QUEST_OBJECTIVE_STATE state)
    {
        switch (state) {
        case RE::QUEST_OBJECTIVE_STATE::kDormant: return "dormant";
        case RE::QUEST_OBJECTIVE_STATE::kDisplayed: return "displayed";
        case RE::QUEST_OBJECTIVE_STATE::kCompleted: return "completed";
        case RE::QUEST_OBJECTIVE_STATE::kCompletedDisplayed: return "completedDisplayed";
        case RE::QUEST_OBJECTIVE_STATE::kFailed: return "failed";
        case RE::QUEST_OBJECTIVE_STATE::kFailedDisplayed: return "failedDisplayed";
        }
        return "unknown";
    }

    // Objectives the journal shows: displayed, completed-displayed and failed-displayed.
    bool Shown(RE::QUEST_OBJECTIVE_STATE state)
    {
        return state == RE::QUEST_OBJECTIVE_STATE::kDisplayed || state == RE::QUEST_OBJECTIVE_STATE::kCompletedDisplayed ||
               state == RE::QUEST_OBJECTIVE_STATE::kFailedDisplayed;
    }

    Json Objectives(RE::TESQuest* quest, bool all)
    {
        auto list = Json::array();
        for (const auto objective : quest->objectives) {
            if (!objective) {
                continue;
            }
            const auto state = objective->state.get();
            if (!all && state == RE::QUEST_OBJECTIVE_STATE::kDormant) {
                continue;
            }
            list.push_back({ { "index", objective->index }, { "text", Name(objective->displayText.c_str()) },
                { "state", ObjectiveState(state) } });
        }
        return list;
    }

    Json DescribeQuest(RE::TESQuest* quest, bool all)
    {
        Json out{ { "formID", Hex(quest->GetFormID()) }, { "editorID", Name(quest->GetFormEditorID()) },
            { "name", Name(quest->GetName()) }, { "stage", quest->GetCurrentStageID() }, { "running", quest->IsRunning() },
            { "completed", quest->IsCompleted() }, { "enabled", quest->IsEnabled() }, { "active", quest->IsActive() } };
        out["objectives"] = Objectives(quest, all);
        return out;
    }
}

DEVTOOLS_MODULE(StateQuest)
{
    Register("Quest.get", [](const Json& params) {
        AllowOnly(params, { "formID", "editorID", "allObjectives" });
        const auto quest = RequireForm(params)->As<RE::TESQuest>();
        if (!quest) {
            throw RpcError(code::invalidParams, "That form is not a quest");
        }
        auto out = DescribeQuest(quest, Optional<bool>(params, "allObjectives", false));
        auto done = Json::array();
        if (quest->executedStages) {
            for (const auto& stage : *quest->executedStages) {
                done.push_back(stage.data.index);
            }
        }
        out["stagesDone"] = std::move(done);
        return out;
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(15) });

    Register("Quest.active", [](const Json& params) {
        AllowOnly(params, { "limit" });
        (void)RequirePlayer();
        const auto limit = std::clamp(Optional<int>(params, "limit", 30), 1, 200);
        const auto handler = RE::TESDataHandler::GetSingleton();
        if (!handler) {
            throw RpcError(code::notReady, "Game data is not loaded yet");
        }
        auto quests = Json::array();
        std::size_t total = 0;
        for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
            if (!quest || !quest->IsRunning()) {
                continue;
            }
            const bool shown = std::ranges::any_of(quest->objectives, [](const RE::BGSQuestObjective* objective) {
                return objective && Shown(objective->state.get());
            });
            if (!shown) {
                continue;
            }
            ++total;
            if (static_cast<int>(quests.size()) < limit) {
                auto entry = DescribeQuest(quest, false);
                auto& objectives = entry["objectives"];
                objectives.erase(std::remove_if(objectives.begin(), objectives.end(), [](const Json& objective) {
                    return objective["state"].get<std::string>().find("Displayed") == std::string::npos && objective["state"] != "displayed";
                }), objectives.end());
                quests.push_back(std::move(entry));
            }
        }
        return Json{ { "total", total }, { "quests", std::move(quests) } };
    }, { .thread = Thread::Main });
}
