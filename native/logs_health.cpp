// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Liveness of the game's main and UI threads, answered from the connection thread so a hung
// game thread can never make the check itself hang.
#include "game.h"
#include "methods.h"

#include <chrono>

namespace
{
    using namespace devtools;

    std::int64_t NowMs()
    {
        using namespace std::chrono;
        return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
    }

    // At most one probe task is queued per thread, so a stalled thread never accumulates a backlog.
    struct Probe
    {
        std::atomic_bool inFlight = false;
        std::atomic<std::int64_t> sentMs = 0;
        std::atomic<std::int64_t> doneMs = 0;
        std::atomic<std::int64_t> latencyMs = -1;
        std::atomic<std::uint64_t> completed = 0;

        void Poke(bool ui)
        {
            bool expected = false;
            if (!inFlight.compare_exchange_strong(expected, true)) {
                return;
            }
            const auto sent = NowMs();
            sentMs = sent;
            const auto tasks = SKSE::GetTaskInterface();
            const auto done = [this, sent] {
                const auto now = NowMs();
                latencyMs = now - sent;
                doneMs = now;
                ++completed;
                inFlight = false;
            };
            if (!tasks) {
                inFlight = false;
                return;
            }
            ui ? tasks->AddUITask(done) : tasks->AddTask(done);
        }

        Json Report(std::int64_t now) const
        {
            Json out{ { "completed", completed.load() } };
            out["stalledMs"] = inFlight ? now - sentMs.load() : 0;
            const auto done = doneMs.load();
            out["lastTickAgeMs"] = done ? Json(now - done) : Json(nullptr);
            const auto latency = latencyMs.load();
            out["lastLatencyMs"] = latency >= 0 ? Json(latency) : Json(nullptr);
            return out;
        }
    };

    Probe mainProbe;
    Probe uiProbe;
    const std::int64_t startedMs = NowMs();
}

DEVTOOLS_MODULE(Health)
{
    SetCapability("heartbeat", true);

    // A client that sees stalledMs keep growing across calls knows the thread stopped turning over
    // while the connection thread still answers: a hung game rather than a crashed one.
    Register("Health.heartbeat", [](const Json& params) {
        AllowOnly(params, {});
        const auto now = NowMs();
        mainProbe.Poke(false);
        uiProbe.Poke(true);
        return Json{ { "uptimeMs", now - startedMs }, { "main", mainProbe.Report(now) }, { "ui", uiProbe.Report(now) } };
    }, { .thread = Thread::Any });
}
