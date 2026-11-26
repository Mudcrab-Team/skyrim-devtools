// SPDX-License-Identifier: GPL-3.0-or-later
// Spans for the profile timeline: menus (loading screens are cell loads) and markers a client adds with
// Profile.mark. Marks are kept only while a profile runs.
#include "game.h"
#include "methods.h"
#include "perf.h"
#include "profiler.h"

#include <mutex>
#include <string>
#include <unordered_map>

using namespace devtools;

namespace
{
    class MarkSink final : public RE::BSTEventSink<RE::MenuOpenCloseEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::MenuOpenCloseEvent* event,
            RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
        {
            if (!event) {
                return RE::BSEventNotifyControl::kContinue;
            }
            const double now = perf::Now();
            const std::string menu = event->menuName.c_str();
            std::lock_guard lock(mutex);
            if (event->opening) {
                opened[menu] = now;
            } else if (const auto it = opened.find(menu); it != opened.end()) {
                profiler::Mark(menu == "Loading Menu" ? "load" : "menu", menu, it->second, now);
                opened.erase(it);
            }
            return RE::BSEventNotifyControl::kContinue;
        }

    private:
        std::mutex mutex;
        std::unordered_map<std::string, double> opened;
    };
}

DEVTOOLS_MODULE(ProfilerMarks)
{
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            static MarkSink sink;
            if (const auto ui = RE::UI::GetSingleton()) {
                ui->AddEventSink<RE::MenuOpenCloseEvent>(&sink);
            }
        }
    });

    Register("Profile.mark", [](const Json& params) {
        AllowOnly(params, { "kind", "name", "durationMs" });
        const auto kind = Optional<std::string>(params, "kind", "marker");
        const auto name = Require<std::string>(params, "name");
        const double duration = Optional<double>(params, "durationMs", 0.0);
        if (duration < 0.0 || duration > 600000.0 || kind.size() > 32 || name.size() > 512) {
            throw RpcError(code::invalidParams, "kind up to 32 chars, name up to 512, durationMs 0 to 600000");
        }
        const double now = perf::Now();
        profiler::Mark(kind, name, now - duration / 1000.0, now);
        return Json{ { "recorded", profiler::Profiling() } };
    }, { .thread = Thread::Any });
}
