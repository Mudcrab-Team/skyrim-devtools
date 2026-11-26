// SPDX-License-Identifier: GPL-3.0-or-later
// Trace core: per-thread event buffers, the name table, Trace start/stop/serialize and the engine function
// hooks. Each thread appends to its own buffer without locks; buffers are chunked and never freed, so a
// reader only needs the published count. A new session bumps a generation and every thread drops its old
// events the next time it writes, which keeps Start free of cross-thread writes.
//
// Engine hooks route through trace_thunk.asm: a per-hook stub loads the hook index and jumps to one shared
// thunk that has unwind info, so the sampler's stack walks continue through hooked functions into the game.
#include "trace.h"

#include "logs.h"
#include "methods.h"
#include "perf.h"
#include "thread_names.h"

#include <Windows.h>
#include <MinHook.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstring>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace devtools::trace
{
    constexpr int maxHooks = 128;
}

extern "C" {
    // Filled by MinHook; the thunk calls through them.
    void* devtools_trace_originals[devtools::trace::maxHooks]{};
    extern unsigned char devtools_trace_stubs[];
    std::int64_t devtools_trace_begin(std::uint32_t index);
    void devtools_trace_end(std::uint32_t index, std::int64_t start);
}

namespace devtools::trace
{
    namespace
    {
        struct Event
        {
            std::int64_t start;
            std::uint32_t duration;
            std::uint32_t name;  // 20 bits name id, 12 bits flags
            std::uint64_t a;
            std::uint64_t b;
        };
        static_assert(sizeof(Event) == 32);

        constexpr std::size_t chunkEvents = 4096;
        constexpr std::size_t maxChunks = 1024;
        constexpr std::uint32_t nameBits = 20;
        constexpr std::size_t stubSize = 16;

        struct Chunk
        {
            Event events[chunkEvents];
        };

        struct ThreadBuffer
        {
            DWORD tid = 0;
            // Taken on the thread itself when it first records, so a thread that exits before the trace
            // is read keeps its label.
            std::uintptr_t start = 0;
            std::uintptr_t root = 0;
            devtools::threads::Name name;
            std::uint64_t generation = 0;
            std::atomic<std::uint64_t> count = 0;
            std::atomic<std::uint64_t> dropped = 0;
            std::atomic<std::uint64_t> shortWaits = 0;
            std::array<std::atomic<Chunk*>, maxChunks> chunks{};
        };

        std::atomic_bool active = false;
        std::atomic<std::uint64_t> generation = 1;
        std::atomic<std::uint64_t> total = 0;
        std::atomic<std::uint64_t> capDropped = 0;
        Settings settings;
        std::int64_t frequency = [] {
            LARGE_INTEGER f{};
            QueryPerformanceFrequency(&f);
            return f.QuadPart;
        }();

        std::mutex buffersMutex;
        std::vector<ThreadBuffer*> buffers;
        thread_local ThreadBuffer* local = nullptr;

        std::shared_mutex namesMutex;
        std::vector<std::pair<std::string, std::string>> names;
        std::unordered_map<std::string, std::uint32_t> nameIndex;

        // Serialization reads buffers after Stop; Start waits so a new session never resets them mid-read.
        std::mutex sessionMutex;

        struct Hook
        {
            void* target = nullptr;
            std::uint32_t name = 0;
            std::string label;
            std::string category;
            std::int64_t minTicks = 0;
            std::atomic_bool on = false;
            std::atomic<std::uint64_t> calls = 0;
            std::atomic<std::uint64_t> filtered = 0;
        };
        std::array<Hook, maxHooks> hooks;
        int hookCount = 0;
        Json hookReport = Json::array();

