// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Coverage of a play session against what the chosen plugins define: quest stages set, dialogue infos
// spoken, scenes started, quests started and the Papyrus scripts that logged. Totals come from the loaded
// records, hits from engine event sinks and a light poll, so a run costs nothing when no session is active.
#include "dialogue_data.h"
#include "events.h"
#include "methods.h"
#include "modtest.h"
#include "state_common.h"

#include <map>
#include <mutex>
#include <regex>
#include <set>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace std::chrono_literals;
    using modtest::UnixMs;

    constexpr std::size_t maxPapyrusLines = 2000;

    struct StageHit
    {
        RE::FormID quest;
        int stage;
        std::int64_t ms;
    };

    struct PapyrusLine
    {
        std::int64_t ms;
        std::string severity;
        std::string text;
        std::string script;
        RE::FormID form = 0;
    };

    struct Session
    {
        bool active = false;
        std::vector<std::string> plugins;
        std::int64_t startMs = 0;
        std::vector<StageHit> stages;
        std::map<RE::FormID, std::int64_t> infos;  // info -> first time
        std::map<RE::FormID, std::int64_t> scenes;
        std::map<RE::FormID, std::int64_t> questsStarted;
        std::set<RE::FormID> saidBaseline;
        std::vector<PapyrusLine> papyrus;
        std::map<std::string, std::uint64_t> scripts;
        std::map<std::string, std::uint64_t> scriptForm;  // "script|form" -> count
        Json eventBaseline = nullptr;
    };

    std::mutex mutex;
    Session session;
    std::atomic_bool pollerRunning = false;
    std::atomic_bool papyrusHooked = false;

    modtest::PluginFilter Filter()
    {
        return modtest::PluginFilter::From(session.plugins);
    }

    class StageSink final : public RE::BSTEventSink<RE::TESQuestStageEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::TESQuestStageEvent* event, RE::BSTEventSource<RE::TESQuestStageEvent>*) override
        {
            if (event) {
                std::lock_guard lock(mutex);
                if (session.active) {
                    session.stages.push_back({ event->formID, static_cast<int>(event->stage), UnixMs() });
                }
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };

    class QuestStartSink final : public RE::BSTEventSink<RE::TESQuestStartStopEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::TESQuestStartStopEvent* event, RE::BSTEventSource<RE::TESQuestStartStopEvent>*) override
        {
            if (event && event->started) {
                std::lock_guard lock(mutex);
                if (session.active) {
                    session.questsStarted.emplace(event->formID, UnixMs());
                }
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };

    std::string Severity(RE::BSScript::ErrorLogger::Severity severity, const std::string& text)
    {
        using S = RE::BSScript::ErrorLogger::Severity;
        std::string level = severity == S::kFatal ? "fatal" : severity == S::kError ? "error" : severity == S::kWarning ? "warning" : "info";
        // The text carries its own marker and the VM severity can lag behind it.
        const auto head = modtest::Lower(text.substr(0, 48));
        if (level == "info" || level == "warning") {
            if (head.find("error:") != std::string::npos || head.starts_with("error")) {
                level = "error";
            } else if (level == "info" && head.find("warning:") != std::string::npos) {
                level = "warning";
            }
        }
        return level;
    }

    class PapyrusSink final : public RE::BSTEventSink<RE::BSScript::LogEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::BSScript::LogEvent* event, RE::BSTEventSource<RE::BSScript::LogEvent>*) override
        {
            if (!event || !event->errorMsg) {
                return RE::BSEventNotifyControl::kContinue;
            }
            std::string text = event->errorMsg;
            while (!text.empty() && (text.back() == '\n' || text.back() == '\r')) {
                text.pop_back();
            }
            static const std::regex scriptRef(R"(\[([A-Za-z0-9_]+) \(([0-9A-Fa-f]{8})\)\])");
            PapyrusLine line{ UnixMs(), Severity(event->severity, text), text, {}, 0 };
            std::smatch match;
            if (std::regex_search(text, match, scriptRef)) {
                line.script = match[1];
                line.form = static_cast<RE::FormID>(std::stoul(match[2], nullptr, 16));
            }
            const bool problem = line.severity != "info";
            if (problem && events::Subscribed("papyrusError")) {
                events::Emit("papyrusError", { { "severity", line.severity }, { "text", text },
                    { "script", line.script.empty() ? Json(nullptr) : Json(line.script) },
                    { "form", line.form ? Json(Hex(line.form)) : Json(nullptr) } });
            }
            std::lock_guard lock(mutex);
            if (session.active) {
                if (!line.script.empty()) {
                    ++session.scripts[line.script];
                    ++session.scriptForm[line.script + "|" + Hex(line.form)];
                }
                if (problem && session.papyrus.size() < maxPapyrusLines) {
                    session.papyrus.push_back(std::move(line));
                }
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };

    StageSink stageSink;
    QuestStartSink questStartSink;
    PapyrusSink papyrusSink;

    void EnsurePapyrusHook()
    {
        if (papyrusHooked) {
            return;
        }
        if (const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton()) {
            static_cast<RE::BSScript::IVirtualMachine*>(vm)->RegisterForLogEvent(&papyrusSink);
            papyrusHooked = true;
        }
    }

    // Scenes have no start event in the library, so the poller watches isPlaying of the plugin's scenes.
    void Poll()
    {
        std::map<RE::FormID, bool> playing;
        bool baseline = true;
        while (true) {
            std::this_thread::sleep_for(250ms);
            {
                std::lock_guard lock(mutex);
                if (!session.active) {
                    playing.clear();
                    pollerRunning = false;
                    return;
                }
            }
            const auto handler = RE::TESDataHandler::GetSingleton();
            if (!handler) {
                continue;
            }
            const auto filter = [] { std::lock_guard lock(mutex); return Filter(); }();
            for (const auto scene : handler->GetFormArray<RE::BGSScene>()) {
                if (!scene || !filter.Matches(scene)) {
                    continue;
                }
                const bool now = scene->isPlaying;
                auto& before = playing[scene->GetFormID()];
                if (now && !before && !baseline) {
                    std::lock_guard lock(mutex);
                    session.scenes.emplace(scene->GetFormID(), UnixMs());
                }
                before = now;
            }
            baseline = false;
        }
    }

    std::set<RE::FormID> SaidInfos(const modtest::PluginFilter& filter)
    {
        std::set<RE::FormID> out;
        const auto handler = RE::TESDataHandler::GetSingleton();
        if (!handler) {
            return out;
        }
        for (const auto topic : handler->GetFormArray<RE::TESTopic>()) {
            if (!topic || !topic->topicInfos) {
                continue;
            }
            for (std::uint32_t i = 0; i < topic->numTopicInfos; ++i) {
                const auto info = topic->topicInfos[i];
                if (info && info->saidOnce && filter.Matches(info)) {
                    out.insert(info->GetFormID());
                }
            }
        }
        return out;
    }

    std::set<int> StageIndexes(RE::TESQuest* quest)
    {
        std::set<int> out;
        if (quest->executedStages) {
            for (const auto& stage : *quest->executedStages) {
                out.insert(stage.data.index);
            }
        }
        if (quest->waitingStages) {
            for (const auto* stage : *quest->waitingStages) {
                if (stage) {
                    out.insert(stage->data.index);
                }
            }
        }
        return out;
    }

    Json QuestRef(RE::FormID id)
    {
        const auto quest = RE::TESForm::LookupByID<RE::TESQuest>(id);
        return Json{ { "formID", Hex(id) }, { "editorID", quest ? Name(quest->GetFormEditorID()) : Json(nullptr) },
            { "name", quest ? Name(quest->GetName()) : Json(nullptr) }, { "plugin", quest ? modtest::Plugin(quest) : std::string() } };
    }

    Json Start(const Json& params)
    {
        AllowOnly(params, { "plugins" });
        const auto plugins = Optional<std::vector<std::string>>(params, "plugins", {});
        const auto handler = RE::TESDataHandler::GetSingleton();
        if (!handler) {
            throw RpcError(code::notReady, "Game data is not loaded yet");
        }
        auto unknown = Json::array();
        for (const auto& name : plugins) {
            if (!handler->LookupModByName(name)) {
                unknown.push_back(name);
            }
        }
        EnsurePapyrusHook();
        const auto filter = modtest::PluginFilter::From(plugins);
        const auto baseline = SaidInfos(filter);
        {
            std::lock_guard lock(mutex);
            session = Session{};
            session.active = true;
            session.plugins = plugins;
            session.startMs = UnixMs();
            session.saidBaseline = baseline;
        }
        if (!pollerRunning.exchange(true)) {
            std::thread(Poll).detach();
        }
        return Json{ { "started", true }, { "plugins", plugins }, { "unknownPlugins", unknown },
            { "infosAlreadySaid", baseline.size() }, { "papyrusHooked", papyrusHooked.load() } };
    }

    Json Report(const Json& params)
    {
        AllowOnly(params, { "limit", "stop" });
        const auto limit = std::clamp(Optional<int>(params, "limit", 100), 0, 5000);
        const bool stop = Optional<bool>(params, "stop", false);
        Session copy;
        {
            std::lock_guard lock(mutex);
            if (!session.active && session.startMs == 0) {
                throw RpcError(code::notReady, "No coverage session was started");
            }
            copy = session;
            if (stop) {
                session.active = false;
            }
        }
        const auto filter = modtest::PluginFilter::From(copy.plugins);
        const auto handler = RE::TESDataHandler::GetSingleton();
        if (!handler) {
            throw RpcError(code::notReady, "Game data is not loaded yet");
        }
        const auto since = [&](std::int64_t ms) { return Round((ms - copy.startMs) / 1000.0, 1); };

        // Stages: defined = every stage the plugin's quests carry in memory; hit = stages set this session.
        std::set<std::pair<RE::FormID, int>> hitStages;
        auto stageHits = Json::array();
        for (const auto& hit : copy.stages) {
            const auto quest = RE::TESForm::LookupByID<RE::TESQuest>(hit.quest);
            if (!quest || !filter.Matches(quest)) {
                continue;
            }
            hitStages.insert({ hit.quest, hit.stage });
            auto entry = QuestRef(hit.quest);
            entry["stage"] = hit.stage;
            entry["ms"] = hit.ms;
            entry["atS"] = since(hit.ms);
            stageHits.push_back(std::move(entry));
        }
        std::size_t stagesTotal = 0, questsTotal = 0, stagesCovered = 0;
        auto stagesMissed = Json::array();
        for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
            if (!quest || !filter.Matches(quest)) {
                continue;
            }
            ++questsTotal;
            auto indexes = StageIndexes(quest);
            for (const auto& [id, stage] : hitStages) {
                if (id == quest->GetFormID()) {
                    indexes.insert(stage);
                }
            }
            for (const int index : indexes) {
                ++stagesTotal;
                if (hitStages.contains({ quest->GetFormID(), index })) {
                    ++stagesCovered;
                } else if (static_cast<int>(stagesMissed.size()) < limit) {
                    auto entry = QuestRef(quest->GetFormID());
                    entry["stage"] = index;
                    stagesMissed.push_back(std::move(entry));
                }
            }
        }

        // Infos: said during the session by the watcher, or newly marked said by the engine.
        std::map<RE::FormID, std::pair<std::int64_t, const char*>> said;
        for (const auto& [id, ms] : copy.infos) {
            said.emplace(id, std::pair{ ms, "watcher" });
        }
        for (const auto id : SaidInfos(filter)) {
            if (!copy.saidBaseline.contains(id)) {
                said.emplace(id, std::pair{ std::int64_t{ 0 }, "saidFlag" });
            }
        }
        std::size_t infosTotal = 0, infosCovered = 0;
        auto infosMissed = Json::array();
        for (const auto topic : handler->GetFormArray<RE::TESTopic>()) {
            if (!topic || !topic->topicInfos) {
                continue;
            }
            for (std::uint32_t i = 0; i < topic->numTopicInfos; ++i) {
                const auto info = topic->topicInfos[i];
                if (!info || !filter.Matches(info)) {
                    continue;
                }
                ++infosTotal;
                if (said.contains(info->GetFormID())) {
                    ++infosCovered;
                } else if (static_cast<int>(infosMissed.size()) < limit) {
                    infosMissed.push_back({ { "formID", Hex(info->GetFormID()) }, { "topicText", Name(topic->GetFullName()) },
                        { "quest", topic->ownerQuest ? Name(topic->ownerQuest->GetFormEditorID()) : Json(nullptr) },
                        { "plugin", modtest::Plugin(info) } });
                }
            }
        }
        auto infosSaid = Json::array();
        for (const auto& [id, how] : said) {
            const auto info = RE::TESForm::LookupByID<RE::TESTopicInfo>(id);
            if (!info || !filter.Matches(info)) {
                continue;
            }
            Json entry{ { "formID", Hex(id) }, { "plugin", modtest::Plugin(info) }, { "via", how.second },
                { "topicText", info->parentTopic ? Name(info->parentTopic->GetFullName()) : Json(nullptr) },
                { "quest", info->parentTopic && info->parentTopic->ownerQuest ? Name(info->parentTopic->ownerQuest->GetFormEditorID()) : Json(nullptr) } };
            if (how.first) {
                entry["atS"] = since(how.first);
            }
            infosSaid.push_back(std::move(entry));
        }

        std::size_t scenesTotal = 0;
        for (const auto scene : handler->GetFormArray<RE::BGSScene>()) {
            scenesTotal += scene && filter.Matches(scene);
        }
        auto scenesStarted = Json::array();
        for (const auto& [id, ms] : copy.scenes) {
            const auto scene = RE::TESForm::LookupByID(id);
            scenesStarted.push_back({ { "formID", Hex(id) }, { "editorID", scene ? Name(scene->GetFormEditorID()) : Json(nullptr) },
                { "plugin", modtest::Plugin(scene) }, { "atS", since(ms) } });
        }
        auto questsStarted = Json::array();
        for (const auto& [id, ms] : copy.questsStarted) {
            const auto quest = RE::TESForm::LookupByID<RE::TESQuest>(id);
            if (quest && filter.Matches(quest)) {
                auto entry = QuestRef(id);
                entry["atS"] = since(ms);
                questsStarted.push_back(std::move(entry));
            }
        }

        // Papyrus: attribute a line to a plugin through the form id the VM printed next to the script name.
        auto errors = Json::array(), warnings = Json::array();
        std::uint64_t errorCount = 0, warningCount = 0;
        for (const auto& line : copy.papyrus) {
            const auto form = line.form ? RE::TESForm::LookupByID(line.form) : nullptr;
            const auto plugin = modtest::Plugin(form);
            const bool attributed = !plugin.empty();
            if (attributed && !filter.Matches(form)) {
                continue;
            }
            Json entry{ { "atS", since(line.ms) }, { "severity", line.severity }, { "text", line.text },
                { "script", line.script.empty() ? Json(nullptr) : Json(line.script) },
                { "plugin", attributed ? Json(plugin) : Json(nullptr) } };
            auto& list = line.severity == "warning" ? warnings : errors;
            (line.severity == "warning" ? warningCount : errorCount)++;
            if (static_cast<int>(list.size()) < limit) {
                list.push_back(std::move(entry));
            }
        }
        auto scripts = Json::array();
        std::map<std::string, std::pair<std::string, std::uint64_t>> byScript;  // script -> plugin, count
        for (const auto& [key, count] : copy.scriptForm) {
            const auto bar = key.find('|');
            const auto form = RE::TESForm::LookupByID(static_cast<RE::FormID>(std::stoul(key.substr(bar + 1), nullptr, 16)));
            const auto plugin = modtest::Plugin(form);
            if (!plugin.empty() && !filter.Matches(form)) {
                continue;
            }
            auto& slot = byScript[key.substr(0, bar)];
            slot.first = plugin;
            slot.second += count;
        }
        for (const auto& [name, data] : byScript) {
            scripts.push_back({ { "script", name }, { "logLines", data.second },
                { "plugin", data.first.empty() ? Json(nullptr) : Json(data.first) } });
        }

        return Json{ { "active", copy.active && !stop }, { "plugins", copy.plugins },
            { "durationS", Round((UnixMs() - copy.startMs) / 1000.0, 1) },
            { "stages", { { "hit", std::move(stageHits) }, { "defined", stagesTotal }, { "covered", stagesCovered },
                { "quests", questsTotal }, { "missed", std::move(stagesMissed) },
                { "note", "defined counts the stages the runtime holds for each quest plus those set this session" } } },
            { "infos", { { "said", std::move(infosSaid) }, { "defined", infosTotal }, { "covered", infosCovered },
                { "missed", std::move(infosMissed) } } },
            { "scenes", { { "started", std::move(scenesStarted) }, { "defined", scenesTotal } } },
            { "questsStarted", std::move(questsStarted) },
            { "papyrus", { { "errors", std::move(errors) }, { "errorCount", errorCount }, { "warnings", std::move(warnings) },
                { "warningCount", warningCount }, { "scriptsLogging", std::move(scripts) },
                { "note", "scripts are those that wrote a log line; a script that ran silently is not observable" } } } };
    }
}

