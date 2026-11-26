// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Sampling CPU profiler. A sampler thread suspends each target thread, reads its context, copies the
// live part of its stack and resumes it, then walks the copy with the modules' own unwind tables.
// While a thread is suspended the sampler takes no lock and allocates nothing: the target may own the
// heap or loader lock. A thread whose
// cycle counter has not moved since its last sample is not suspended at all; its previous stack is
// recorded again as waiting, which keeps sampling all threads cheap and marks off-CPU time.
// Addresses are resolved only when a profile is read: module, RVA, the unwind-table function start,
// the Address Library ID for the game and DbgHelp names for DLLs. Names for game code are added by
// the client from local symbol tables; none ship with the plugin.
#include "build_info.h"
#include "game.h"
#include "logs.h"
#include "methods.h"
#include "perf.h"
#include "profiler.h"
#include "thread_names.h"
#include "trace.h"

#include <Windows.h>
#include <DbgHelp.h>
#include <TlHelp32.h>
#include <psapi.h>
#include <wct.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cmath>
#include <cstring>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#pragma comment(lib, "dbghelp.lib")
#pragma comment(lib, "advapi32.lib")

namespace
{
    using namespace devtools;

    constexpr int maxDepth = 160;
    constexpr std::size_t ringSize = 8192;          // main-thread samples kept for hitch capture
    constexpr std::size_t maxHitches = 64;
    constexpr std::size_t maxSessionRuns = 2'000'000;
    constexpr std::size_t chunkBytes = 4 * 1024 * 1024;
    constexpr std::uint32_t flagWaiting = 1;        // the thread did not run since its previous sample
    constexpr std::uint32_t flagReused = 2;         // the stack was not walked again (it cannot have changed)
    constexpr std::uint32_t flagThrottled = 4;      // skipped to stay inside the suspension budget; holds the last stack

    // ---- modules and unwind tables -----------------------------------------------------------

    struct Module
    {
        std::uintptr_t base = 0;
        std::uintptr_t end = 0;
        const RUNTIME_FUNCTION* table = nullptr;
        DWORD count = 0;
        std::wstring path;
    };

    // Owned and swapped by the sampler thread only, so a walk never sees it change.
    std::vector<Module> modules;

