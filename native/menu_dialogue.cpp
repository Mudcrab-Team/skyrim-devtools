// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Dialogue by meaning: who is speaking, what they said, which topics the player can pick, and when
// the conversation is ready for the next choice.
//
// A topic click starts a chain of spoken lines (the player's, then the NPC's). While a line plays the
// menu still shows the old list (or a lone "..."), and the engine keeps `currentTopicInfo` set; the new
// list appears only after the last line finished. Voice playback takes real time, so waiting for
// the list to come back is the reliable "ready" signal and `Dialogue.advance` skips a line.
#include "events.h"
#include "menu_util.h"
#include "methods.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <format>
#include <atomic>
#include <limits>
#include <mutex>
#include <optional>
#include <set>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;
    using Clock = std::chrono::steady_clock;

    constexpr const char* dialogueMenu = "Dialogue Menu";
    constexpr auto pollInterval = 50ms;
    // The list must stay selectable this long before waitIdle reports it, so the transition between
    // two lines (when the engine briefly has no current line) is not mistaken for "ready".
    constexpr auto settleTime = 300ms;
    constexpr std::size_t lineLimit = 200;

    struct Topic
    {
        int index = 0;
        std::string text;
        bool neverSaid = false;
    };

    struct Line
    {
        std::uint64_t seq = 0;
        std::string speaker;
        std::string role;  // "player" or "npc"
        std::string text;
    };

    // Everything one evaluation of the conversation reads from the game. UI thread only.
    struct Status
    {
        bool open = false;
        bool menuOpenFlag = false;
        bool speaking = false;
        bool transition = false;
        bool selectable = false;
        bool greeting = false;
        bool canSkip = false;
        bool waitingToAdvance = false;
        Json speaker = nullptr;
        // A menu opened from the conversation (training, barter, gift, ...) sits above the dialogue and
        // keeps it waiting until the player closes it.
        std::string overlay;
        std::vector<Topic> topics;

        [[nodiscard]] const char* Phase() const
        {
            if (!open) {
                return speaking ? "npc-speaking" : "closed";
            }
            if (!overlay.empty()) {
                return "submenu";
            }
            return speaking ? "speaking" : transition ? "advancing" : "topics";
        }
    };

    enum class Kind
    {
        Idle,
        Open,
        Closed
    };

    struct Waiter
    {
        Kind kind = Kind::Idle;
        ReplyPtr reply;
        Clock::time_point began;
        Clock::time_point deadline;
        Clock::time_point openGrace;
        std::uint64_t since = 0;
        bool skip = false;
        bool sawOpen = false;
        bool cancelSent = false;
        bool hideSent = false;
        Clock::time_point lastSkip;
    };

    std::mutex sharedMutex;
    std::deque<Line> lines;
    std::uint64_t lineSeq = 0;
    std::uint64_t mark = 0;
    std::vector<Waiter> waiters;
    Json lastStatus = nullptr;
    std::string lastPhase = "closed";
    std::optional<Clock::time_point> selectableSince;
    std::optional<Clock::time_point> speakingSince;
    std::optional<Clock::time_point> overlaySince;
    std::uint64_t speakStartSeq = 0;
    std::atomic_bool tickPending = false;
    std::atomic_bool watcherStarted = false;

    std::string Hex(const RE::TESForm* form)
    {
        return form ? std::format("{:08X}", form->GetFormID()) : std::string();
    }

    Json Describe(RE::TESObjectREFR* ref)
    {
        if (!ref) {
            return nullptr;
        }
        const auto name = ref->GetDisplayFullName();
        return Json{ { "refId", Hex(ref) }, { "name", name ? name : "" } };
    }

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

    RE::ObjectRefHandle Speaker(const RE::MenuTopicManager& manager)
    {
        return manager.speaker.get() ? manager.speaker : manager.lastSpeaker;
    }

    Status Evaluate()
    {
        Status status;
        auto& ui = RequireUI();
        status.open = ui.IsMenuOpen(dialogueMenu);
        const auto manager = RE::MenuTopicManager::GetSingleton();
        if (!manager) {
            return status;
        }
        status.menuOpenFlag = manager->menuOpen;
        status.greeting = manager->isGreetingPlayer;
        status.canSkip = manager->canSkip;
        status.waitingToAdvance = manager->waitingToAdvance;
        status.speaking = manager->currentTopicInfo != nullptr && !manager->advanceInProgress;
        status.transition = manager->waitingToAdvance || manager->advanceRequested || manager->advanceInProgress ||
                            manager->currentTopicInfo != nullptr;
        if (const auto ref = Speaker(*manager).get()) {
            status.speaker = Describe(ref.get());
        }
        if (status.open) {
            bool above = false;
            for (const auto& entry : ui.menuStack) {
                if (!entry) {
                    continue;
                }
                std::string name;
                for (const auto& item : ui.menuMap) {
                    if (item.second.menu.get() == entry.get()) {
                        name = item.first.c_str();
                        break;
                    }
                }
                if (name == dialogueMenu) {
                    above = true;
                } else if (above && name != "Cursor Menu" && name != "HUD Menu" && name != "Fader Menu" && name != "Mist Menu" && status.overlay.empty()) {
                    status.overlay = name;
                }
            }
            status.topics = NativeTopics(*manager);
            status.selectable = status.overlay.empty() && !status.transition && !manager->shutMenu;
        }
        return status;
    }

    Json TopicsJson(const Status& status)
    {
        auto topics = Json::array();
        for (const auto& topic : status.topics) {
            topics.push_back({ { "index", topic.index }, { "text", topic.text }, { "neverSaid", topic.neverSaid } });
        }
        return topics;
    }

    Json LinesSince(std::uint64_t since)
    {
        auto out = Json::array();
        for (const auto& line : lines) {
            if (line.seq > since) {
                out.push_back({ { "seq", line.seq }, { "speaker", line.speaker }, { "role", line.role }, { "text", line.text } });
            }
        }
        return out;
    }

    Json StatusJson(const Status& status)
    {
        Json out{ { "open", status.open }, { "phase", status.Phase() }, { "selectable", status.selectable },
            { "speaker", status.speaker }, { "greeting", status.greeting }, { "waitingToAdvance", status.waitingToAdvance },
            { "canSkip", status.canSkip }, { "topics", TopicsJson(status) } };
        if (!status.overlay.empty()) {
            out["submenu"] = status.overlay;
        }
        {
            std::lock_guard lock(sharedMutex);
            out["cursor"] = lineSeq;
            out["lastLine"] = lines.empty() ? Json(nullptr) :
                Json{ { "speaker", lines.back().speaker }, { "role", lines.back().role }, { "text", lines.back().text } };
        }
        return out;
    }

    bool SkipLine()
    {
        const auto view = menu::RequireMovie(dialogueMenu);
        return menu::CallDelegate(*view, "SkipText", {});
    }

    bool PressCancel()
    {
        const auto view = menu::RequireMovie(dialogueMenu);
        RE::GFxValue menu;
        if (!view->GetVariable(&menu, "_root.DialogueMenu_mc")) {
            return false;
        }
        return menu.Invoke("onCancelPress", nullptr, nullptr, 0);
    }

    void HideMenu(const char* name = dialogueMenu)
    {
        if (const auto queue = RE::UIMessageQueue::GetSingleton()) {
            queue->AddMessage(RE::BSFixedString(name), RE::UI_MESSAGE_TYPE::kHide, nullptr);
        }
    }

    // Runs on the UI thread. Resolves waiters and publishes phase changes.
    void Tick()
    {
        tickPending = false;
        Status status;
        try {
            status = Evaluate();
        } catch (const RpcError&) {
            return;
        }
        const auto now = Clock::now();
        if (status.selectable) {
            if (!selectableSince) {
                selectableSince = now;
            }
        } else {
            selectableSince.reset();
        }
        if (status.speaking) {
            if (!speakingSince) {
                speakingSince = now;
                std::lock_guard lock(sharedMutex);
                speakStartSeq = lineSeq;
            }
        } else {
            speakingSince.reset();
        }
        const bool settled = selectableSince && now - *selectableSince >= settleTime;
        if (status.overlay.empty()) {
            overlaySince.reset();
        } else if (!overlaySince) {
            overlaySince = now;
        }
        const bool overlayAged = overlaySince && now - *overlaySince >= settleTime;
        // Skip only after the line's text was recorded (its subtitle shows up a moment after the line
        // starts), or after a while when no subtitle comes.
        bool lineAged = false;
        if (speakingSince) {
            std::lock_guard lock(sharedMutex);
            lineAged = (lineSeq > speakStartSeq && now - *speakingSince >= 150ms) || now - *speakingSince >= 1500ms;
        }
        const auto statusJson = StatusJson(status);
        const std::string phase = status.Phase();

        std::vector<std::pair<ReplyPtr, Json>> done;
        bool phaseChanged = false;
        {
            std::lock_guard lock(sharedMutex);
            if (phase != lastPhase) {
                lastPhase = phase;
                phaseChanged = true;
            }
            for (auto it = waiters.begin(); it != waiters.end();) {
                auto& waiter = *it;
                std::optional<std::string> result;
                const auto waited = std::chrono::duration_cast<std::chrono::milliseconds>(now - waiter.began).count();
                if (status.open) {
                    waiter.sawOpen = true;
                }
                if (waiter.reply->Done()) {
                    it = waiters.erase(it);
                    continue;
                }
                if (waiter.kind == Kind::Open) {
                    if (status.open) {
                        result = "open";
                    }
                } else if (waiter.kind == Kind::Closed) {
                    if (!status.open) {
                        result = "closed";
                    } else if (!status.overlay.empty()) {
                        // Exit the submenu with the player's key; hiding it directly only after that failed.
                        if (now - waiter.lastSkip > 800ms) {
                            waiter.lastSkip = now;
                            if (waited > 3000 || !TapControl("Cancel")) {
                                HideMenu(status.overlay.c_str());
                            }
                        }
                    } else if (!waiter.cancelSent) {
                        // The player's own exit key lets the engine end the conversation for the NPC too;
                        // calling the menu's ActionScript exit instead left the NPC unable to start another one.
                        waiter.cancelSent = true;
                        waiter.lastSkip = now;
                        if (!TapControl("Cancel") && !PressCancel()) {
                            HideMenu();
                            waiter.hideSent = true;
                        }
                    } else if (!waiter.hideSent && now - waiter.lastSkip > 1500ms) {
                        waiter.lastSkip = now;
                        if (waited > 3000) {
                            waiter.hideSent = true;
                            HideMenu();
                        } else {
                            TapControl("Cancel");
                        }
                    }
                } else {
                    if (!status.open && (waiter.sawOpen || now >= waiter.openGrace)) {
                        result = "closed";
                    } else if (status.open && !status.overlay.empty() && overlayAged) {
                        result = "submenu";
                    } else if (status.open && settled) {
                        result = status.topics.empty() ? "idle" : "topics";
                    } else if (waiter.skip && status.open && status.speaking && lineAged && status.canSkip && now - waiter.lastSkip > 250ms) {
                        waiter.lastSkip = now;
                        SkipLine();
                        speakingSince = now;
                        speakStartSeq = lineSeq;
                    }
                }
                if (result) {
                    Json out{ { "status", *result }, { "waitedMs", waited }, { "lines", LinesSince(waiter.since) } };
                    for (const auto& [key, value] : statusJson.items()) {
                        if (key != "cursor") {
                            out[key] = value;
                        }
                    }
                    out["cursor"] = lineSeq;
                    mark = lineSeq;
                    done.emplace_back(waiter.reply, std::move(out));
                    it = waiters.erase(it);
                } else {
                    ++it;
                }
            }
        }
        if (phaseChanged) {
            events::Emit("dialoguePhase", { { "phase", phase }, { "speaker", status.speaker } });
        }
        for (auto& [reply, body] : done) {
            reply->Ok(std::move(body));
        }
    }

    void SampleLines(const RE::MenuTopicManager& manager, std::set<std::pair<std::uint32_t, std::string>>& shown)
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        const auto npc = Speaker(manager);
        std::set<std::pair<std::uint32_t, std::string>> current;
        std::vector<Line> fresh;
        for (const auto& line : menu::SubtitleSnapshot()) {
            const bool isPlayer = player && line.speaker == player->GetHandle();
            if (!isPlayer && line.speaker != npc) {
                continue;
            }
            const std::pair<std::uint32_t, std::string> key{ line.speaker.native_handle(), line.text };
            current.insert(key);
            if (!shown.contains(key)) {
                fresh.push_back({ 0, line.name, isPlayer ? "player" : "npc", line.text });
            }
        }
        shown = std::move(current);
        for (auto& line : fresh) {
            {
                std::lock_guard lock(sharedMutex);
                line.seq = ++lineSeq;
                lines.push_back(line);
                if (lines.size() > lineLimit) {
                    lines.pop_front();
                }
            }
            events::Emit("dialogueLine", { { "seq", line.seq }, { "speaker", line.speaker }, { "role", line.role }, { "text", line.text } });
        }
    }

    // Reads game state off the UI thread only where it is safe (flags, handles, locked arrays); everything
    // else is scheduled onto the UI thread through Tick.
    void Watch()
    {
        std::set<std::pair<std::uint32_t, std::string>> shown;
        bool wasActive = false;
        while (true) {
            std::this_thread::sleep_for(pollInterval);
            if (!state::dataLoaded) {
                continue;
            }
            const auto manager = RE::MenuTopicManager::GetSingleton();
            const bool active = manager && (manager->menuOpen || manager->currentTopicInfo != nullptr);
            if (manager && active) {
                SampleLines(*manager, shown);
            } else {
                shown.clear();
            }
            std::vector<Waiter> expired;
            bool anyWaiter = false;
            Json snapshot;
            {
                std::lock_guard lock(sharedMutex);
                const auto now = Clock::now();
                for (auto it = waiters.begin(); it != waiters.end();) {
                    if (it->reply->Done()) {
                        it = waiters.erase(it);
                    } else if (now >= it->deadline) {
                        expired.push_back(*it);
                        mark = lineSeq;
                        it = waiters.erase(it);
                    } else {
                        ++it;
                    }
                }
                anyWaiter = !waiters.empty();
                snapshot = lastStatus;
            }
            for (const auto& waiter : expired) {
                const auto& reply = waiter.reply;
                // The UI thread did not get to answer in time (or the conversation did not settle).
                Json body{ { "status", "timeout" }, { "lines", Json::array() },
                    { "waitedMs", std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - waiter.began).count() } };
                {
                    std::lock_guard lock(sharedMutex);
                    body["lines"] = LinesSince(waiter.since);
                }
                if (snapshot.is_object()) {
                    body["state"] = snapshot;
                }
                reply->Ok(std::move(body));
            }
            if ((active || anyWaiter || wasActive) && !tickPending.exchange(true)) {
                SKSE::GetTaskInterface()->AddUITask([] {
                    Tick();
                    try {
                        auto json = StatusJson(Evaluate());
                        std::lock_guard lock(sharedMutex);
                        lastStatus = std::move(json);
                    } catch (const RpcError&) {
                    }
                });
            }
            wasActive = active || anyWaiter;
        }
    }

    void EnsureWatcher()
    {
        if (!watcherStarted.exchange(true)) {
            std::thread(Watch).detach();
        }
    }

    void MarkNow()
    {
        std::lock_guard lock(sharedMutex);
        mark = lineSeq;
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

    std::chrono::milliseconds Timeout(const Json& params, std::chrono::milliseconds fallback)
    {
        const auto ms = Optional<std::int64_t>(params, "timeoutMs", fallback.count());
        if (ms < 100 || ms > 120000) {
            throw RpcError(code::invalidParams, "timeoutMs is between 100 and 120000");
        }
        return std::chrono::milliseconds(ms);
    }

    void AddWaiter(Waiter waiter)
    {
        EnsureWatcher();
        std::lock_guard lock(sharedMutex);
        waiters.push_back(std::move(waiter));
    }
}