DEVTOOLS_MODULE(Coverage)
{
    events::Declare("papyrusError", "A Papyrus warning, error or fatal log line: {severity, text, script, form}");
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        switch (message.type) {
        case SKSE::MessagingInterface::kPostLoad:
        case SKSE::MessagingInterface::kPostPostLoad:
        case SKSE::MessagingInterface::kInputLoaded:
            EnsurePapyrusHook();
            break;
        case SKSE::MessagingInterface::kDataLoaded:
        case SKSE::MessagingInterface::kPostLoadGame:
        case SKSE::MessagingInterface::kNewGame:
            EnsurePapyrusHook();
            if (message.type == SKSE::MessagingInterface::kDataLoaded) {
                if (const auto holder = RE::ScriptEventSourceHolder::GetSingleton()) {
                    holder->GetEventSource<RE::TESQuestStageEvent>()->AddEventSink(&stageSink);
                    holder->GetEventSource<RE::TESQuestStartStopEvent>()->AddEventSink(&questStartSink);
                }
                dialogue::OnInfoSaid([](const dialogue::InfoSaid& said) {
                    std::lock_guard lock(mutex);
                    if (session.active) {
                        session.infos.emplace(said.info, said.ms);
                    }
                });
            }
            break;
        default:
            break;
        }
    });

    Register("Coverage.start", Start, { .thread = Thread::Main, .timeout = std::chrono::seconds(30) });
    Register("Coverage.report", Report, { .thread = Thread::Main, .timeout = std::chrono::seconds(60) });
}