    void ReadUnwindTable(Module& module)
    {
        const auto base = module.base;
        const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
        if (dos->e_magic != IMAGE_DOS_SIGNATURE) {
            return;
        }
        const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
        if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) {
            return;
        }
        const auto& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXCEPTION];
        if (dir.VirtualAddress && dir.Size) {
            module.table = reinterpret_cast<const RUNTIME_FUNCTION*>(base + dir.VirtualAddress);
            module.count = dir.Size / sizeof(RUNTIME_FUNCTION);
        }
    }

    void TryReadUnwindTable(Module& module)
    {
        __try {
            ReadUnwindTable(module);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
    }

    std::vector<Module> SnapshotModules()
    {
        std::vector<HMODULE> handles(1024);
        DWORD needed = 0;
        const HANDLE self = GetCurrentProcess();
        if (!EnumProcessModulesEx(self, handles.data(), static_cast<DWORD>(handles.size() * sizeof(HMODULE)), &needed, LIST_MODULES_64BIT)) {
            return {};
        }
        handles.resize(std::min<std::size_t>(handles.size(), needed / sizeof(HMODULE)));
        std::vector<Module> out;
        out.reserve(handles.size());
        for (const auto handle : handles) {
            MODULEINFO info{};
            if (!GetModuleInformation(self, handle, &info, sizeof(info))) {
                continue;
            }
            Module module;
            module.base = reinterpret_cast<std::uintptr_t>(info.lpBaseOfDll);
            module.end = module.base + info.SizeOfImage;
            wchar_t path[MAX_PATH * 2]{};
            GetModuleFileNameW(handle, path, static_cast<DWORD>(std::size(path)));
            module.path = path;
            TryReadUnwindTable(module);
            out.push_back(std::move(module));
        }
        std::ranges::sort(out, {}, &Module::base);
        return out;
    }

    bool ModuleSetChanged(const std::vector<Module>& list)
    {
        std::vector<HMODULE> handles(1024);
        DWORD needed = 0;
        if (!EnumProcessModulesEx(GetCurrentProcess(), handles.data(), static_cast<DWORD>(handles.size() * sizeof(HMODULE)), &needed, LIST_MODULES_64BIT)) {
            return false;
        }
        handles.resize(std::min<std::size_t>(handles.size(), needed / sizeof(HMODULE)));
        // Only a module the list lacks matters; a stale entry for an unloaded one is harmless.
        return std::ranges::any_of(handles, [&](HMODULE handle) {
            const auto base = reinterpret_cast<std::uintptr_t>(handle);
            const auto it = std::ranges::lower_bound(list, base, {}, &Module::base);
            return it == list.end() || it->base != base;
        });
    }

    const Module* FindModule(const std::vector<Module>& list, std::uintptr_t address)
    {
        auto it = std::upper_bound(list.begin(), list.end(), address, [](std::uintptr_t a, const Module& m) { return a < m.base; });
        if (it == list.begin()) {
            return nullptr;
        }
        --it;
        return address < it->end ? &*it : nullptr;
    }

    const RUNTIME_FUNCTION* FindEntry(const Module& module, std::uintptr_t address)
    {
        if (!module.table) {
            return nullptr;
        }
        const auto rva = static_cast<DWORD>(address - module.base);
        std::size_t low = 0;
        std::size_t high = module.count;
        while (low < high) {
            const std::size_t mid = (low + high) / 2;
            const auto& entry = module.table[mid];
            if (rva < entry.BeginAddress) {
                high = mid;
            } else if (rva >= entry.EndAddress) {
                low = mid + 1;
            } else {
                return &entry;
            }
        }
        return nullptr;
    }

    // A chained entry describes a fragment of a larger function; its primary entry holds the start.
    DWORD FunctionStart(const Module& module, const RUNTIME_FUNCTION* entry)
    {
        for (int guard = 0; entry && guard < 32; ++guard) {
            const auto* info = reinterpret_cast<const std::uint8_t*>(module.base + entry->UnwindData);
            const std::uint8_t flags = info[0] >> 3;
            if (!(flags & UNW_FLAG_CHAININFO)) {
                return entry->BeginAddress;
            }
            const std::uint8_t codes = info[2];
            const auto* codesStart = reinterpret_cast<const std::uint16_t*>(info + 4);
            entry = reinterpret_cast<const RUNTIME_FUNCTION*>(codesStart + ((codes + 1) & ~1));
        }
        return entry ? entry->BeginAddress : 0;
    }

    const RUNTIME_FUNCTION* TryFindEntry(const Module& module, std::uintptr_t address)
    {
        __try {
            return FindEntry(module, address);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            return nullptr;
        }
    }

    // ---- the stack walk ----------------------------------------------------------------------

    // Bytes of a suspended thread's stack copied per sample. Deeper stacks are cut where the copy ends.
    constexpr std::size_t stackCopyMax = 256 * 1024;

    struct Stack
    {
        std::uint64_t pc[maxDepth];
        int depth = 0;
        alignas(16) std::uint8_t copy[stackCopyMax];
        std::size_t copied = 0;
    };

    // Runs while the target is suspended, so it only copies: the walk happens after the thread resumes,
    // which keeps the suspension to a context read and one memcpy of the live part of the stack.
    bool CopyStack(const CONTEXT& context, std::uintptr_t stackHigh, Stack& out)
    {
        out.copied = 0;
        if (context.Rsp >= stackHigh || context.Rsp & 7) {
            return false;
        }
        const std::size_t size = std::min<std::size_t>(stackHigh - context.Rsp, stackCopyMax);
        __try {
            std::memcpy(out.copy, reinterpret_cast<const void*>(context.Rsp), size);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            return false;
        }
        out.copied = size;
        return true;
    }

    // Moves every register and stack word that points into the original stack range onto the copy, so
    // frame pointers saved in the stack and restored by the unwinder land in the copy too.
    void RebaseOntoCopy(CONTEXT& context, Stack& stack)
    {
        const std::uintptr_t low = context.Rsp;
        const std::uintptr_t high = low + stack.copied;
        const auto delta = reinterpret_cast<std::uintptr_t>(stack.copy) - low;
        const auto rebase = [&](DWORD64& value) {
            if (value >= low && value < high) {
                value += delta;
            }
        };
        for (DWORD64* reg = &context.Rax; reg <= &context.R15; ++reg) {
            rebase(*reg);
        }
        auto* words = reinterpret_cast<DWORD64*>(stack.copy);
        for (std::size_t i = 0; i < stack.copied / 8; ++i) {
            rebase(words[i]);
        }
    }

    // No destructors here so the structured handler may wrap it: a torn stack ends the walk.
    // Caller frames are stored as return address minus one so they resolve inside the call.
    // The context has been rebased onto the stack copy, so every read stays inside the copy.
    void Walk(CONTEXT& context, Stack& out)
    {
        out.depth = 0;
        const auto stackLow = reinterpret_cast<std::uintptr_t>(out.copy);
        const std::uintptr_t stackHigh = stackLow + out.copied;
        __try {
            while (out.depth < maxDepth && context.Rip) {
                out.pc[out.depth] = out.depth == 0 ? context.Rip : context.Rip - 1;
                ++out.depth;
                const auto* module = FindModule(modules, context.Rip);
                const RUNTIME_FUNCTION* entry = module ? FindEntry(*module, context.Rip) : nullptr;
                const auto rspBefore = context.Rsp;
                if (entry) {
                    void* handlerData = nullptr;
                    DWORD64 establisher = 0;
                    RtlVirtualUnwind(UNW_FLAG_NHANDLER, module->base, context.Rip, const_cast<PRUNTIME_FUNCTION>(entry), &context,
                        &handlerData, &establisher, nullptr);
                } else if (out.depth == 1) {
                    // A leaf function has no unwind entry: its return address is on top of the stack.
                    if (context.Rsp < stackLow || context.Rsp + 8 > stackHigh) {
                        break;
                    }
                    context.Rip = *reinterpret_cast<const DWORD64*>(context.Rsp);
                    context.Rsp += 8;
                } else {
                    break;  // code outside every image (a hook trampoline) has no way to unwind
                }
                if (context.Rsp <= rspBefore || context.Rsp >= stackHigh || context.Rsp < stackLow) {
                    break;
                }
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
    }

    // ---- interning: stacks as a prefix tree, root first ------------------------------------

    struct Interner
    {
        std::vector<std::uint64_t> frames;
        std::unordered_map<std::uint64_t, std::uint32_t> frameIndex;
        std::vector<std::pair<std::int32_t, std::uint32_t>> stacks;  // prefix (-1 = root), frame
        std::unordered_map<std::uint64_t, std::uint32_t> stackIndex;

        std::uint32_t Frame(std::uint64_t pc)
        {
            const auto [it, added] = frameIndex.try_emplace(pc, static_cast<std::uint32_t>(frames.size()));
            if (added) {
                frames.push_back(pc);
            }
            return it->second;
        }

        std::int32_t Add(const Stack& stack)
        {
            std::int32_t prefix = -1;
            for (int i = stack.depth - 1; i >= 0; --i) {
                const auto frame = Frame(stack.pc[i]);
                const std::uint64_t key = (static_cast<std::uint64_t>(prefix + 1) << 32) | frame;
                const auto [it, added] = stackIndex.try_emplace(key, static_cast<std::uint32_t>(stacks.size()));
                if (added) {
                    stacks.emplace_back(prefix, frame);
                }
                prefix = static_cast<std::int32_t>(it->second);
            }
            return prefix;
        }

        void Clear()
        {
            frames.clear();
            frameIndex.clear();
            stacks.clear();
            stackIndex.clear();
        }

        // The frames of one stack, leaf first.
        std::vector<std::uint64_t> Expand(std::int32_t stack) const
        {
            std::vector<std::uint64_t> out;
            for (int guard = 0; stack >= 0 && guard < maxDepth + 1; ++guard) {
                out.push_back(frames[stacks[stack].second]);
                stack = stacks[stack].first;
            }
            return out;
        }
    };

    // ---- targets -----------------------------------------------------------------------------

    using NtQueryInformationThreadFn = LONG(NTAPI*)(HANDLE, ULONG, PVOID, ULONG, PULONG);

    struct ThreadBasicInformation
    {
        LONG exitStatus;
        PVOID tebBaseAddress;
        PVOID clientProcess;
        PVOID clientThread;
        KAFFINITY affinityMask;
        LONG priority;
        LONG basePriority;
    };

    std::uintptr_t StackBase(HANDLE thread)
    {
        static const auto query = reinterpret_cast<NtQueryInformationThreadFn>(
            GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQueryInformationThread"));
        ThreadBasicInformation info{};
        if (!query || query(thread, 0, &info, sizeof(info), nullptr) != 0 || !info.tebBaseAddress) {
            return 0;
        }
        return reinterpret_cast<std::uintptr_t>(static_cast<NT_TIB*>(info.tebBaseAddress)->StackBase);
    }

    std::string Narrow(const std::wstring& text)
    {
        if (text.empty()) {
            return {};
        }
        const int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0, nullptr, nullptr);
        std::string out(static_cast<std::size_t>(size), '\0');
        WideCharToMultiByte(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), out.data(), size, nullptr, nullptr);
        return out;
    }

    // Consecutive samples of one thread with the same stack and state, as one record.
    struct RunRecord
    {
        double at;
        std::int32_t stack;
        std::uint32_t count;
        std::uint32_t flags;
    };

    struct Target
    {
        DWORD tid = 0;
        HANDLE handle = nullptr;
        devtools::threads::Name name;
        std::uintptr_t start = 0;
        bool main = false;
        std::uintptr_t stackHigh = 0;
        std::uint64_t lastCycles = 0;
        std::int32_t lastStack = -1;      // in the session interner
        std::int32_t lastRingStack = -1;  // in the hitch interner
        std::uint64_t samples = 0;
        std::uint64_t walked = 0;
        double suspendedSeconds = 0.0;
        std::uint64_t throttled = 0;
        std::uint32_t lastFlags = 0;
        // Suspension budget: suspended seconds charged against the current window, which slides forward
        // carrying any overspend so a burst of expensive samples is paid back by skipped ticks.
        double budgetWindowStart = -1.0;
        double budgetSpent = 0.0;
        bool gone = false;
        std::vector<RunRecord> runs;
        double lastAt = -1.0;
    };


    struct RingEntry
    {
        double at = 0.0;
        std::int32_t stack = -1;
        std::uint32_t flags = 0;
    };

    struct Hitch
    {
        std::uint64_t frame = 0;
        double start = 0.0;
        double end = 0.0;
        std::vector<std::vector<std::uint64_t>> stacks;  // leaf first
        std::vector<std::tuple<double, std::uint32_t, std::uint32_t>> samples;  // at, local stack, flags
        Json waitChain;
    };

    struct FrameMark
    {
        double start;
        double end;
        std::uint64_t frame;
    };

    struct Session
    {
        double hz = 1000.0;
        double started = 0.0;
        double stopped = 0.0;
        double deadline = 0.0;
        bool traced = false;
        std::string threadsSpec;
        Interner interner;
        std::vector<Target> threads;
        std::size_t runCount = 0;
        std::uint64_t sampleCount = 0;
        std::uint64_t ticks = 0;
        std::uint64_t lateTicks = 0;
        double samplerCpuSeconds = 0.0;
        double suspendBudget = 0.025;
        std::uint32_t otherEvery = 4;
        bool truncated = false;
        // The main-thread sampler outlives sessions (hitch watch, Profile.top), so its totals are read
        // relative to where they stood when this session started.
        double mainSuspendedBase = -1.0;
        std::uint64_t mainThrottledBase = 0;
    };

    // ---- shared state ------------------------------------------------------------------------

    DWORD mainThreadId = 0;
    std::atomic<DWORD> frameThreadId = 0;
    std::atomic_bool profiling = false;
    std::atomic_bool watching = false;
    std::atomic<double> watchHz = 250.0;
    std::atomic<double> hitchThresholdMs = 100.0;
    std::atomic_bool stopRequested = false;
    std::atomic<double> lastFrameEnd = 0.0;
    std::atomic<std::uint64_t> lastFrameNumber = 0;

    std::mutex controlMutex;   // start/stop/watch configuration and the sampler thread handle
    std::thread sampler;
    std::unique_ptr<Session> pending;   // configured by Profile.start, adopted by the sampler
    std::unique_ptr<Session> finished;  // handed back by the sampler when it stops
    std::string serialized;             // the last stopped profile as JSON
    std::string payload;                // `serialized` in the encoding last asked for, read in chunks
    std::string payloadEncoding;
    double serializeMs = 0.0;

    std::mutex frameMutex;
    std::vector<FrameMark> pendingHitches;
    std::vector<FrameMark> sessionFrames;

    std::mutex hitchMutex;
    std::deque<Hitch> hitchLog;
    std::uint64_t hitchesSeen = 0;

    // Profile.top keeps the main-thread ring filling for a few seconds after each call, so a live view
    // samples only while someone is looking and the watch need not stay on.
    std::atomic<double> topLeaseUntil = 0.0;

    bool Watching(double now)
    {
        return watching.load(std::memory_order_relaxed) || now < topLeaseUntil.load(std::memory_order_relaxed);
    }

    // Aggregates of the main-thread ring over the last `seconds`, computed by the sampler thread (which owns
    // the ring) on request.
    struct TopData
    {
        double seconds = 0.0;
        double hz = 0.0;
        std::uint32_t samples = 0;
        std::uint32_t waiting = 0;
        std::vector<std::pair<std::uint64_t, std::uint32_t>> self;
        std::vector<std::pair<std::uint64_t, std::uint32_t>> total;
        std::vector<std::pair<std::uint64_t, std::uint32_t>> waits;
    };
    std::mutex topMutex;
    std::condition_variable topReady;
    std::uint64_t topWanted = 0;
    std::uint64_t topDone = 0;
    double topSeconds = 5.0;
    TopData topData;

    struct MarkRecord
    {
        std::string kind;
        std::string name;
        double start;
        double end;
    };
    constexpr std::size_t maxMarks = 200000;
    std::mutex marksMutex;
    std::vector<MarkRecord> sessionMarks;

    Json WaitChain(DWORD tid)
    {
        const HWCT session = OpenThreadWaitChainSession(0, nullptr);
        if (!session) {
            return nullptr;
        }
        WAITCHAIN_NODE_INFO nodes[WCT_MAX_NODE_COUNT];
        DWORD count = WCT_MAX_NODE_COUNT;
        BOOL cycle = FALSE;
        Json out = nullptr;
        if (GetThreadWaitChain(session, 0, WCTP_GETINFO_ALL_FLAGS, tid, &count, nodes, &cycle)) {
            out = Json{ { "cycle", cycle != FALSE }, { "nodes", Json::array() } };
            static const char* types[] = { "", "CriticalSection", "SendMessage", "Mutex", "Alpc", "Com", "ThreadWait",
                "ProcessWait", "Thread", "ComActivation", "Unknown", "SocketIo", "SmbIo" };
            static const char* states[] = { "", "Running", "Blocked", "PidOnly", "PidOnlyRpcss", "Owned", "NotOwned",
                "Abandoned", "Unknown", "Error" };
            for (DWORD i = 0; i < count && i < WCT_MAX_NODE_COUNT; ++i) {
                const auto& node = nodes[i];
                const auto type = static_cast<std::size_t>(node.ObjectType);
                const auto state = static_cast<std::size_t>(node.ObjectStatus);
                Json entry{ { "type", type < std::size(types) ? types[type] : "?" },
                    { "status", state < std::size(states) ? states[state] : "?" } };
                if (node.ObjectType == WctThreadType) {
                    entry["tid"] = node.ThreadObject.ThreadId;
                    entry["pid"] = node.ThreadObject.ProcessId;
                    entry["waitMs"] = node.ThreadObject.WaitTime;
                } else if (node.LockObject.ObjectName[0]) {
                    entry["name"] = Narrow(node.LockObject.ObjectName);
                }
                out["nodes"].push_back(std::move(entry));
            }
        }
        CloseThreadWaitChainSession(session);
        return out;
    }

    // ---- the sampler -------------------------------------------------------------------------

    bool OpenTarget(DWORD tid, Target& target)
    {
        const HANDLE handle = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE, tid);
        if (!handle) {
            return false;
        }
        target.tid = tid;
        target.handle = handle;
        target.name = devtools::threads::NameOf(tid, handle);
        target.start = devtools::threads::StartAddress(handle);
        target.main = tid == mainThreadId;
        target.stackHigh = StackBase(handle);
        if (!target.stackHigh) {
            CloseHandle(handle);
            return false;
        }
        QueryThreadCycleTime(handle, &target.lastCycles);
        target.lastCycles = ~0ull;  // the first sample always walks
        return true;
    }

    std::vector<DWORD> ProcessThreads()
    {
        std::vector<DWORD> out;
        const HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if (snap == INVALID_HANDLE_VALUE) {
            return out;
        }
        THREADENTRY32 entry{ sizeof(entry) };
        const DWORD pid = GetCurrentProcessId();
        for (BOOL ok = Thread32First(snap, &entry); ok; ok = Thread32Next(snap, &entry)) {
            if (entry.th32OwnerProcessID == pid) {
                out.push_back(entry.th32ThreadID);
            }
        }
        CloseHandle(snap);
        return out;
    }

    bool WantsThread(const std::string& spec, DWORD tid, const std::string& name)
    {
        if (spec == "all") {
            return true;
        }
        if (spec == "main") {
            return tid == mainThreadId;
        }
        if (spec == "frame") {
            return tid == mainThreadId || tid == frameThreadId.load();
        }
        // a comma-separated list of thread ids or name fragments, "main" allowed among them
        std::size_t begin = 0;
        while (begin <= spec.size()) {
            auto end = spec.find(',', begin);
            if (end == std::string::npos) {
                end = spec.size();
            }
            const auto item = spec.substr(begin, end - begin);
            if (!item.empty()) {
                if (item == "main" && tid == mainThreadId) {
                    return true;
                }
                if (std::all_of(item.begin(), item.end(), ::isdigit) && std::stoul(item) == tid) {
                    return true;
                }
                if (!name.empty() && name.find(item) != std::string::npos) {
                    return true;
                }
            }
            begin = end + 1;
        }
        return false;
    }

    void RefreshTargets(Session& session, std::unordered_set<DWORD>& known)
    {
        const DWORD self = GetCurrentThreadId();
        for (const auto tid : ProcessThreads()) {
            if (tid == self || known.contains(tid)) {
                continue;
            }
            known.insert(tid);
            Target target;
            if (!OpenTarget(tid, target)) {
                continue;
            }
            if (!WantsThread(session.threadsSpec, tid, target.name.text)) {
                CloseHandle(target.handle);
                continue;
            }
            session.threads.push_back(std::move(target));
        }
    }

    // True when the thread has used up its share of suspended time; the caller then holds the last stack.
    // Suspending costs the target 15-40 us per sample (the kernel has to interrupt it and deliver the
    // suspend APC), so at 1 kHz a busy thread would otherwise lose 3-5% of its time.
    bool OverBudget(Target& target, double now, double budget)
    {
        if (budget <= 0.0) {
            return false;
        }
        if (target.budgetWindowStart < 0.0) {
            target.budgetWindowStart = now;
        }
        const double elapsed = now - target.budgetWindowStart;
        if (elapsed > 0.1) {
            target.budgetSpent = std::max(0.0, target.budgetSpent - budget * elapsed);
            target.budgetWindowStart = now;
            return target.budgetSpent > 0.0;
        }
        return target.budgetSpent > budget * std::max(elapsed, 0.001);
    }

    // Samples one thread: false when the thread is gone.
    bool SampleThread(Target& target, Stack& scratch, double now, bool& walkedOut)
    {
        walkedOut = false;
        std::uint64_t cycles = 0;
        if (!QueryThreadCycleTime(target.handle, &cycles)) {
            return false;
        }
        if (cycles == target.lastCycles) {
            return true;
        }
        const double before = perf::Now();
        if (SuspendThread(target.handle) == static_cast<DWORD>(-1)) {
            return false;
        }
        CONTEXT context{};
        context.ContextFlags = CONTEXT_CONTROL | CONTEXT_INTEGER;
        bool ok = GetThreadContext(target.handle, &context) != FALSE && CopyStack(context, target.stackHigh, scratch);
        ResumeThread(target.handle);
        const double suspended = perf::Now() - before;
        target.suspendedSeconds += suspended;
        target.budgetSpent += suspended;
        scratch.depth = 0;
        if (ok) {
            RebaseOntoCopy(context, scratch);
            Walk(context, scratch);
        }
        // The cycle count is read again after the walk so the next sample compares like with like.
        QueryThreadCycleTime(target.handle, &cycles);
        target.lastCycles = cycles;
        walkedOut = ok && scratch.depth > 0;
        (void)now;
        return true;
    }

    // Hitch capture: the main-thread ring and the frames the Present hook reported slow.
    struct HitchState
    {
        Interner interner;
        std::vector<RingEntry> ring = std::vector<RingEntry>(ringSize);
        std::size_t head = 0;
        std::size_t count = 0;
        std::uint64_t waitChainFrame = 0;
        Json waitChain;
        double waitChainFrameStart = 0.0;

        void Push(double at, std::int32_t stack, std::uint32_t flags)
        {
            ring[head] = { at, stack, flags };
            head = (head + 1) % ring.size();
            count = std::min(count + 1, ring.size());
        }
    };

    void CollectHitches(HitchState& state, double latest)
    {
        std::vector<FrameMark> ready;
        {
            std::lock_guard lock(frameMutex);
            auto split = std::partition(pendingHitches.begin(), pendingHitches.end(), [&](const FrameMark& f) { return f.end > latest; });
            ready.assign(split, pendingHitches.end());
            pendingHitches.erase(split, pendingHitches.end());
        }
        for (const auto& mark : ready) {
            Hitch hitch;
            hitch.frame = mark.frame;
            hitch.start = mark.start;
            hitch.end = mark.end;
            std::unordered_map<std::int32_t, std::uint32_t> local;
            for (std::size_t i = 0; i < state.count; ++i) {
                const auto& entry = state.ring[(state.head + state.ring.size() - state.count + i) % state.ring.size()];
                if (entry.at < mark.start || entry.at > mark.end || entry.stack < 0) {
                    continue;
                }
                const auto [it, added] = local.try_emplace(entry.stack, static_cast<std::uint32_t>(hitch.stacks.size()));
                if (added) {
                    hitch.stacks.push_back(state.interner.Expand(entry.stack));
                }
                hitch.samples.emplace_back(entry.at, it->second, entry.flags);
            }
            if (std::abs(state.waitChainFrameStart - mark.start) < 1e-6) {
                hitch.waitChain = std::move(state.waitChain);
                state.waitChain = nullptr;
            }
            std::lock_guard lock(hitchMutex);
            ++hitchesSeen;
            hitchLog.push_back(std::move(hitch));
            if (hitchLog.size() > maxHitches) {
                hitchLog.pop_front();
            }
        }
        // The hitch interner only needs the stacks the ring still points at; rebuild it when it grows large.
        if (state.interner.stacks.size() > 2'000'000) {
            state.interner.Clear();
            state.count = 0;
        }
    }

    std::vector<std::pair<std::uint64_t, std::uint32_t>> Largest(const std::unordered_map<std::uint64_t, std::uint32_t>& counts, std::size_t keep)
    {
        std::vector<std::pair<std::uint64_t, std::uint32_t>> out(counts.begin(), counts.end());
        const auto cut = std::min(keep, out.size());
        std::partial_sort(out.begin(), out.begin() + static_cast<std::ptrdiff_t>(cut), out.end(),
            [](const auto& a, const auto& b) { return a.second > b.second; });
        out.resize(cut);
        return out;
    }

    void ServeTop(const HitchState& state, double now)
    {
        double seconds = 0.0;
        {
            std::lock_guard lock(topMutex);
            if (topWanted == topDone) {
                return;
            }
            seconds = topSeconds;
        }
        TopData data;
        data.seconds = seconds;
        data.hz = watchHz.load();
        std::unordered_map<std::int32_t, std::pair<std::uint32_t, std::uint32_t>> stacks;  // on-CPU, waiting
        for (std::size_t i = 0; i < state.count; ++i) {
            const auto& entry = state.ring[(state.head + state.ring.size() - state.count + i) % state.ring.size()];
            if (entry.at < now - seconds || entry.stack < 0) {
                continue;
            }
            ++data.samples;
            auto& counts = stacks[entry.stack];
            if (entry.flags & flagWaiting) {
                ++data.waiting;
                ++counts.second;
            } else {
                ++counts.first;
            }
        }
        std::unordered_map<std::uint64_t, std::uint32_t> self, total, waits;
        std::unordered_set<std::uint64_t> seen;
        for (const auto& [stack, counts] : stacks) {
            const auto pcs = state.interner.Expand(stack);
            if (pcs.empty()) {
                continue;
            }
            if (counts.second) {
                waits[pcs.front()] += counts.second;
            }
            if (!counts.first) {
                continue;
            }
            self[pcs.front()] += counts.first;
            seen.clear();
            for (const auto pc : pcs) {
                if (seen.insert(pc).second) {
                    total[pc] += counts.first;
                }
            }
        }
        data.self = Largest(self, 80);
        data.total = Largest(total, 80);
        data.waits = Largest(waits, 20);
        {
            std::lock_guard lock(topMutex);
            topData = std::move(data);
            topDone = topWanted;
        }
        topReady.notify_all();
    }

    double SamplerCpu()
    {
        FILETIME created, exited, kernel, user;
        if (!GetThreadTimes(GetCurrentThread(), &created, &exited, &kernel, &user)) {
            return 0.0;
        }
        const auto value = [](const FILETIME& t) { return (static_cast<std::uint64_t>(t.dwHighDateTime) << 32) | t.dwLowDateTime; };
        return static_cast<double>(value(kernel) + value(user)) / 1e7;
    }

    // `weight` is the number of ticks the sample stands for (threads other than the main one are sampled
    // every few ticks).
    void Append(Session& session, Target& target, double at, std::uint32_t flags, std::uint32_t weight = 1)
    {
        ++target.samples;
        ++session.sampleCount;
        const double period = 1.0 / session.hz;
        const bool contiguous = target.lastAt >= 0.0 && at - target.lastAt < period * (weight + 0.5);
        target.lastAt = at;
        target.lastFlags = flags & flagWaiting;
        if (!target.runs.empty() && contiguous && (flags & flagReused)) {
            auto& last = target.runs.back();
            if (last.stack == target.lastStack && last.flags == flags) {
                last.count += weight;
                return;
            }
        }
        target.runs.push_back({ at, target.lastStack, weight, flags });
        if (++session.runCount >= maxSessionRuns) {
            session.truncated = true;
        }
    }

    HANDLE wake = CreateEventW(nullptr, FALSE, FALSE, nullptr);

    // One sampler thread for the plugin's lifetime; it idles on an event while nothing is sampled.
    void Run()
    {
        SetThreadDescription(GetCurrentThread(), L"devtools-profiler");
        SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
        const HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
        modules = SnapshotModules();
        double modulesAt = perf::Now();

        std::unique_ptr<Session> session;
        std::unordered_set<DWORD> known;
        double targetsAt = 0.0;
        Target mainTarget;
        bool haveMain = false;
        HitchState hitches;
        auto scratch = std::make_unique<Stack>();
        double next = perf::Now();

        for (;;) {
            {
                std::lock_guard lock(controlMutex);
                if (pending) {
                    session = std::move(pending);
                    session->started = perf::Now();
                    known.clear();
                    modules = SnapshotModules();
                    RefreshTargets(*session, known);
                    targetsAt = session->started;
                    session->samplerCpuSeconds = SamplerCpu();
                    next = session->started;
                    {
                        std::lock_guard marks(marksMutex);
                        sessionMarks.clear();
                    }
                    std::lock_guard frames(frameMutex);
                    sessionFrames.clear();
                }
            }
            const bool watch = Watching(perf::Now());
            if (!session && !watch) {
                WaitForSingleObject(wake, 250);
                next = perf::Now();
                continue;
            }
            const double now = perf::Now();
            if (session && (now >= session->deadline || !profiling.load() || session->truncated)) {
                session->stopped = now;
                session->samplerCpuSeconds = SamplerCpu() - session->samplerCpuSeconds;
                for (auto& t : session->threads) {
                    CloseHandle(t.handle);
                    t.handle = nullptr;
                }
                trace::Stop();
                std::lock_guard lock(controlMutex);
                finished = std::move(session);
                profiling.store(false);
                continue;
            }
            if (now - modulesAt > 1.0) {
                modules = SnapshotModules();
                modulesAt = now;
            }
            if (session && session->threadsSpec != "main" && now - targetsAt > 1.0) {
                RefreshTargets(*session, known);
                targetsAt = now;
            }

            // The main thread always feeds the hitch ring; a profile that includes it reuses this sample.
            if (!haveMain && mainThreadId) {
                haveMain = OpenTarget(mainThreadId, mainTarget);
            }
            std::int32_t mainSessionStack = -1;
            bool mainWalked = false;
            bool mainSampled = false;
            if (session && haveMain && session->mainSuspendedBase < 0.0) {
                session->mainSuspendedBase = mainTarget.suspendedSeconds;
                session->mainThrottledBase = mainTarget.throttled;
            }
            const double budget = session ? session->suspendBudget : 0.025;
            bool mainThrottled = false;
            if (haveMain && OverBudget(mainTarget, now, budget)) {
                mainThrottled = true;
                ++mainTarget.throttled;
            } else if (haveMain) {
                bool walked = false;
                if (SampleThread(mainTarget, *scratch, now, walked)) {
                    mainSampled = true;
                    if (walked) {
                        mainTarget.lastRingStack = hitches.interner.Add(*scratch);
                        mainWalked = true;
                        if (session) {
                            mainSessionStack = session->interner.Add(*scratch);
                        }
                    }
                    if (mainTarget.lastRingStack >= 0) {
                        hitches.Push(now, mainTarget.lastRingStack, walked ? 0 : (flagWaiting | flagReused));
                    }
                }
                const double frameStart = lastFrameEnd.load();
                if (mainSampled && !mainWalked && frameStart > 0.0 && (now - frameStart) * 1000.0 > hitchThresholdMs.load() &&
                    hitches.waitChainFrameStart != frameStart) {
                    hitches.waitChainFrameStart = frameStart;
                    hitches.waitChain = WaitChain(mainThreadId);
                }
                CollectHitches(hitches, now);
                ServeTop(hitches, now);
            }
            if (session) {
                ++session->ticks;
                const bool otherTick = session->ticks % session->otherEvery == 0;
                for (auto& target : session->threads) {
                    if (target.gone) {
                        continue;
                    }
                    bool walked = false;
                    if (target.main && haveMain && mainThrottled) {
                        target.throttled = mainTarget.throttled >= session->mainThrottledBase ? mainTarget.throttled - session->mainThrottledBase : 0;
                        if (target.lastStack >= 0) {
                            Append(*session, target, now, target.lastFlags | flagReused | flagThrottled);
                        }
                        continue;
                    }
                    if (!target.main && !otherTick) {
                        continue;
                    }
                    const std::uint32_t weight = target.main ? 1 : session->otherEvery;
                    if (!target.main && OverBudget(target, now, session->suspendBudget * session->otherEvery)) {
                        ++target.throttled;
                        if (target.lastStack >= 0) {
                            Append(*session, target, now, target.lastFlags | flagReused | flagThrottled, weight);
                        }
                        continue;
                    }
                    if (target.main && haveMain) {
                        if (!mainSampled) {
                            continue;
                        }
                        walked = mainWalked;
                        if (walked) {
                            target.lastStack = mainSessionStack;
                            ++target.walked;
                        }
                        target.suspendedSeconds = std::max(0.0, mainTarget.suspendedSeconds - std::max(0.0, session->mainSuspendedBase));
                    } else if (!SampleThread(target, *scratch, now, walked)) {
                        target.gone = true;
                        continue;
                    } else if (walked) {
                        target.lastStack = session->interner.Add(*scratch);
                        ++target.walked;
                    }
                    if (target.lastStack < 0) {
                        continue;
                    }
                    Append(*session, target, now, walked ? 0 : (flagWaiting | flagReused), weight);
                }
            }

            const double hz = session ? std::max(session->hz, watch ? watchHz.load() : 0.0) : watchHz.load();
            const double period = 1.0 / hz;
            next += period;
            const double after = perf::Now();
            if (after > next + 5 * period) {
                if (session) {
                    ++session->lateTicks;
                }
                next = after;
            }
            const double wait = next - after;
            if (wait > 0.0 && timer) {
                LARGE_INTEGER due;
                due.QuadPart = -static_cast<LONGLONG>(wait * 1e7);
                SetWaitableTimer(timer, &due, 0, nullptr, nullptr, FALSE);
                WaitForSingleObject(timer, 1000);
            }
        }
    }

    // Called with controlMutex held.
    void EnsureSampler()
    {
        static bool started = false;
        if (!started) {
            started = true;
            std::thread(Run).detach();
        }
        SetEvent(wake);
    }
    // ---- resolution --------------------------------------------------------------------------

    // DbgHelp is single-threaded; the per-call resolvers and the persistent one behind Profile.top share it.
    std::mutex dbgHelpMutex;

    struct Resolver
    {
        explicit Resolver(std::uintptr_t handle = 0x5d7e1e11) : symbols(reinterpret_cast<HANDLE>(handle)) {}
        Resolver(const Resolver&) = delete;
        Resolver& operator=(const Resolver&) = delete;

        std::vector<Module> list = SnapshotModules();
        std::uintptr_t gameBase = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
        std::vector<std::uint32_t> used;  // module indices in output order
        std::unordered_map<std::uint32_t, std::uint32_t> moduleOut;
        HANDLE symbols;
        bool symInit = false;
        std::unordered_set<std::uint32_t> symLoaded;
        std::unordered_map<std::uint64_t, Json> frames;

        ~Resolver()
        {
            if (symInit) {
                std::lock_guard lock(dbgHelpMutex);
                SymCleanup(symbols);
            }
        }

        bool Covers(std::uint64_t pc) const { return FindModule(list, pc) != nullptr; }

        static const std::vector<REL::IDDB::mapping_t>& Ids()
        {
            static const std::vector<REL::IDDB::mapping_t> table = [] {
                std::vector<REL::IDDB::mapping_t> out;
                try {
                    REL::Offset2ID ids;
                    out.assign(ids.begin(), ids.end());
                } catch (...) {
                }
                return out;
            }();
            return table;
        }

        std::string SymbolPath()
        {
            std::wstring path;
            std::unordered_set<std::wstring> dirs;
            for (const auto& m : list) {
                const auto slash = m.path.find_last_of(L"\\/");
                if (slash != std::wstring::npos && dirs.insert(m.path.substr(0, slash)).second) {
                    path += m.path.substr(0, slash) + L";";
                }
            }
            wchar_t env[2048]{};
            if (GetEnvironmentVariableW(L"_NT_SYMBOL_PATH", env, static_cast<DWORD>(std::size(env)))) {
                path += env;
            }
            return Narrow(path);
        }

        Json Frame(std::uint64_t pc)
        {
            if (const auto cached = frames.find(pc); cached != frames.end()) {
                return cached->second;
            }
            auto out = Resolve(pc);
            if (frames.size() > 65536) {
                frames.clear();
            }
            frames.emplace(pc, out);
            return out;
        }

        Json Resolve(std::uint64_t pc)
        {
            const auto* module = FindModule(list, pc);
            if (!module) {
                return Json{ { "addr", pc } };
            }
            const auto moduleIndex = static_cast<std::uint32_t>(module - list.data());
            const auto [it, added] = moduleOut.try_emplace(moduleIndex, static_cast<std::uint32_t>(used.size()));
            if (added) {
                used.push_back(moduleIndex);
            }
            const auto rva = static_cast<std::uint32_t>(pc - module->base);
            Json out{ { "m", it->second }, { "rva", rva } };
            const RUNTIME_FUNCTION* entry = TryFindEntry(*module, pc);
            if (entry) {
                out["start"] = FunctionStart(*module, entry);
            }
            if (module->base == gameBase) {
                const auto& ids = Ids();
                const std::uint64_t key = out.contains("start") ? out["start"].get<std::uint64_t>() : rva;
                auto found = std::upper_bound(ids.begin(), ids.end(), key, [](std::uint64_t k, const REL::IDDB::mapping_t& m) { return k < m.offset; });
                if (found != ids.begin()) {
                    --found;
                    out["id"] = found->id;
                    out["idOff"] = key - found->offset;
                }
                return out;
            }
            std::lock_guard lock(dbgHelpMutex);
            if (!symInit) {
                SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_FAIL_CRITICAL_ERRORS | SYMOPT_NO_PROMPTS);
                symInit = SymInitialize(symbols, SymbolPath().c_str(), FALSE) != FALSE;
            }
            if (symInit && symLoaded.insert(moduleIndex).second) {
                SymLoadModuleExW(symbols, nullptr, module->path.c_str(), nullptr, module->base,
                    static_cast<DWORD>(module->end - module->base), nullptr, 0);
            }
            if (symInit) {
                alignas(SYMBOL_INFO) char buffer[sizeof(SYMBOL_INFO) + 512]{};
                auto* info = reinterpret_cast<SYMBOL_INFO*>(buffer);
                info->SizeOfStruct = sizeof(SYMBOL_INFO);
                info->MaxNameLen = 511;
                DWORD64 displacement = 0;
                if (SymFromAddr(symbols, pc, &displacement, info)) {
                    out["sym"] = std::string(info->Name, info->NameLen);
                    out["symOff"] = displacement;
                }
            }
            return out;
        }

        Json Modules()
        {
            auto out = Json::array();
            for (const auto index : used) {
                const auto& m = list[index];
                const auto slash = m.path.find_last_of(L"\\/");
                Json entry{ { "name", Narrow(slash == std::wstring::npos ? m.path : m.path.substr(slash + 1)) },
                    { "path", Narrow(m.path) }, { "base", m.base }, { "size", m.end - m.base },
                    { "game", m.base == gameBase } };
                if (symInit) {
                    std::lock_guard lock(dbgHelpMutex);
                    IMAGEHLP_MODULEW64 info{};
                    info.SizeOfStruct = sizeof(info);
                    if (SymGetModuleInfoW64(symbols, m.base, &info)) {
                        static const char* kinds[] = { "none", "coff", "codeview", "pdb", "export", "deferred", "sym", "dia", "virtual" };
                        const auto kind = static_cast<std::size_t>(info.SymType);
                        entry["symbols"] = kind < std::size(kinds) ? kinds[kind] : "?";
                        if (info.LoadedPdbName[0]) {
                            entry["pdb"] = Narrow(info.LoadedPdbName);
                        }
                    }
                }
                out.push_back(std::move(entry));
            }
            return out;
        }
    };

    Json SerializeHitches(Resolver& resolver, double origin, std::size_t limit, std::uint64_t sinceFrame)
    {
        std::vector<Hitch> copy;
        {
            std::lock_guard lock(hitchMutex);
            for (auto it = hitchLog.rbegin(); it != hitchLog.rend() && copy.size() < limit; ++it) {
                if (it->frame > sinceFrame) {
                    copy.push_back(*it);
                }
            }
        }
        std::ranges::reverse(copy);
        std::unordered_map<std::uint64_t, std::uint32_t> frameIndex;
        auto frames = Json::array();
        auto list = Json::array();
        for (const auto& h : copy) {
            auto stacks = Json::array();
            for (const auto& stack : h.stacks) {
                auto ids = Json::array();
                for (const auto pc : stack) {
                    const auto [it, added] = frameIndex.try_emplace(pc, static_cast<std::uint32_t>(frames.size()));
                    if (added) {
                        frames.push_back(resolver.Frame(pc));
                    }
                    ids.push_back(it->second);
                }
                stacks.push_back(std::move(ids));
            }
            auto samples = Json::array();
            for (const auto& [at, stack, flags] : h.samples) {
                samples.push_back({ std::round((at - h.start) * 1e5) / 100.0, stack, flags });
            }
            list.push_back({ { "frame", h.frame }, { "ms", std::round((h.end - h.start) * 1e5) / 100.0 },
                { "startMs", std::round((h.start - origin) * 1e5) / 100.0 }, { "stacks", std::move(stacks) },
                { "samples", std::move(samples) }, { "waitChain", h.waitChain } });
        }
        return Json{ { "hitches", std::move(list) }, { "frames", std::move(frames) } };
    }

    std::string SerializeSession(Session& s)
    {
        Resolver resolver;
        auto frames = Json::array();
        for (const auto pc : s.interner.frames) {
            frames.push_back(resolver.Frame(pc));
        }
        auto stacks = Json::array();
        for (const auto& [prefix, frame] : s.interner.stacks) {
            stacks.push_back({ prefix, frame });
        }
        // Samples per thread as runs: [startMs, stack, count, flags]; a run of reused stacks stands for
        // `count` consecutive ticks, which collapses idle threads to a few entries.
        auto threads = Json::array();
        for (const auto& target : s.threads) {
            auto runs = Json::array();
            for (const auto& run : target.runs) {
                runs.push_back({ std::round((run.at - s.started) * 1e5) / 100.0, run.stack, run.count, run.flags });
            }
            Json thread{ { "tid", target.tid }, { "main", target.main },
                { "frameThread", target.tid == frameThreadId.load() }, { "samples", target.samples },
                { "walked", target.walked }, { "suspendedMs", std::round(target.suspendedSeconds * 1e5) / 100.0 },
                { "throttled", target.throttled }, { "runs", std::move(runs) } };
            devtools::threads::Describe(thread, target.tid, target.handle, target.start, 0, target.name);
            threads.push_back(std::move(thread));
        }
        auto marks = Json::array();
        {
            std::lock_guard lock(frameMutex);
            for (const auto& f : sessionFrames) {
                marks.push_back({ std::round((f.start - s.started) * 1e5) / 100.0, std::round((f.end - s.started) * 1e5) / 100.0, f.frame });
            }
        }
        auto markList = Json::array();
        {
            std::lock_guard lock(marksMutex);
            for (const auto& m : sessionMarks) {
                markList.push_back({ std::round((m.start - s.started) * 1e5) / 100.0, std::round((m.end - s.started) * 1e5) / 100.0, m.kind, m.name });
            }
        }
        Json hitchJson = SerializeHitches(resolver, s.started, maxHitches, 0);
        // keep only hitches that fall inside the profile
        auto kept = Json::array();
        for (auto& h : hitchJson["hitches"]) {
            if (h["startMs"].get<double>() >= 0.0) {
                kept.push_back(std::move(h));
            }
        }
        const double seconds = s.stopped - s.started;
        Json out{ { "format", "skyrim-devtools-profile" }, { "version", 1 }, { "hz", s.hz }, { "threadsSpec", s.threadsSpec },
            { "suspendBudgetPct", s.suspendBudget * 100.0 }, { "otherEvery", s.otherEvery },
            { "startEpochMs", static_cast<std::int64_t>(std::chrono::duration<double, std::milli>(
                                  std::chrono::system_clock::now().time_since_epoch()).count() - (perf::Now() - s.started) * 1000.0) },
            { "durationS", seconds }, { "ticks", s.ticks }, { "lateTicks", s.lateTicks }, { "truncated", s.truncated },
            { "samplerCpuMs", std::round(s.samplerCpuSeconds * 1e5) / 100.0 },
            { "mainThread", mainThreadId }, { "frameThread", frameThreadId.load() },
            { "pluginBuild", std::string(BuildId()) }, { "build", GetConfig().build },
            { "imageSha256", GetConfig().imageSha256 },
            { "frames", std::move(frames) }, { "stacks", std::move(stacks) }, { "threads", std::move(threads) },
            { "frameMarks", std::move(marks) }, { "marks", std::move(markList) }, { "hitchThresholdMs", hitchThresholdMs.load() },
            { "hitches", std::move(kept) }, { "hitchFrames", std::move(hitchJson["frames"]) },
            { "gpu", profiler::GpuFrames(s.started, s.stopped, s.started) }, { "counters", perf::CounterSeries(s.started - 0.3, s.stopped + 0.3, s.started) },
            { "gpuCounters", profiler::GpuCounters(s.started, s.stopped + 1.1, s.started) },
            { "gpuMetrics", profiler::GpuMetrics(s.started, s.stopped + 1.1, s.started) } };
        out["modules"] = resolver.Modules();
        if (s.traced) {
            out["trace"] = trace::Serialize(s.started);
        }
        return out.dump(-1, ' ', false, Json::error_handler_t::replace);
    }

    std::string Base64(const unsigned char* data, std::size_t size)
    {
        static constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        std::string out;
        out.reserve((size + 2) / 3 * 4);
        std::size_t i = 0;
        for (; i + 2 < size; i += 3) {
            const std::uint32_t v = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2];
            out += alphabet[v >> 18];
            out += alphabet[(v >> 12) & 63];
            out += alphabet[(v >> 6) & 63];
            out += alphabet[v & 63];
        }
        if (i < size) {
            const std::uint32_t v = (data[i] << 16) | (i + 1 < size ? data[i + 1] << 8 : 0);
            out += alphabet[v >> 18];
            out += alphabet[(v >> 12) & 63];
            out += i + 1 < size ? alphabet[(v >> 6) & 63] : '=';
            out += '=';
        }
        return out;
    }

    // MSZIP through the Windows Compression API: deflate blocks a Python client decodes with zlib alone.
    // A threads=all profile is tens of MB of JSON and the link to a remote client can be slow, so the
    // transfer, not the plugin, dominated reading one.
    // The project targets an SDK level below the one compressapi.h declares, so cabinet.dll is bound by hand.
    std::optional<std::string> Mszip(const std::string& text)
    {
        using Create = BOOL(WINAPI*)(DWORD, void*, HANDLE*);
        using Run = BOOL(WINAPI*)(HANDLE, const void*, SIZE_T, void*, SIZE_T, SIZE_T*);
        using Close = BOOL(WINAPI*)(HANDLE);
        constexpr DWORD algorithmMszip = 2;
        static const HMODULE cabinet = LoadLibraryW(L"cabinet.dll");
        const auto create = cabinet ? reinterpret_cast<Create>(GetProcAddress(cabinet, "CreateCompressor")) : nullptr;
        const auto compress = cabinet ? reinterpret_cast<Run>(GetProcAddress(cabinet, "Compress")) : nullptr;
        const auto close = cabinet ? reinterpret_cast<Close>(GetProcAddress(cabinet, "CloseCompressor")) : nullptr;
        HANDLE compressor = nullptr;
        if (!create || !compress || !close || !create(algorithmMszip, nullptr, &compressor)) {
            return std::nullopt;
        }
        SIZE_T size = 0;
        compress(compressor, text.data(), text.size(), nullptr, 0, &size);
        std::vector<unsigned char> buffer(size);
        const bool ok = size && compress(compressor, text.data(), text.size(), buffer.data(), buffer.size(), &size);
        close(compressor);
        if (!ok) {
            return std::nullopt;
        }
        return Base64(buffer.data(), size);
    }

    // Encodes the stopped profile for Profile.read; falls back to plain JSON when compression fails.
    Json PayloadHead(const std::string& encoding)
    {
        if (payload.empty() || payloadEncoding != encoding) {
            const double started = perf::Now();
            std::optional<std::string> packed;
            if (encoding == "mszip") {
                packed = Mszip(serialized);
            }
            payloadEncoding = packed ? encoding : "json";
            payload = packed ? std::move(*packed) : serialized;
            const double ms = (perf::Now() - started) * 1000.0;
            return Json{ { "bytes", serialized.size() }, { "encoding", payloadEncoding }, { "encodedBytes", payload.size() },
                { "chunks", (payload.size() + chunkBytes - 1) / chunkBytes }, { "serializeMs", std::round(serializeMs) },
                { "encodeMs", std::round(ms) } };
        }
        return Json{ { "bytes", serialized.size() }, { "encoding", payloadEncoding }, { "encodedBytes", payload.size() },
            { "chunks", (payload.size() + chunkBytes - 1) / chunkBytes }, { "serializeMs", std::round(serializeMs) } };
    }

    Json Status()
    {
        Json out{ { "profiling", profiling.load() }, { "watching", watching.load() }, { "watchHz", watchHz.load() },
            { "hitchMs", hitchThresholdMs.load() }, { "mainThread", mainThreadId }, { "frameThread", frameThreadId.load() } };
        std::lock_guard lock(hitchMutex);
        out["hitchesCaptured"] = hitchesSeen;
        return out;
    }
}

