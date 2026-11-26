// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Hang evidence: a crash logger covers access violations, but a frozen game leaves nothing behind.
// A watchdog notices that the main thread stopped taking queued tasks and records where it is
// stuck as module+offset frames (no memory contents) in the plugin log.
#include "game.h"
#include "logs.h"
#include "methods.h"

#include <Windows.h>

#include <array>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    constexpr int maxFrames = 40;
    constexpr auto stallThreshold = 6s;
    constexpr auto repeatEvery = 20s;
    constexpr int maxCaptures = 6;

    DWORD gameThreadId = 0;

    struct Frames
    {
        std::array<DWORD64, maxFrames> pc{};
        int count = 0;
    };

    // Plain function without destructors so the structured handler may wrap it: a damaged stack
    // must end the walk, not take the game down from the watchdog thread.
    void Unwind(CONTEXT& context, Frames& out)
    {
        __try {
            while (out.count < maxFrames && context.Rip) {
                out.pc[out.count++] = context.Rip;
                DWORD64 base = 0;
                const auto entry = RtlLookupFunctionEntry(context.Rip, &base, nullptr);
                if (!entry) {
                    break;
                }
                void* handlerData = nullptr;
                DWORD64 frame = 0;
                RtlVirtualUnwind(UNW_FLAG_NHANDLER, base, context.Rip, entry, &context, &handlerData, &frame, nullptr);
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
    }

    Frames Walk(HANDLE thread)
    {
        Frames out;
        if (SuspendThread(thread) == static_cast<DWORD>(-1)) {
            return out;
        }
        CONTEXT context{};
        context.ContextFlags = CONTEXT_FULL;
        if (GetThreadContext(thread, &context)) {
            Unwind(context, out);
        }
        ResumeThread(thread);
        return out;
    }

    std::string Describe(DWORD64 pc)
    {
        void* base = nullptr;
        if (!RtlPcToFileHeader(reinterpret_cast<void*>(pc), &base) || !base) {
            return "?";
        }
        std::array<char, MAX_PATH> path{};
        const auto size = GetModuleFileNameA(static_cast<HMODULE>(base), path.data(), static_cast<DWORD>(path.size()));
        std::string name(path.data(), size);
        name = name.substr(name.find_last_of("\\/") + 1);
        std::array<char, 24> offset{};
        std::snprintf(offset.data(), offset.size(), "+0x%llX", static_cast<unsigned long long>(pc - reinterpret_cast<DWORD64>(base)));
        return name + offset.data();
    }

    void ReportStall(std::int64_t stalledMs)
    {
        const HANDLE thread = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, gameThreadId);
        if (!thread) {
            return;
        }
        const auto frames = Walk(thread);
        CloseHandle(thread);
        std::string text = "main thread has not run a queued task for " + std::to_string(stalledMs) + " ms; frames:";
        for (int i = 0; i < frames.count; ++i) {
            text += ' ' + Describe(frames.pc[i]);
        }
        logs::Plugin("warning", text);
    }

    void Watch()
    {
        std::atomic<std::int64_t> sent = 0;
        auto lastReport = std::chrono::steady_clock::time_point{};
        int reports = 0;
        const auto nowMs = [] {
            return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
        };
        // The flag outlives this thread's frame because a queued task may run after it returns.
        auto* flag = new std::atomic_bool(false);
        for (;;) {
            std::this_thread::sleep_for(1s);
            const auto tasks = SKSE::GetTaskInterface();
            if (!tasks) {
                continue;
            }
            if (!flag->load()) {
                flag->store(true);
                sent = nowMs();
                tasks->AddTask([flag] { flag->store(false); });
                continue;
            }
            const auto stalled = nowMs() - sent.load();
            const auto now = std::chrono::steady_clock::now();
            if (stalled >= std::chrono::duration_cast<std::chrono::milliseconds>(stallThreshold).count() &&
                reports < maxCaptures && now - lastReport >= repeatEvery) {
                lastReport = now;
                ++reports;
                ReportStall(stalled);
            }
        }
    }
}

DEVTOOLS_MODULE(Diagnostics)
{
    // The module installs from the plugin load callback, which runs on the game's main thread.
    gameThreadId = GetCurrentThreadId();
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        static std::atomic_bool started = false;
        if (message.type == SKSE::MessagingInterface::kDataLoaded && !started.exchange(true)) {
            std::thread(Watch).detach();
        }
    });
}