        ThreadBuffer& Local()
        {
            if (!local) {
                auto* buffer = new ThreadBuffer;
                buffer->tid = GetCurrentThreadId();
                buffer->start = devtools::threads::StartAddress(GetCurrentThread());
                buffer->root = devtools::threads::RootGameFrame();
                buffer->name = devtools::threads::NameOf(buffer->tid, GetCurrentThread());
                std::lock_guard lock(buffersMutex);
                buffers.push_back(buffer);
                local = buffer;
            }
            return *local;
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

        // The executable's code range; engine hooks must land inside it.
        bool InExecutableText(std::uintptr_t address)
        {
            const auto base = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
            const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
            const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
            const auto* section = IMAGE_FIRST_SECTION(nt);
            for (WORD i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++section) {
                if ((section->Characteristics & IMAGE_SCN_MEM_EXECUTE) == 0) {
                    continue;
                }
                const auto begin = base + section->VirtualAddress;
                if (address >= begin && address < begin + section->Misc.VirtualSize) {
                    return true;
                }
            }
            return false;
        }

        // A hook only goes on a function's first byte: the unwind table must start a function exactly there.
        // That rejects stale RVAs from another build before MinHook patches the middle of some function.
        std::string CheckTarget(std::uintptr_t address)
        {
            if (!InExecutableText(address)) {
                return "outside the executable's code";
            }
            DWORD64 imageBase = 0;
            const auto* entry = RtlLookupFunctionEntry(address, &imageBase, nullptr);
            if (!entry) {
                return "no unwind entry";
            }
            if (imageBase + entry->BeginAddress != address) {
                return "not a function start";
            }
            return {};
        }

        std::uintptr_t ResolveTarget(const Json& spec, std::string& error)
        {
            if (spec.contains("id")) {
                try {
                    return REL::ID(spec["id"].get<std::uint64_t>()).address();
                } catch (const std::exception& e) {
                    error = std::string("Address Library id not found: ") + e.what();
                    return 0;
                }
            }
            if (spec.contains("rva")) {
                const auto& rva = spec["rva"];
                std::uint64_t value = 0;
                if (rva.is_string()) {
                    value = std::stoull(rva.get<std::string>(), nullptr, 0);
                } else {
                    value = rva.get<std::uint64_t>();
                }
                return reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr)) + value;
            }
            error = "needs id or rva";
            return 0;
        }