namespace devtools::profiler
{
    bool Profiling()
    {
        return profiling.load(std::memory_order_relaxed);
    }

    void Mark(std::string_view kind, std::string_view name, double start, double end)
    {
        if (!profiling.load(std::memory_order_relaxed)) {
            return;
        }
        std::lock_guard lock(marksMutex);
        if (sessionMarks.size() < maxMarks) {
            sessionMarks.push_back({ std::string(kind), std::string(name), start, end });
        }
    }

    Json TopSamples(double seconds)
    {
        seconds = std::clamp(seconds, 0.5, 30.0);
        const double now = perf::Now();
        const double previousLease = topLeaseUntil.exchange(now + 10.0);
        {
            std::lock_guard lock(controlMutex);
            EnsureSampler();
        }
        TopData data;
        bool fresh = false;
        {
            std::unique_lock lock(topMutex);
            topSeconds = seconds;
            const auto ticket = ++topWanted;
            fresh = topReady.wait_for(lock, std::chrono::milliseconds(1500), [&] { return topDone >= ticket; });
            if (fresh) {
                data = topData;
            }
        }
        Json out{ { "seconds", seconds }, { "hz", watchHz.load() }, { "warm", previousLease > now || watching.load() },
            { "ready", fresh } };
        if (!fresh) {
            return out;
        }
        // One resolver lives across calls so DbgHelp loads each module once; it is rebuilt when a frame lands
        // in a module that loaded after its snapshot.
        static std::mutex resolverMutex;
        static std::unique_ptr<Resolver> resolver;
        std::lock_guard lock(resolverMutex);
        const auto covers = [&](const auto& list) {
            return std::ranges::all_of(list, [&](const auto& item) { return resolver->Covers(item.first); });
        };
        // A rebuild reloads DbgHelp symbols for every module, which costs hundreds of milliseconds. Code outside
        // any module (hook trampolines, generated stubs) never becomes covered, so rebuild only when the loaded
        // module set really changed.
        const double resolveStart = perf::Now();
        if (!resolver || ((!covers(data.self) || !covers(data.total) || !covers(data.waits)) && ModuleSetChanged(resolver->list))) {
            resolver.reset();
            resolver = std::make_unique<Resolver>(0x5d7e1e12);
            out["resolverRebuilt"] = true;
        }
        const auto encode = [&](const auto& list) {
            auto rows = Json::array();
            for (const auto& [pc, count] : list) {
                auto frame = resolver->Frame(pc);
                frame["n"] = count;
                rows.push_back(std::move(frame));
            }
            return rows;
        };
        out["samples"] = data.samples;
        out["waiting"] = data.waiting;
        out["self"] = encode(data.self);
        out["total"] = encode(data.total);
        out["waits"] = encode(data.waits);
        out["modules"] = resolver->Modules();
        out["resolveMs"] = std::round((perf::Now() - resolveStart) * 10000.0) / 10.0;
        return out;
    }

