// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Dialogue by meaning: who is speaking, what they said, which topics the player can pick.
#include "menu_util.h"
#include "methods.h"

#include <cmath>
#include <format>
#include <limits>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    constexpr const char* dialogueMenu = "Dialogue Menu";

    struct Topic
    {
        int index = 0;
        std::string text;
        bool neverSaid = false;
    };

    std::vector<Topic> NativeTopics(RE::MenuTopicManager& manager)
    {
        std::vector<Topic> topics;
        if (manager.dialogueList) {
            int index = 0;
            for (const auto* dialogue : *manager.dialogueList) {
                if (dialogue) {
                    topics.push_back({ index, dialogue->topicText.c_str() ? dialogue->topicText.c_str() : "", dialogue->neverSaid });
                }
                ++index;
            }
        }
        return topics;
    }

    Json Describe(RE::TESObjectREFR* ref)
    {
        if (!ref) {
            return nullptr;
        }
        const auto name = ref->GetDisplayFullName();
        return Json{ { "refId", std::format("{:08X}", ref->GetFormID()) }, { "name", name ? name : "" } };
    }

    RE::Actor* NearestTalker()
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        const auto lists = RE::ProcessLists::GetSingleton();
        if (!player || !lists) {
            return nullptr;
        }
        RE::Actor* best = nullptr;
        float bestDistance = std::numeric_limits<float>::max();
        for (const auto& handle : lists->highActorHandles) {
            const auto actor = handle.get();
            if (!actor || actor.get() == player || actor->IsDead() || actor->IsDisabled()) {
                continue;
            }
            const float distance = actor->GetPosition().GetDistance(player->GetPosition());
            if (distance < bestDistance) {
                bestDistance = distance;
                best = actor.get();
            }
        }
        return best;
    }
}

DEVTOOLS_MODULE(MenuDialogue)
{
    Register("Dialogue.state", [](const Json& params) {
        AllowOnly(params, {});
        auto& ui = RequireUI();
        const bool open = ui.IsMenuOpen(dialogueMenu);
        const auto manager = RE::MenuTopicManager::GetSingleton();
        Json out{ { "open", open }, { "subtitles", menu::CurrentSubtitleLines() } };
        if (!manager) {
            return out;
        }
        out["speaker"] = Describe(manager->speaker.get().get());
        out["greeting"] = manager->isGreetingPlayer;
        out["waitingToAdvance"] = manager->waitingToAdvance;
        auto topics = Json::array();
        if (open) {
            for (const auto& topic : NativeTopics(*manager)) {
                topics.push_back({ { "index", topic.index }, { "text", topic.text }, { "neverSaid", topic.neverSaid } });
            }
        }
        out["topics"] = topics;
        return out;
    }, { .thread = Thread::UI });

    Register("Dialogue.select", [](const Json& params) {
        AllowOnly(params, { "index", "text" });
        auto& ui = RequireUI();
        if (!ui.IsMenuOpen(dialogueMenu)) {
            throw RpcError(code::notFound, "No dialogue is open");
        }
        const auto manager = RE::MenuTopicManager::GetSingleton();
        if (!manager) {
            throw RpcError(code::notReady, "Dialogue manager is unavailable");
        }
        const auto topics = NativeTopics(*manager);
        std::vector<std::string> labels;
        for (const auto& topic : topics) {
            labels.push_back(topic.text);
        }
        const int pick = menu::PickIndex(params, labels, "topic");
        const auto view = menu::RequireMovie(dialogueMenu);
        const bool called = menu::CallDelegate(*view, "TopicClicked", { RE::GFxValue(static_cast<double>(topics[pick].index)) });
        return Json{ { "index", topics[pick].index }, { "text", topics[pick].text }, { "called", called } };
    }, { .thread = Thread::UI });

    Register("Dialogue.start", [](const Json& params) {
        AllowOnly(params, { "refID", "loadEpoch" });
        RequirePlayable(params);
        const auto player = RE::PlayerCharacter::GetSingleton();
        RE::TESObjectREFR* target = nullptr;
        if (params.contains("refID")) {
            const auto id = std::stoul(Require<std::string>(params, "refID"), nullptr, 16);
            target = RE::TESForm::LookupByID<RE::TESObjectREFR>(static_cast<RE::FormID>(id));
            if (!target) {
                throw RpcError(code::notFound, "No reference has that ID");
            }
        } else {
            target = NearestTalker();
            if (!target) {
                throw RpcError(code::notFound, "No living actor is loaded near the player");
            }
        }
        const bool activated = target->ActivateRef(player, 0, nullptr, 0, false);
        auto out = Describe(target);
        out["activated"] = activated;
        return out;
    }, { .thread = Thread::Main, .timeout = 5s });
}