        bool StubLooksRight(int index)
        {
            const unsigned char* stub = devtools_trace_stubs + index * stubSize;
            std::uint32_t value = 0;
            std::memcpy(&value, stub + 2, sizeof(value));
            return stub[0] == 0x41 && stub[1] == 0xBA && value == static_cast<std::uint32_t>(index);
        }
    }

    bool Active()
    {
        return active.load(std::memory_order_relaxed);
    }

    std::int64_t Ticks()
    {
        LARGE_INTEGER now{};
        QueryPerformanceCounter(&now);
        return now.QuadPart;
    }

    const Settings& Current()
    {
        return settings;
    }

    std::uint32_t Name(std::string_view name, std::string_view category)
    {
        std::string key(name);
        key += '\x1f';
        key += category;
        {
            std::shared_lock lock(namesMutex);
            if (const auto it = nameIndex.find(key); it != nameIndex.end()) {
                return it->second;
            }
        }
        std::unique_lock lock(namesMutex);
        const auto [it, added] = nameIndex.try_emplace(key, static_cast<std::uint32_t>(names.size()));
        if (added) {
            names.emplace_back(std::string(name), std::string(category));
        }
        return it->second;
    }

    void Complete(std::uint32_t name, std::int64_t start, std::int64_t end, std::uint64_t a, std::uint64_t b, std::uint32_t flags)
    {
        if (!Active()) {
            return;
        }
        auto& buffer = Local();
        const auto gen = generation.load(std::memory_order_acquire);
        if (buffer.generation != gen) {
            buffer.generation = gen;
            buffer.count.store(0, std::memory_order_release);
            buffer.dropped.store(0, std::memory_order_relaxed);
            buffer.shortWaits.store(0, std::memory_order_relaxed);
        }
        const auto index = buffer.count.load(std::memory_order_relaxed);
        if (index >= chunkEvents * maxChunks || total.fetch_add(1, std::memory_order_relaxed) >= settings.maxEvents) {
            buffer.dropped.fetch_add(1, std::memory_order_relaxed);
            return;
        }
        auto* chunk = buffer.chunks[index / chunkEvents].load(std::memory_order_relaxed);
        if (!chunk) {
            chunk = new (std::nothrow) Chunk;
            if (!chunk) {
                buffer.dropped.fetch_add(1, std::memory_order_relaxed);
                return;
            }
            buffer.chunks[index / chunkEvents].store(chunk, std::memory_order_release);
        }
        const auto span = end > start ? end - start : 0;
        chunk->events[index % chunkEvents] = Event{ start, static_cast<std::uint32_t>(std::min<std::int64_t>(span, 0xFFFFFFFF)),
            (name & ((1u << nameBits) - 1)) | (flags << nameBits), a, b };
        buffer.count.store(index + 1, std::memory_order_release);
    }

    void CountShortWait()
    {
        if (!Active()) {
            return;
        }
        auto& buffer = Local();
        if (buffer.generation != generation.load(std::memory_order_acquire)) {
            return;  // the first recorded event resets the buffer; a short wait alone is not worth it
        }
        buffer.shortWaits.fetch_add(1, std::memory_order_relaxed);
    }

    Json InstallEngineHooks(const Json& list)
    {
        for (int i = 0; i < hookCount; ++i) {
            hooks[i].on.store(false);
        }
        auto report = Json::array();
        bool queued = false;
        for (const auto& spec : list) {
            const auto label = spec.value("label", std::string());
            const auto category = spec.value("category", std::string("engine"));
            Json entry{ { "label", label } };
            std::string error;
            const auto target = ResolveTarget(spec, error);
            if (target && error.empty()) {
                error = CheckTarget(target);
            }
            if (!error.empty()) {
                entry["error"] = error;
                report.push_back(std::move(entry));
                continue;
            }
            entry["rva"] = target - reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
            int index = -1;
            for (int i = 0; i < hookCount; ++i) {
                if (hooks[i].target == reinterpret_cast<void*>(target)) {
                    index = i;
                }
            }
            if (index < 0) {
                if (hookCount >= maxHooks) {
                    entry["error"] = "hook table full";
                    report.push_back(std::move(entry));
                    continue;
                }
                index = hookCount;
                if (!StubLooksRight(index)) {
                    entry["error"] = "stub table mismatch";
                    report.push_back(std::move(entry));
                    continue;
                }
                const auto status = MH_CreateHook(reinterpret_cast<void*>(target), devtools_trace_stubs + index * stubSize,
                    &devtools_trace_originals[index]);
                if (status != MH_OK) {
                    entry["error"] = std::string("MinHook: ") + MH_StatusToString(status);
                    report.push_back(std::move(entry));
                    continue;
                }
                MH_QueueEnableHook(reinterpret_cast<void*>(target));
                queued = true;
                hooks[index].target = reinterpret_cast<void*>(target);
                ++hookCount;
            }
            auto& hook = hooks[index];
            hook.label = label;
            hook.category = category;
            hook.name = Name(label, category);
            hook.minTicks = static_cast<std::int64_t>(spec.value("minUs", 0.0) * frequency / 1e6);
            hook.calls.store(0);
            hook.filtered.store(0);
            hook.on.store(true);
            entry["hook"] = index;
            report.push_back(std::move(entry));
        }
        if (queued) {
            const auto status = MH_ApplyQueued();
            if (status != MH_OK) {
                logs::Plugin("warn", std::string("trace: enabling engine hooks failed: ") + MH_StatusToString(status));
                for (auto& entry : report) {
                    if (entry.contains("hook")) {
                        entry["error"] = std::string("MinHook apply: ") + MH_StatusToString(status);
                    }
                }
            }
        }
        hookReport = report;
        return report;
    }

    Json EngineHookStatus()
    {
        auto out = hookReport;
        for (auto& entry : out) {
            if (entry.contains("hook")) {
                const auto& hook = hooks[entry["hook"].get<int>()];
                entry["calls"] = hook.calls.load();
                entry["filtered"] = hook.filtered.load();
            }
        }
        return out;
    }

    Json Start(const Json& config)
    {
        std::lock_guard lock(sessionMutex);
        active.store(false);
        const auto api = config.value("api", std::string("batched"));
        if (api != "batched" && api != "full" && api != "off") {
            throw RpcError(code::invalidParams, "trace.api is batched, full or off");
        }
        settings.apiMode = api == "off" ? 0 : api == "full" ? 2 : 1;
        settings.os = config.value("os", true);
        settings.waitMinTicks = static_cast<std::int64_t>(config.value("waitMinUs", 20.0) * frequency / 1e6);
        settings.maxEvents = config.value("maxEvents", static_cast<std::uint64_t>(2'000'000));
        if (settings.maxEvents > chunkEvents * maxChunks * 4) {
            throw RpcError(code::invalidParams, "trace.maxEvents is too large");
        }
        Json out{ { "api", api }, { "os", settings.os } };
        out["hooks"] = InstallEngineHooks(config.value("hooks", Json::array()));
        if (settings.os) {
            out["osHooks"] = InstallOsHooks();
        }
        ResetApiSession();
        total.store(0);
        capDropped.store(0);
        generation.fetch_add(1, std::memory_order_acq_rel);
        active.store(true);
        return out;
    }

    void Stop()
    {
        active.store(false);
    }

    Json Serialize(double origin)
    {
        std::lock_guard lock(sessionMutex);
        const auto gen = generation.load();
        Json namesJson = Json::array();
        {
            std::shared_lock names_lock(namesMutex);
            for (const auto& [name, category] : names) {
                namesJson.push_back({ name, category });
            }
        }
        auto threads = Json::array();
        std::vector<ThreadBuffer*> list;
        {
            std::lock_guard buffers_lock(buffersMutex);
            list = buffers;
        }
        std::uint64_t events = 0;
        std::vector<unsigned char> bytes;
        for (auto* buffer : list) {
            if (buffer->generation != gen) {
                continue;
            }
            const auto count = buffer->count.load(std::memory_order_acquire);
            bytes.resize(count * sizeof(Event));
            for (std::uint64_t i = 0; i < count; i += chunkEvents) {
                const auto* chunk = buffer->chunks[i / chunkEvents].load(std::memory_order_acquire);
                const auto n = std::min<std::uint64_t>(chunkEvents, count - i);
                std::memcpy(bytes.data() + i * sizeof(Event), chunk->events, n * sizeof(Event));
            }
            events += count;
            Json thread{ { "tid", buffer->tid }, { "count", count }, { "dropped", buffer->dropped.load() },
                { "shortWaits", buffer->shortWaits.load() }, { "events", Base64(bytes.data(), bytes.size()) } };
            devtools::threads::Describe(thread, buffer->tid, nullptr, buffer->start, buffer->root, buffer->name);
            threads.push_back(std::move(thread));
        }
        const char* api = settings.apiMode == 0 ? "off" : settings.apiMode == 2 ? "full" : "batched";
        return Json{ { "version", 1 }, { "freq", frequency },
            { "originTicks", static_cast<std::int64_t>(origin * static_cast<double>(frequency)) },
            { "eventLayout", "int64 start, uint32 duration, uint32 name|flags<<20, uint64 a, uint64 b" },
            { "settings", { { "api", api }, { "os", settings.os }, { "waitMinUs", settings.waitMinTicks * 1e6 / frequency },
                              { "maxEvents", settings.maxEvents } } },
            { "names", std::move(namesJson) }, { "threads", std::move(threads) }, { "events", events },
            { "hooks", EngineHookStatus() }, { "api", ApiStatus() }, { "osHooks", OsStatus() } };
    }
}

extern "C" std::int64_t devtools_trace_begin(std::uint32_t index)
{
    using namespace devtools::trace;
    if (!Active() || index >= maxHooks || !hooks[index].on.load(std::memory_order_relaxed)) {
        return 0;
    }
    const auto now = Ticks();
    return now ? now : 1;
}

extern "C" void devtools_trace_end(std::uint32_t index, std::int64_t start)
{
    using namespace devtools::trace;
    if (!start) {
        return;
    }
    const auto end = Ticks();
    auto& hook = hooks[index];
    hook.calls.fetch_add(1, std::memory_order_relaxed);
    if (end - start < hook.minTicks) {
        hook.filtered.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    Complete(hook.name, start, end);
}

DEVTOOLS_MODULE(Trace)
{
    devtools::trace::InstallFileMapHooks();
    devtools::SetCapability("profileTrace", true);
}
