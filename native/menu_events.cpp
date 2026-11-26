// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// HUD notifications and subtitles as events. Notifications come from a detour on the game's
// notification function; subtitles are read from the subtitle manager while someone subscribes.
#include "events.h"
#include "game.h"
#include "menu_util.h"
#include "methods.h"

#include <MinHook.h>

#include <atomic>
#include <chrono>
#include <deque>
#include <mutex>
#include <set>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    using ShowFn = void (*)(const char*, const char*, bool);
    ShowFn originalShow = nullptr;

    std::mutex noticeMutex;
    std::deque<std::pair<std::uint64_t, std::string>> notices;
    std::uint64_t noticeSequence = 0;
    constexpr std::size_t noticeLimit = 200;

    void HookedShow(const char* text, const char* sound, bool cancelIfQueued)
    {
        if (text && *text) {
            std::uint64_t seq = 0;
            {
                std::lock_guard lock(noticeMutex);
                seq = ++noticeSequence;
                notices.emplace_back(seq, text);
                if (notices.size() > noticeLimit) {
                    notices.pop_front();
                }
            }
            events::Emit("hudNotification", { { "text", text }, { "seq", seq } });
        }
        originalShow(text, sound, cancelIfQueued);
    }

    bool InstallHook()
    {
        REL::Relocation<std::uintptr_t> target{ RELOCATION_ID(52050, 52933) };
        auto* address = reinterpret_cast<void*>(target.address());
        return MH_CreateHook(address, reinterpret_cast<void*>(&HookedShow), reinterpret_cast<void**>(&originalShow)) == MH_OK &&
               MH_EnableHook(address) == MH_OK;
    }

    std::string SpeakerName(RE::ObjectRefHandle handle)
    {
        const auto ref = handle.get();
        const auto name = ref ? ref->GetDisplayFullName() : nullptr;
        return name ? name : "";
    }

    std::vector<std::pair<std::string, std::string>> CurrentSubtitles()
    {
        std::vector<std::pair<std::string, std::string>> out;
        for (auto& line : menu::SubtitleSnapshot()) {
            out.emplace_back(std::move(line.name), std::move(line.text));
        }
        return out;
    }

    // Subtitles live for a few seconds in the manager's array; a short poll catches each one.
    void PollSubtitles()
    {
        std::set<std::pair<std::string, std::string>> shown;
        while (true) {
            std::this_thread::sleep_for(100ms);
            if (!state::dataLoaded || !events::Subscribed("subtitle")) {
                shown.clear();
                continue;
            }
            const auto now = CurrentSubtitles();
            std::set<std::pair<std::string, std::string>> current(now.begin(), now.end());
            for (const auto& line : current) {
                if (!shown.contains(line)) {
                    events::Emit("subtitle", { { "speaker", line.first }, { "text", line.second } });
                }
            }
            shown = std::move(current);
        }
    }
}

namespace devtools::menu
{
    std::vector<SubtitleLine> SubtitleSnapshot()
    {
        std::vector<SubtitleLine> out;
        if (const auto manager = RE::SubtitleManager::GetSingleton()) {
            {
                // Only copy under the subtitle lock; resolving a speaker takes other engine locks.
                RE::BSSpinLockGuard guard(manager->lock);
                for (const auto& info : manager->subtitles) {
                    if (info.subtitle.c_str() && *info.subtitle.c_str()) {
                        out.push_back({ info.speaker, {}, info.subtitle.c_str() });
                    }
                }
            }
            for (auto& line : out) {
                line.name = SpeakerName(line.speaker);
            }
        }
        return out;
    }

    Json CurrentSubtitleLines()
    {
        auto out = Json::array();
        for (const auto& [speaker, text] : CurrentSubtitles()) {
            out.push_back({ { "speaker", speaker }, { "text", text } });
        }
        return out;
    }
}

DEVTOOLS_MODULE(MenuEvents)
{
    events::Declare("hudNotification", "A HUD notification was shown: {text, seq}");
    events::Declare("subtitle", "A subtitle appeared: {speaker, text}");
    const bool hooked = InstallHook();
    SetCapability("hudEvents", hooked);
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            // The engine only builds subtitle entries while the preference is on; with it off an
            // observer sees no NPC line at all.
            if (const auto setting = RE::GetINISetting("bDialogueSubtitles:Interface")) {
                setting->data.b = true;
            }
            std::thread(PollSubtitles).detach();
        }
    });

    Register("UI.notifications", [](const Json& params) {
        AllowOnly(params, { "since" });
        const auto since = Optional<std::uint64_t>(params, "since", 0);
        std::lock_guard lock(noticeMutex);
        auto lines = Json::array();
        for (const auto& [seq, text] : notices) {
            if (seq > since) {
                lines.push_back({ { "seq", seq }, { "text", text } });
            }
        }
        return Json{ { "notifications", lines }, { "lastSeq", noticeSequence } };
    }, { .thread = Thread::Any });

    Register("UI.subtitles", [](const Json& params) {
        AllowOnly(params, {});
        return Json{ { "subtitles", menu::CurrentSubtitleLines() } };
    }, { .thread = Thread::UI });
}
