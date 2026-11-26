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

    // The quest's own completed flag is only set by a script's CompleteQuest. Most quests end by a stage
    // marked "shut down", which stops the quest (enabled drops) without that flag, and the stage records are
    // not reachable reliably. A stopped quest that ran and whose objectives are all completed is finished.
    bool AllObjectivesCompleted(RE::TESQuest* quest)
    {
        bool any = false;
        for (const auto objective : quest->objectives) {
            if (!objective) {
                continue;
            }
            switch (objective->state.get()) {
            case RE::QUEST_OBJECTIVE_STATE::kDormant:
                break;
            case RE::QUEST_OBJECTIVE_STATE::kCompleted:
            case RE::QUEST_OBJECTIVE_STATE::kCompletedDisplayed:
                any = true;
                break;
            default:
                return false;
            }
        }
        return any;
    }

    // status: failed, completed, running, stopped (ran, stopped, not finished) or not-started. The library's
    // IsRunning stays true after a quest is stopped, so the enabled flag is what says whether it is live.
    Json Progress(RE::TESQuest* quest)
    {
        const auto flags = quest->data.flags;
        const bool live = flags.all(RE::QuestFlag::kEnabled);
        const bool failed = flags.all(RE::QuestFlag::kFailed);
        const bool flagged = flags.all(RE::QuestFlag::kCompleted);
        const bool objectivesDone = !live && quest->alreadyRun && AllObjectivesCompleted(quest);
        const bool done = flagged || objectivesDone;
        const char* status = failed ? "failed" : done ? "completed" : live ? "running"
                             : quest->alreadyRun ? "stopped" : "not-started";
        const char* reason = failed ? "failedFlag" : flagged ? "completedFlag"
                             : objectivesDone ? "objectivesCompleted" : "none";
        return Json{ { "status", status }, { "finished", failed || done }, { "finishedBy", reason },
            { "flags", static_cast<unsigned>(flags.underlying()) }, { "alreadyRun", quest->alreadyRun } };
    }

    Json DescribeQuest(RE::TESQuest* quest, bool all)
    {
        Json out{ { "formID", Hex(quest->GetFormID()) }, { "editorID", Name(quest->GetFormEditorID()) },
            { "name", Name(quest->GetName()) }, { "stage", quest->GetCurrentStageID() }, { "running", quest->IsRunning() },
            { "completed", quest->IsCompleted() }, { "enabled", quest->IsEnabled() }, { "active", quest->IsActive() } };
        out.update(Progress(quest));
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
