// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Quest.targets: what the compass and map quest markers point at. Each displayed objective carries
// target records; the engine resolves a record to the reference the marker follows (the target) and,
// when that lies in another cell, to the load door or marker the player has to reach first (the tracking ref).
#include "methods.h"
#include "quest_targets.h"
#include "state_common.h"

#include <algorithm>
#include <cctype>
#include <cmath>

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

    bool Shown(RE::QUEST_OBJECTIVE_STATE state)
    {
        return state == RE::QUEST_OBJECTIVE_STATE::kDisplayed || state == RE::QUEST_OBJECTIVE_STATE::kCompletedDisplayed ||
               state == RE::QUEST_OBJECTIVE_STATE::kFailedDisplayed;
    }

    bool Pending(RE::QUEST_OBJECTIVE_STATE state) { return state == RE::QUEST_OBJECTIVE_STATE::kDisplayed; }

    Json DescribeTargetRef(RE::TESObjectREFR* ref, RE::PlayerCharacter& player)
    {
        if (!ref) {
            return nullptr;
        }
        const auto pos = ref->GetPosition();
        const auto cell = ref->GetParentCell();
        const bool sameSpace = cell && player.GetParentCell() && (cell == player.GetParentCell() ||
            (!cell->IsInteriorCell() && !player.GetParentCell()->IsInteriorCell() && ref->GetWorldspace() == player.GetWorldspace()));
        Json out{ { "refID", Hex(ref->GetFormID()) }, { "name", Name(ref->GetDisplayFullName()) }, { "pos", Vec3(pos) },
            { "sameSpace", sameSpace }, { "loaded", ref->Is3DLoaded() } };
        if (cell) {
            out["cell"] = Name(cell->GetName());
            out["interior"] = cell->IsInteriorCell();
        }
        if (const auto base = ref->GetBaseObject()) {
            out["baseType"] = FormTypeName(base);
        }
        if (sameSpace) {
            const auto from = player.GetPosition();
            out["distance"] = std::round(std::hypot(std::hypot(pos.x - from.x, pos.y - from.y), pos.z - from.z));
        } else {
            out["distance"] = nullptr;
        }
        return out;
    }

    bool Matches(RE::TESQuest* quest, const std::string& wanted)
    {
        char id[16];
        std::snprintf(id, sizeof(id), "%08X", quest->GetFormID());
        const auto name = Lower(quest->GetName() ? quest->GetName() : "");
        const auto editor = Lower(quest->GetFormEditorID() ? quest->GetFormEditorID() : "");
        return editor == wanted || Lower(id) == wanted || name == wanted;
    }
}

namespace devtools::quest
{
    std::vector<Target> CurrentTargets(const std::string& questText, bool pendingOnly)
    {
        std::vector<Target> out;
        const auto handler = RE::TESDataHandler::GetSingleton();
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!handler || !player) {
            throw RpcError(code::notReady, "Game data is not loaded yet");
        }
        const auto wanted = Lower(questText);
        std::vector<RE::TESQuest*> quests;
        if (!wanted.empty()) {
            for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
                if (quest && Matches(quest, wanted)) {
                    quests.push_back(quest);
                }
            }
            if (quests.empty()) {
                // A partial display name is accepted when exactly one quest has it.
                for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
                    if (quest && quest->GetName() && Lower(quest->GetName()).find(wanted) != std::string::npos) {
                        quests.push_back(quest);
                    }
                }
            }
            if (quests.empty()) {
                throw RpcError(code::notFound, "No quest matches " + questText);
            }
        } else {
            for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
                if (quest && quest->IsRunning() && quest->IsEnabled()) {
                    quests.push_back(quest);
                }
            }
        }
        for (const auto quest : quests) {
            for (const auto objective : quest->objectives) {
                if (!objective || !objective->targets) {
                    continue;
                }
                const auto state = objective->state.get();
                if (pendingOnly ? !Pending(state) : !Shown(state)) {
                    continue;
                }
                for (std::uint32_t i = 0; i < objective->numTargets; ++i) {
                    const auto target = objective->targets[i];
                    if (!target) {
                        continue;
                    }
                    RE::ObjectRefHandle targetHandle;
                    RE::ObjectRefHandle trackingHandle;
                    target->GetTargetRef(targetHandle, false, quest);
                    target->GetTrackingRef(trackingHandle, quest);
                    Target entry;
                    entry.quest = quest;
                    entry.objectiveIndex = objective->index;
                    entry.objectiveText = objective->displayText.c_str() ? objective->displayText.c_str() : "";
                    entry.alias = target->alias;
                    entry.target = targetHandle.get().get();
                    entry.tracking = trackingHandle.get().get();
                    if (entry.target || entry.tracking) {
                        out.push_back(entry);
                    }
                }
            }
        }
        std::ranges::stable_sort(out, [](const Target& a, const Target& b) { return a.quest->IsActive() && !b.quest->IsActive(); });
        return out;
    }
}

DEVTOOLS_MODULE(QuestTargets)
{
    Register("Quest.targets", [](const Json& params) {
        AllowOnly(params, { "quest", "limit", "includeCompleted" });
        auto& player = *RE::PlayerCharacter::GetSingleton();
        const auto limit = std::clamp(Optional<int>(params, "limit", 20), 1, 100);
        const auto targets = quest::CurrentTargets(Optional<std::string>(params, "quest", ""), !Optional<bool>(params, "includeCompleted", false));
        auto list = Json::array();
        for (const auto& t : targets) {
            if (static_cast<int>(list.size()) >= limit) {
                break;
            }
            Json entry{ { "quest", Name(t.quest->GetFormEditorID()) }, { "questName", Name(t.quest->GetName()) },
                { "formID", Hex(t.quest->GetFormID()) }, { "tracked", t.quest->IsActive() }, { "objective", t.objectiveIndex },
                { "text", Name(t.objectiveText.c_str()) }, { "alias", t.alias },
                { "target", DescribeTargetRef(t.target, player) }, { "tracking", DescribeTargetRef(t.tracking, player) } };
            const auto ref = t.tracking ? t.tracking : t.target;
            entry["distance"] = DescribeTargetRef(ref, player)["distance"];
            list.push_back(std::move(entry));
        }
        return Json{ { "total", targets.size() }, { "targets", std::move(list) } };
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(15) });
}