DEVTOOLS_MODULE(MenuDialogue)
{
    events::Declare("dialogueLine", "A line was spoken in a conversation: {seq, speaker, role: player|npc, text}");
    events::Declare("dialoguePhase", "The conversation changed phase: {phase: closed|npc-speaking|speaking|advancing|topics, speaker}");
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            EnsureWatcher();
        }
    });

    Register("Dialogue.state", [](const Json& params) {
        AllowOnly(params, { "debug" });
        const auto status = Evaluate();
        Json out = StatusJson(status);
        // The subtitles the engine shows right now, limited to the two people in the conversation.
        auto subtitles = Json::array();
        for (const auto& line : menu::SubtitleSnapshot()) {
            const auto player = RE::PlayerCharacter::GetSingleton();
            const auto manager = RE::MenuTopicManager::GetSingleton();
            const bool involved = (player && line.speaker == player->GetHandle()) || (manager && line.speaker == Speaker(*manager));
            if (involved || !status.open) {
                subtitles.push_back({ { "speaker", line.name }, { "text", line.text } });
            }
        }
        out["subtitles"] = subtitles;
        if (Optional(params, "debug", false)) {
            if (const auto manager = RE::MenuTopicManager::GetSingleton()) {
                Json debug{ { "menuOpen", manager->menuOpen }, { "forceGoodbye", manager->forceGoodbye }, { "shutMenu", manager->shutMenu },
                    { "advanceRequested", manager->advanceRequested }, { "skipHeadResponse", manager->skipHeadResponse },
                    { "advanceInProgress", manager->advanceInProgress }, { "lineSkipped", manager->lineSkipped },
                    { "closeInProgress", manager->closeInProgress },
                    { "currentTopicInfo", manager->currentTopicInfo ? Json(Hex(manager->currentTopicInfo)) : Json(nullptr) } };
                out["debug"] = debug;
            }
        }
        return out;
    }, { .thread = Thread::UI });

    Register("Dialogue.select", [](const Json& params) {
        AllowOnly(params, { "index", "text", "force" });
        auto& ui = RequireUI();
        if (!ui.IsMenuOpen(dialogueMenu)) {
            throw RpcError(code::notFound, "No dialogue is open");
        }
        const auto manager = RE::MenuTopicManager::GetSingleton();
        if (!manager) {
            throw RpcError(code::notReady, "Dialogue manager is unavailable");
        }
        const auto status = Evaluate();
        if (!status.selectable && !Optional(params, "force", false)) {
            throw RpcError(code::busy, "A line is still being spoken; Dialogue.waitIdle waits for the topics, Dialogue.advance skips the line",
                Json{ { "phase", status.Phase() } });
        }
        std::vector<std::string> labels;
        for (const auto& topic : status.topics) {
            labels.push_back(topic.text);
        }
        const int pick = menu::PickIndex(params, labels, "topic");
        const auto view = menu::RequireMovie(dialogueMenu);
        EnsureWatcher();
        MarkNow();
        // The menu's own TopicClicked handler, the one a player's click reaches through GameDelegate.
        const bool called = menu::CallDelegate(*view, "TopicClicked", { RE::GFxValue(static_cast<double>(status.topics[pick].index)) });
        std::lock_guard lock(sharedMutex);
        return Json{ { "index", status.topics[pick].index }, { "text", status.topics[pick].text }, { "called", called }, { "cursor", lineSeq } };
    }, { .thread = Thread::UI });

    Register("Dialogue.advance", [](const Json& params) {
        AllowOnly(params, {});
        auto& ui = RequireUI();
        if (!ui.IsMenuOpen(dialogueMenu)) {
            throw RpcError(code::notFound, "No dialogue is open");
        }
        const auto status = Evaluate();
        if (!status.speaking) {
            return Json{ { "skipped", false }, { "phase", status.Phase() }, { "reason", "no line is being spoken" } };
        }
        EnsureWatcher();
        const bool called = SkipLine();
        return Json{ { "skipped", called }, { "phase", "advancing" } };
    }, { .thread = Thread::UI });

    RegisterDeferred("Dialogue.waitIdle", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "timeoutMs", "skip", "since", "openGraceMs" });
        const auto timeout = Timeout(params, 30000ms);
        const auto now = Clock::now();
        Waiter waiter;
        waiter.kind = Kind::Idle;
        waiter.reply = reply;
        waiter.began = now;
        waiter.deadline = now + timeout;
        waiter.openGrace = now + std::chrono::milliseconds(Optional<std::int64_t>(params, "openGraceMs", 1500));
        waiter.skip = Optional(params, "skip", false);
        {
            std::lock_guard lock(sharedMutex);
            waiter.since = params.contains("since") ? Optional<std::uint64_t>(params, "since", 0) : mark;
        }
        AddWaiter(std::move(waiter));
    }, { .thread = Thread::Any, .timeout = 125s });

    RegisterDeferred("Dialogue.close", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "timeoutMs" });
        const auto timeout = Timeout(params, 5000ms);
        const auto now = Clock::now();
        Waiter waiter;
        waiter.kind = Kind::Closed;
        waiter.reply = reply;
        waiter.began = now;
        waiter.deadline = now + timeout;
        {
            std::lock_guard lock(sharedMutex);
            waiter.since = mark;
        }
        AddWaiter(std::move(waiter));
    }, { .thread = Thread::Any, .timeout = 125s });

    RegisterDeferred("Dialogue.start", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "refID", "loadEpoch", "wait" });
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
        const bool wait = Optional(params, "wait", true);
        MarkNow();
        const bool activated = target->ActivateRef(player, 0, nullptr, 0, false);
        auto out = Describe(target);
        out["activated"] = activated;
        if (!wait || !activated) {
            reply->Ok(std::move(out));
            return;
        }
        // Reply once the menu is up (or after the grace time), with the description of the actor.
        const auto now = Clock::now();
        Waiter waiter;
        waiter.kind = Kind::Open;
        waiter.reply = std::make_shared<Reply>([reply, out](Json body) mutable {
            if (body.contains("result")) {
                out["opened"] = body["result"].value("status", "") == "open";
                out["phase"] = body["result"].value("status", "") == "open" ? "open" : "closed";
                reply->Ok(std::move(out));
            } else {
                reply->Fail(body["error"].value("code", code::internal), body["error"].value("message", "failed"));
            }
        });
        waiter.began = now;
        waiter.deadline = now + 3000ms;
        AddWaiter(std::move(waiter));
    }, { .thread = Thread::Main, .timeout = 8s });
}