    bool Sampling()
    {
        return profiling.load(std::memory_order_relaxed) || Watching(perf::Now());
    }

    void OnFrame(double start, double end, std::uint64_t frame)
    {
        const bool session = profiling.load(std::memory_order_relaxed);
        const bool watch = session || Watching(end);
        if (!watch) {
            return;
        }
        frameThreadId.store(GetCurrentThreadId(), std::memory_order_relaxed);
        lastFrameEnd.store(end, std::memory_order_relaxed);
        lastFrameNumber.store(frame, std::memory_order_relaxed);
        std::lock_guard lock(frameMutex);
        if (session && sessionFrames.size() < 200000) {
            sessionFrames.push_back({ start, end, frame });
        }
        if (watch && (end - start) * 1000.0 > hitchThresholdMs.load(std::memory_order_relaxed) && pendingHitches.size() < 256) {
            pendingHitches.push_back({ start, end, frame });
        }
    }
}

DEVTOOLS_MODULE(Profiler)
{
    mainThreadId = GetCurrentThreadId();
    SetCapability("profile", true);

    Register("Profile.start", [](const Json& params) {
        AllowOnly(params, { "threads", "hz", "durationS", "suspendBudgetPct", "otherHz", "trace" });
        const double hz = Optional<double>(params, "hz", 1000.0);
        const double duration = Optional<double>(params, "durationS", 30.0);
        const double budgetPct = Optional<double>(params, "suspendBudgetPct", 2.5);
        const double otherHz = Optional<double>(params, "otherHz", 250.0);
        if (budgetPct < 0.0 || budgetPct > 100.0) {
            throw RpcError(code::invalidParams, "suspendBudgetPct is 0 (no limit) to 100");
        }
        if (otherHz < 1.0) {
            throw RpcError(code::invalidParams, "otherHz is at least 1");
        }
        std::string threads = "main";
        if (params.contains("threads") && params["threads"].is_array()) {
            threads.clear();
            for (const auto& item : params["threads"]) {
                threads += (threads.empty() ? "" : ",") + (item.is_number() ? std::to_string(item.get<std::uint64_t>()) : item.get<std::string>());
            }
        } else {
            threads = Optional<std::string>(params, "threads", "main");
        }
        if (hz < 10.0 || hz > 4000.0) {
            throw RpcError(code::invalidParams, "hz is 10 to 4000");
        }
        if (duration <= 0.0 || duration > 600.0) {
            throw RpcError(code::invalidParams, "durationS is up to 600");
        }
        std::lock_guard lock(controlMutex);
        if (profiling.load() || pending) {
            throw RpcError(code::busy, "A profile is already running; call Profile.stop");
        }
        auto session = std::make_unique<Session>();
        Json traceInfo = nullptr;
        if (params.contains("trace") && params["trace"].is_object() && params["trace"].value("enabled", true)) {
            traceInfo = trace::Start(params["trace"]);
            session->traced = true;
        }
        session->hz = hz;
        session->suspendBudget = budgetPct / 100.0;
        session->otherEvery = static_cast<std::uint32_t>(std::max(1.0, std::round(hz / std::min(otherHz, hz))));
        session->threadsSpec = threads;
        session->deadline = perf::Now() + duration;
        finished.reset();
        serialized.clear();
        payload.clear();
        pending = std::move(session);
        profiling.store(true);
        EnsureSampler();
        return Json{ { "started", true }, { "hz", hz }, { "threads", threads }, { "durationS", duration },
            { "mainThread", mainThreadId }, { "trace", std::move(traceInfo) } };
    }, { .thread = Thread::Any });

    Register("Profile.stop", [](const Json& params) {
        AllowOnly(params, { "encoding" });
        const auto encoding = Optional<std::string>(params, "encoding", "json");
        if (encoding != "json" && encoding != "mszip") {
            throw RpcError(code::invalidParams, "encoding is json or mszip");
        }
        const double stopAsked = perf::Now();
        {
            std::lock_guard lock(controlMutex);
            if (!profiling.load() && !finished && !pending) {
                if (!serialized.empty()) {
                    auto head = PayloadHead(encoding);
                    head["again"] = true;
                    return head;
                }
                throw RpcError(code::notReady, "No profile is running");
            }
        }
        profiling.store(false);
        trace::Stop();
        for (int i = 0; i < 4000; ++i) {
            {
                std::lock_guard lock(controlMutex);
                if (finished) {
                    break;
                }
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        std::unique_ptr<Session> session;
        {
            std::lock_guard lock(controlMutex);
            session = std::move(finished);
        }
        if (!session) {
            throw RpcError(code::internal, "The sampler did not hand the profile back");
        }
        const double serializeStarted = perf::Now();
        const double waitMs = (serializeStarted - stopAsked) * 1000.0;
        std::string text = SerializeSession(*session);
        std::lock_guard lock(controlMutex);
        serialized = std::move(text);
        serializeMs = (perf::Now() - serializeStarted) * 1000.0;
        payload.clear();
        Json summary{ { "durationS", session->stopped - session->started }, { "samples", session->sampleCount }, { "runs", session->runCount },
            { "threads", session->threads.size() }, { "stacks", session->interner.stacks.size() },
            { "frames", session->interner.frames.size() }, { "ticks", session->ticks }, { "lateTicks", session->lateTicks },
            { "samplerCpuMs", std::round(session->samplerCpuSeconds * 1e5) / 100.0 }, { "truncated", session->truncated } };
        auto head = PayloadHead(encoding);
        head["waitMs"] = std::round(waitMs);
        head["summary"] = std::move(summary);
        return head;
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(120000) });

    Register("Profile.read", [](const Json& params) {
        AllowOnly(params, { "chunk" });
        const auto chunk = Require<std::size_t>(params, "chunk");
        std::lock_guard lock(controlMutex);
        if (payload.empty()) {
            throw RpcError(code::notReady, "No stopped profile to read");
        }
        const std::size_t offset = chunk * chunkBytes;
        if (offset >= payload.size()) {
            throw RpcError(code::invalidParams, "chunk is past the end");
        }
        return Json{ { "chunk", chunk }, { "encoding", payloadEncoding }, { "data", payload.substr(offset, chunkBytes) },
            { "last", offset + chunkBytes >= payload.size() } };
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(30000) });

    Register("Profile.status", [](const Json& params) {
        AllowOnly(params, {});
        return Status();
    }, { .thread = Thread::Any });

    Register("Profile.watch", [](const Json& params) {
        AllowOnly(params, { "enabled", "hz", "hitchMs" });
        if (params.contains("hz")) {
            watchHz.store(std::clamp(Require<double>(params, "hz"), 10.0, 2000.0));
        }
        if (params.contains("hitchMs")) {
            hitchThresholdMs.store(std::clamp(Require<double>(params, "hitchMs"), 10.0, 60000.0));
        }
        if (params.contains("enabled")) {
            const bool enabled = Require<bool>(params, "enabled");
            std::lock_guard lock(controlMutex);
            watching.store(enabled);
            if (enabled) {
                EnsureSampler();
            }
        }
        return Status();
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(10000) });

    Register("Profile.hitches", [](const Json& params) {
        AllowOnly(params, { "limit", "sinceFrame", "clear" });
        const auto limit = Optional<std::size_t>(params, "limit", 10);
        const auto since = Optional<std::uint64_t>(params, "sinceFrame", 0);
        Resolver resolver;
        auto out = SerializeHitches(resolver, perf::Now(), limit, since);
        out["modules"] = resolver.Modules();
        out["status"] = Status();
        if (Optional<bool>(params, "clear", false)) {
            std::lock_guard lock(hitchMutex);
            hitchLog.clear();
        }
        return out;
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(60000) });
}
