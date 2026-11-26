// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// What an NPC could say or offer, straight from the loaded quest, topic and info records: each info's
// conditions are evaluated by the engine against the NPC and the player. Also reports which infos the
// dialogue menu offers now and notices every info an NPC starts to speak.
#include "dialogue_data.h"

#include "events.h"
#include "methods.h"
#include "modtest.h"
#include "state_common.h"

#include <format>
#include <mutex>
#include <set>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace std::chrono_literals;

    std::mutex listenerMutex;
    std::vector<std::function<void(const dialogue::InfoSaid&)>> listeners;

    const char* TypeName(RE::DIALOGUE_TYPE type)
    {
        switch (type) {
        case RE::DIALOGUE_TYPE::kPlayerDialogue: return "player";
        case RE::DIALOGUE_TYPE::kCommandDialogue: return "command";
        case RE::DIALOGUE_TYPE::kSceneDialogue: return "scene";
        case RE::DIALOGUE_TYPE::kCombat: return "combat";
        case RE::DIALOGUE_TYPE::kFavors: return "favors";
        case RE::DIALOGUE_TYPE::kDetection: return "detection";
        case RE::DIALOGUE_TYPE::kService: return "service";
        case RE::DIALOGUE_TYPE::kMiscellaneous: return "misc";
        default: return "other";
        }
    }

    Json QuestJson(RE::TESQuest* quest)
    {
        if (!quest) {
            return nullptr;
        }
        return Json{ { "formID", Hex(quest->GetFormID()) }, { "editorID", Name(quest->GetFormEditorID()) },
            { "name", Name(quest->GetName()) }, { "plugin", modtest::Plugin(quest) },
            { "running", quest->IsEnabled() } };
    }

    // The first line of the info's first response, which is what the player would read as a subtitle.
    Json FirstResponse(RE::TESTopicInfo* info)
    {
        const auto list = info->GetResponseList();
        if (!list || !list->head || !list->head->responseText.c_str()) {
            return nullptr;
        }
        return Name(list->head->responseText.c_str());
    }

    std::set<RE::FormID> OfferedTopics()
    {
        std::set<RE::FormID> out;
        const auto manager = RE::MenuTopicManager::GetSingleton();
        if (manager && manager->menuOpen && manager->dialogueList) {
            for (const auto* entry : *manager->dialogueList) {
                if (entry && entry->parentTopic) {
                    out.insert(entry->parentTopic->GetFormID());
                }
            }
        }
        return out;
    }

    void Watch()
    {
        RE::FormID last = 0;
        while (true) {
            std::this_thread::sleep_for(40ms);
            if (!state::dataLoaded) {
                continue;
            }
            const auto manager = RE::MenuTopicManager::GetSingleton();
            const auto info = manager ? manager->currentTopicInfo : nullptr;
            const RE::FormID id = info ? info->GetFormID() : 0;
            if (id == last) {
                continue;
            }
            last = id;
            if (!id) {
                continue;
            }
            dialogue::InfoSaid said;
            said.info = id;
            said.topic = info->parentTopic ? info->parentTopic->GetFormID() : 0;
            said.quest = info->parentTopic && info->parentTopic->ownerQuest ? info->parentTopic->ownerQuest->GetFormID() : 0;
            said.ms = modtest::UnixMs();
            std::vector<std::function<void(const dialogue::InfoSaid&)>> copy;
            {
                std::lock_guard lock(listenerMutex);
                copy = listeners;
            }
            for (const auto& listener : copy) {
                listener(said);
            }
            if (events::Subscribed("dialogueInfo")) {
                const auto quest = info->parentTopic ? info->parentTopic->ownerQuest : nullptr;
                events::Emit("dialogueInfo", { { "info", Hex(said.info) }, { "topic", Hex(said.topic) },
                    { "topicText", info->parentTopic ? Name(info->parentTopic->GetFullName()) : Json(nullptr) },
                    { "quest", Hex(said.quest) }, { "questEditorID", quest ? Name(quest->GetFormEditorID()) : Json(nullptr) },
                    { "plugin", modtest::Plugin(info) } });
            }
        }
    }

    Json Topics(const Json& params)
    {
        AllowOnly(params, { "refID", "includeGeneric", "onlyPassing", "plugins", "quest", "kinds", "limit", "responses" });
        const auto ref = RequireRef(params, "refID");
        const auto speaker = ref->As<RE::Actor>();
        if (!speaker) {
            throw RpcError(code::invalidParams, "refID is not an actor");
        }
        const auto handler = RE::TESDataHandler::GetSingleton();
        if (!handler) {
            throw RpcError(code::notReady, "Game data is not loaded yet");
        }
        auto& player = RequirePlayer();
        const bool includeGeneric = Optional<bool>(params, "includeGeneric", false);
        const bool onlyPassing = Optional<bool>(params, "onlyPassing", false);
        const bool responses = Optional<bool>(params, "responses", true);
        const auto filter = modtest::PluginFilter::From(Optional<std::vector<std::string>>(params, "plugins", {}));
        const auto questFilter = modtest::Lower(Optional<std::string>(params, "quest", ""));
        std::set<std::string> kinds;
        for (const auto& kind : Optional<std::vector<std::string>>(params, "kinds", {})) {
            kinds.insert(modtest::Lower(kind));
        }
        const auto limit = std::clamp(Optional<int>(params, "limit", 300), 1, 5000);
        const auto offered = OfferedTopics();

        auto topics = Json::array();
        std::size_t topicTotal = 0, infoTotal = 0, passingTotal = 0, saidTotal = 0, scanned = 0, textBudget = 600;
        for (const auto topic : handler->GetFormArray<RE::TESTopic>()) {
            if (!topic || !topic->ownerQuest || !topic->topicInfos) {
                continue;
            }
            const auto type = topic->data.type.get();
            if (!kinds.empty() && !kinds.contains(TypeName(type))) {
                continue;
            }
            const auto quest = topic->ownerQuest;
            if (!questFilter.empty() && modtest::Lower(quest->GetFormEditorID() ? quest->GetFormEditorID() : "").find(questFilter) == std::string::npos &&
                modtest::Lower(quest->GetName() ? quest->GetName() : "").find(questFilter) == std::string::npos) {
                continue;
            }
            const bool questRunning = quest->IsEnabled();
            auto infos = Json::array();
            for (std::uint32_t i = 0; i < topic->numTopicInfos; ++i) {
                const auto info = topic->topicInfos[i];
                if (!info || !filter.Matches(info)) {
                    continue;
                }
                ++scanned;
                RE::ConditionCheckParams check(speaker, &player);
                check.quest = quest;
                const auto own = modtest::Evaluate(info->objConditions, check, true);
                RE::ConditionCheckParams questCheck(speaker, &player);
                questCheck.quest = quest;
                const auto questOwn = modtest::Evaluate(quest->objConditions, questCheck, true);
                const bool identity = own.hasIdentity || questOwn.hasIdentity;
                if (identity ? !(own.pass && questOwn.pass) : !includeGeneric) {
                    continue;
                }
                RE::ConditionCheckParams full(speaker, &player);
                full.quest = quest;
                auto verdict = modtest::Evaluate(info->objConditions, full, false);
                RE::ConditionCheckParams questFull(speaker, &player);
                questFull.quest = quest;
                const auto questVerdict = modtest::Evaluate(quest->objConditions, questFull, false);
                const bool sayOnce = info->data.flags.any(RE::TOPIC_INFO_DATA::TOPIC_INFO_FLAGS::kSayOnce);
                const bool exhausted = sayOnce && info->saidOnce;
                const bool passes = questRunning && verdict.pass && questVerdict.pass && !exhausted;
                ++infoTotal;
                passingTotal += passes;
                saidTotal += info->saidOnce;
                if (onlyPassing && !passes) {
                    continue;
                }
                Json entry{ { "formID", Hex(info->GetFormID()) }, { "plugin", modtest::Plugin(info) },
                    { "passes", passes }, { "sayOnce", sayOnce }, { "said", info->saidOnce },
                    { "addressed", identity } };
                if (!verdict.pass) {
                    entry["failing"] = verdict.failing;
                }
                if (!questVerdict.pass) {
                    entry["questFailing"] = questVerdict.failing;
                }
                if (responses && textBudget > 0 && (passes || !onlyPassing)) {
                    --textBudget;
                    entry["text"] = FirstResponse(info);
                }
                infos.push_back(std::move(entry));
            }
            if (infos.empty()) {
                continue;
            }
            ++topicTotal;
            if (static_cast<int>(topics.size()) < limit) {
                topics.push_back({ { "formID", Hex(topic->GetFormID()) }, { "editorID", Name(topic->GetFormEditorID()) },
                    { "text", Name(topic->GetFullName()) }, { "kind", TypeName(type) },
                    { "subtype", static_cast<int>(topic->data.subtype.get()) },
                    { "plugin", modtest::Plugin(topic) }, { "quest", QuestJson(quest) },
                    { "offeredNow", offered.contains(topic->GetFormID()) }, { "infos", std::move(infos) } });
            }
        }
        return Json{ { "speaker", DescribeRef(ref, nullptr) }, { "topicTotal", topicTotal }, { "infoTotal", infoTotal },
            { "passing", passingTotal }, { "saidOnce", saidTotal }, { "scannedInfos", scanned },
            { "truncated", topicTotal > topics.size() }, { "topics", std::move(topics) } };
    }

    Json Offered(const Json& params)
    {
        AllowOnly(params, {});
        const auto manager = RE::MenuTopicManager::GetSingleton();
        auto list = Json::array();
        if (manager && manager->dialogueList) {
            int index = 0;
            for (const auto* entry : *manager->dialogueList) {
                if (entry) {
                    list.push_back({ { "index", index }, { "text", entry->topicText.c_str() ? entry->topicText.c_str() : "" },
                        { "neverSaid", entry->neverSaid },
                        { "topic", entry->parentTopic ? Hex(entry->parentTopic->GetFormID()) : "" },
                        { "info", entry->parentTopicInfo ? Hex(entry->parentTopicInfo->GetFormID()) : "" },
                        { "quest", QuestJson(entry->parentQuest) } });
                }
                ++index;
            }
        }
        const auto current = manager ? manager->currentTopicInfo : nullptr;
        return Json{ { "open", manager && manager->menuOpen }, { "topics", std::move(list) },
            { "currentInfo", current ? Json(Hex(current->GetFormID())) : Json(nullptr) } };
    }
}

namespace devtools::dialogue
{
    void OnInfoSaid(std::function<void(const InfoSaid&)> listener)
    {
        std::lock_guard lock(listenerMutex);
        listeners.push_back(std::move(listener));
    }
}

DEVTOOLS_MODULE(DialogueData)
{
    events::Declare("dialogueInfo", "An NPC began a dialogue info: {info, topic, topicText, quest, questEditorID, plugin}");
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            std::thread(Watch).detach();
        }
    });

    Register("Dialogue.topics", Topics, { .thread = Thread::Main, .timeout = std::chrono::seconds(60) });
    Register("Dialogue.offered", Offered, { .thread = Thread::UI });
}
