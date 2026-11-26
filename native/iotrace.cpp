// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// I/O and load tracing: every file open, read, seek, mapping and DLL load in the game process, with file,
// bytes, latency, thread and call stack, plus memory, page-fault and per-thread CPU counters and load-phase
// marks. The client turns a dump into SQLite tables, a Perfetto timeline and a report
// (skyrim_devtools/profile_io.py).
//
// Opt-in with SKYRIM_DEVTOOLS_IOTRACE=1 (or a list such as `stack=12,events=3000000`). The hooks are inline
// detours on the kernelbase exports, installed while this DLL is being loaded, so they see the calls of every
// module, including SKSE plugins loaded after this one and the data-file loading that follows. Mod Organizer's
// virtual file system patches some of the same functions first; MinHook copies its jump into the trampoline,
// so its redirection still runs below ours and the paths seen here are the ones the caller asked for, while
// the physical file is read back from the handle.
#include "events.h"
#include "game.h"
#include "methods.h"

#include <MinHook.h>
#include <Windows.h>
#include <DbgHelp.h>
#include <Psapi.h>
#include <TlHelp32.h>
#include <winternl.h>

#include <array>
#include <atomic>
#include <cstdio>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#pragma comment(lib, "dbghelp.lib")

namespace
{
    using namespace devtools;

    enum Op : std::uint8_t
    {
        opOpen = 1,
        opRead = 2,         // synchronous ReadFile; `result` is bytes read
        opReadIssue = 3,    // overlapped ReadFile or ReadFileEx; completes later
        opReadWait = 4,     // GetOverlappedResult(Ex); `extra` is the OVERLAPPED, latency is from its issue
        opSeek = 5,
        opMapping = 6,      // CreateFileMapping; `offset` is the maximum size
        opMapView = 7,      // MapViewOfFile(Ex); `offset` is the file offset, `size` the view size
        opClose = 8,
        opNtRead = 9,       // NtReadFile not reached through ReadFile
        opLoadLibrary = 10, // `file` is the DLL path
        opPluginLoad = 11,  // SKSEPlugin_Load of the DLL in `file`
    };

    enum Flag : std::uint8_t
    {
        flagFailed = 1,
        flagOverlapped = 2,  // handle opened with FILE_FLAG_OVERLAPPED, or the read passed an OVERLAPPED
        flagNoBuffering = 4,
        flagSequential = 8,
        flagRandom = 16,
        flagWrite = 32,
        flagPending = 64,     // the read returned ERROR_IO_PENDING
        flagNested = 128,     // made from inside another traced call (A to W forwarding, for example)
    };

    // 48 bytes, written once; `op` is stored last with release order so a reader skips half-written slots.
    struct Event
    {
        std::int64_t start;
        std::uint32_t ticks;
        std::uint32_t file;
        std::uint64_t offset;
        std::uint32_t size;
        std::uint32_t result;
        std::uint32_t stack;
        std::uint16_t thread;
        std::uint8_t flags;
        std::atomic<std::uint8_t> op;
        std::uint64_t extra;
    };
    static_assert(sizeof(Event) == 48);

    struct Settings
    {
        bool armed = false;
        int stackDepth = 10;
        std::uint64_t maxEvents = 3'000'000;
        int counterMs = 50;
        int threadMs = 250;
    } settings;

    std::atomic_bool recording = false;
    std::int64_t qpcFrequency = 1;
    std::int64_t armedAt = 0;
    double armedEpochMs = 0;  // wall clock at armedAt, to line events up with the sampling profiler's startEpochMs

    double EpochMs()
    {
        FILETIME ft;
        GetSystemTimeAsFileTime(&ft);
        const auto t = static_cast<std::uint64_t>(ft.dwHighDateTime) << 32 | ft.dwLowDateTime;
        return static_cast<double>(t - 116444736000000000ULL) / 1e4;
    }

    std::int64_t Now()
    {
        LARGE_INTEGER t;
        QueryPerformanceCounter(&t);
        return t.QuadPart;
    }

    // ---- event storage ------------------------------------------------------------------------

    constexpr std::size_t chunkEvents = 1 << 15;
    std::vector<std::atomic<Event*>> chunks;
    std::mutex chunkMutex;
    std::atomic<std::uint64_t> nextEvent = 0;
    std::atomic<std::uint64_t> dropped = 0;
    std::atomic<std::int64_t> hookTicks = 0;  // bookkeeping time spent in the hooks, excluding the real call

    Event* Slot()
    {
        const auto index = nextEvent.fetch_add(1, std::memory_order_relaxed);
        if (index >= settings.maxEvents) {
            dropped.fetch_add(1, std::memory_order_relaxed);
            return nullptr;
        }
        auto& chunk = chunks[index / chunkEvents];
        auto* base = chunk.load(std::memory_order_acquire);
        if (!base) {
            std::lock_guard lock(chunkMutex);
            base = chunk.load(std::memory_order_acquire);
            if (!base) {
                base = static_cast<Event*>(VirtualAlloc(nullptr, chunkEvents * sizeof(Event), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE));
                if (!base) {
                    dropped.fetch_add(1, std::memory_order_relaxed);
                    return nullptr;
                }
                chunk.store(base, std::memory_order_release);
            }
        }
        return base + index % chunkEvents;
    }

    // ---- interning ----------------------------------------------------------------------------

    std::shared_mutex fileMutex;
    std::unordered_map<std::wstring, std::uint32_t> fileIds;
    std::vector<std::wstring> fileNames{ L"" };   // id 0: unknown
    std::vector<std::wstring> filePhysical{ L"" };
    std::unordered_map<HANDLE, std::uint32_t> handleFiles;
    std::unordered_map<HANDLE, std::uint32_t> mappingFiles;

    std::uint32_t InternFile(std::wstring_view path)
    {
        std::wstring key(path);
        {
            std::shared_lock lock(fileMutex);
            if (const auto it = fileIds.find(key); it != fileIds.end()) {
                return it->second;
            }
        }
        std::unique_lock lock(fileMutex);
        const auto [it, inserted] = fileIds.emplace(std::move(key), static_cast<std::uint32_t>(fileNames.size()));
        if (inserted) {
            fileNames.push_back(it->first);
            filePhysical.emplace_back();
        }
        return it->second;
    }

    std::uint32_t FileOf(HANDLE handle)
    {
        std::shared_lock lock(fileMutex);
        const auto it = handleFiles.find(handle);
        return it == handleFiles.end() ? 0 : it->second;
    }

    std::mutex threadMutex;
    std::unordered_map<DWORD, std::uint16_t> threadIds;
    std::vector<DWORD> threadList{ 0 };
    thread_local std::uint16_t threadIndex = 0;
    thread_local int depth = 0;  // >0 while inside a traced call on this thread
    thread_local bool quiet = false;  // this module's own threads

    std::uint16_t ThreadIndex()
    {
        if (!threadIndex) {
            const auto id = GetCurrentThreadId();
            std::lock_guard lock(threadMutex);
            const auto [it, inserted] = threadIds.emplace(id, static_cast<std::uint16_t>(threadList.size()));
            if (inserted) {
                threadList.push_back(id);
            }
            threadIndex = it->second;
        }
        return threadIndex;
    }

    std::mutex stackMutex;
    std::unordered_map<std::uint64_t, std::uint32_t> stackIds;
    std::vector<std::vector<std::uintptr_t>> stackFrames{ {} };

    std::uint32_t CaptureStack()
    {
        if (settings.stackDepth <= 0) {
            return 0;
        }
        void* frames[48];
        ULONG hash = 0;
        // Nothing is skipped here because inlining moves the hook frames; the client drops this module's frames.
        const auto count = RtlCaptureStackBackTrace(0, static_cast<DWORD>(std::min(settings.stackDepth + 6, 48)), frames, &hash);
        if (!count) {
            return 0;
        }
        const std::uint64_t key = (static_cast<std::uint64_t>(hash) << 8) ^ count ^ (reinterpret_cast<std::uintptr_t>(frames[0]) << 20);
        std::lock_guard lock(stackMutex);
        const auto [it, inserted] = stackIds.emplace(key, static_cast<std::uint32_t>(stackFrames.size()));
        if (inserted) {
            stackFrames.emplace_back(reinterpret_cast<std::uintptr_t*>(frames), reinterpret_cast<std::uintptr_t*>(frames) + count);
        }
        return it->second;
    }

    void Record(Op op, std::int64_t start, std::int64_t end, std::uint32_t file, std::uint64_t offset, std::uint32_t size,
        std::uint32_t result, std::uint8_t flags, std::uint64_t extra, bool stack = true)
    {
        auto* e = Slot();
        if (!e) {
            return;
        }
        e->start = start;
        e->ticks = static_cast<std::uint32_t>(std::min<std::int64_t>(end - start, UINT32_MAX));
        e->file = file;
        e->offset = offset;
        e->size = size;
        e->result = result;
        e->stack = stack ? CaptureStack() : 0;
        e->thread = ThreadIndex();
        e->flags = static_cast<std::uint8_t>(flags | (depth > 1 ? flagNested : 0));
        e->extra = extra;
        e->op.store(op, std::memory_order_release);
    }

    // Counts this hook's own work so the dump can report the overhead.
    struct Scope
    {
        bool on;
        std::int64_t mark = 0;
        Scope() : on(recording.load(std::memory_order_relaxed) && !quiet) { ++depth; }
        ~Scope() { --depth; }
        void Begin() { mark = Now(); }
        void End() { hookTicks.fetch_add(Now() - mark, std::memory_order_relaxed); }
    };

    // ---- hooks -------------------------------------------------------------------------------

    template <class Fn>
    struct Hook
    {
        Fn original = nullptr;
    };

    Hook<decltype(&CreateFileW)> createFileW;
    Hook<decltype(&CreateFileA)> createFileA;
    // Declared by hand: the SDK hides CreateFile2 and GetOverlappedResultEx below the Windows 8 target version.
    struct CreateFile2Params
    {
        DWORD size, attributes, flags;
    };
    using CreateFile2Fn = HANDLE(WINAPI*)(LPCWSTR, DWORD, DWORD, DWORD, const CreateFile2Params*);
    using GetOverlappedResultExFn = BOOL(WINAPI*)(HANDLE, LPOVERLAPPED, LPDWORD, DWORD, BOOL);
    Hook<CreateFile2Fn> createFile2;
    Hook<decltype(&ReadFile)> readFile;
    Hook<decltype(&ReadFileEx)> readFileEx;
    Hook<decltype(&GetOverlappedResult)> getOverlappedResult;
    Hook<GetOverlappedResultExFn> getOverlappedResultEx;
    Hook<decltype(&SetFilePointerEx)> setFilePointerEx;
    Hook<decltype(&SetFilePointer)> setFilePointer;
    Hook<decltype(&CreateFileMappingW)> createFileMappingW;
    Hook<decltype(&CreateFileMappingA)> createFileMappingA;
    Hook<decltype(&MapViewOfFile)> mapViewOfFile;
    Hook<decltype(&MapViewOfFileEx)> mapViewOfFileEx;
    Hook<decltype(&CloseHandle)> closeHandle;
    Hook<decltype(&LoadLibraryExW)> loadLibraryExW;
    Hook<decltype(&LoadLibraryExA)> loadLibraryExA;
    Hook<decltype(&GetProcAddress)> getProcAddress;
    using NtReadFileFn = NTSTATUS(NTAPI*)(HANDLE, HANDLE, PIO_APC_ROUTINE, PVOID, PIO_STATUS_BLOCK, PVOID, ULONG, PLARGE_INTEGER, PULONG);
    Hook<NtReadFileFn> ntReadFile;

    // Overlapped reads in flight: OVERLAPPED -> (issue time, file), for the issue-to-completion latency.
    std::mutex pendingMutex;
    std::unordered_map<const void*, std::pair<std::int64_t, std::uint32_t>> pending;

    std::uint8_t OpenFlags(DWORD access, DWORD flags)
    {
        std::uint8_t f = 0;
        f |= (flags & FILE_FLAG_OVERLAPPED) ? flagOverlapped : 0;
        f |= (flags & FILE_FLAG_NO_BUFFERING) ? flagNoBuffering : 0;
        f |= (flags & FILE_FLAG_SEQUENTIAL_SCAN) ? flagSequential : 0;
        f |= (flags & FILE_FLAG_RANDOM_ACCESS) ? flagRandom : 0;
        f |= (access & (GENERIC_WRITE | FILE_WRITE_DATA | FILE_APPEND_DATA)) ? flagWrite : 0;
        return f;
    }

    void AfterOpen(HANDLE handle, std::wstring_view path, std::int64_t start, std::int64_t end, DWORD access, DWORD disposition,
        DWORD flags)
    {
        const bool ok = handle != INVALID_HANDLE_VALUE && handle;
        const auto file = InternFile(path);
        if (ok) {
            const auto type = GetFileType(handle);
            if (type != FILE_TYPE_DISK) {
                return;
            }
            bool needPhysical = false;
            {
                std::unique_lock lock(fileMutex);
                handleFiles[handle] = file;
                needPhysical = filePhysical[file].empty();
            }
            if (needPhysical) {
                wchar_t buffer[1024];
                const auto n = GetFinalPathNameByHandleW(handle, buffer, static_cast<DWORD>(std::size(buffer)), FILE_NAME_NORMALIZED);
                if (n && n < std::size(buffer)) {
                    std::unique_lock lock(fileMutex);
                    filePhysical[file].assign(buffer, n);
                }
            }
        }
        LARGE_INTEGER size{};
        if (ok) {
            GetFileSizeEx(handle, &size);
        }
        Record(opOpen, start, end, file, static_cast<std::uint64_t>(size.QuadPart), access, disposition,
            static_cast<std::uint8_t>(OpenFlags(access, flags) | (ok ? 0 : flagFailed)), reinterpret_cast<std::uintptr_t>(handle));
    }

    HANDLE WINAPI HookedCreateFileW(LPCWSTR path, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES sa, DWORD disposition, DWORD flags,
        HANDLE templ)
    {
        Scope scope;
        const auto start = Now();
        const auto handle = createFileW.original(path, access, share, sa, disposition, flags, templ);
        if (scope.on && path) {
            const auto end = Now();
            scope.Begin();
            AfterOpen(handle, path, start, end, access, disposition, flags);
            scope.End();
        }
        return handle;
    }

    HANDLE WINAPI HookedCreateFileA(LPCSTR path, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES sa, DWORD disposition, DWORD flags,
        HANDLE templ)
    {
        Scope scope;
        const auto start = Now();
        const auto handle = createFileA.original(path, access, share, sa, disposition, flags, templ);
        if (scope.on && path) {
            const auto end = Now();
            scope.Begin();
            wchar_t wide[1024];
            const auto n = MultiByteToWideChar(CP_ACP, 0, path, -1, wide, static_cast<int>(std::size(wide)));
            AfterOpen(handle, n > 0 ? std::wstring_view(wide, n - 1) : std::wstring_view(L"?"), start, end, access, disposition, flags);
            scope.End();
        }
        return handle;
    }

    HANDLE WINAPI HookedCreateFile2(LPCWSTR path, DWORD access, DWORD share, DWORD disposition, const CreateFile2Params* ext)
    {
        Scope scope;
        const auto start = Now();
        const auto handle = createFile2.original(path, access, share, disposition, ext);
        if (scope.on && path) {
            const auto end = Now();
            scope.Begin();
            AfterOpen(handle, path, start, end, access, disposition, ext ? ext->flags : 0);
            scope.End();
        }
        return handle;
    }

    std::uint64_t Position(HANDLE file)
    {
        LARGE_INTEGER zero{}, pos{};
        return setFilePointerEx.original(file, zero, &pos, FILE_CURRENT) ? static_cast<std::uint64_t>(pos.QuadPart) : UINT64_MAX;
    }

    BOOL WINAPI HookedReadFile(HANDLE file, LPVOID buffer, DWORD size, LPDWORD read, LPOVERLAPPED overlapped)
    {
        Scope scope;
        if (!scope.on) {
            return readFile.original(file, buffer, size, read, overlapped);
        }
        const auto id = FileOf(file);
        if (!id) {
            return readFile.original(file, buffer, size, read, overlapped);  // pipes, consoles, handles opened before arming
        }
        std::uint64_t offset = UINT64_MAX;
        if (overlapped) {
            offset = (static_cast<std::uint64_t>(overlapped->OffsetHigh) << 32) | overlapped->Offset;
        } else {
            offset = Position(file);
        }
        const auto start = Now();
        const auto ok = readFile.original(file, buffer, size, read, overlapped);
        const auto end = Now();
        const auto error = ok ? 0 : GetLastError();
        scope.Begin();
        if (overlapped && (ok || error == ERROR_IO_PENDING)) {
            const bool inFlight = !ok;
            if (inFlight) {
                std::lock_guard lock(pendingMutex);
                pending[overlapped] = { start, id };
            }
            Record(opReadIssue, start, end, id, offset, size, ok && read ? *read : 0,
                static_cast<std::uint8_t>(flagOverlapped | (inFlight ? flagPending : 0)), reinterpret_cast<std::uintptr_t>(overlapped));
        } else {
            Record(opRead, start, end, id, offset, size, ok && read ? *read : 0, static_cast<std::uint8_t>(ok ? 0 : flagFailed),
                reinterpret_cast<std::uintptr_t>(file));
        }
        scope.End();
        SetLastError(error);
        return ok;
    }

    BOOL WINAPI HookedReadFileEx(HANDLE file, LPVOID buffer, DWORD size, LPOVERLAPPED overlapped, LPOVERLAPPED_COMPLETION_ROUTINE routine)
    {
        Scope scope;
        const auto start = Now();
        const auto ok = readFileEx.original(file, buffer, size, overlapped, routine);
        if (scope.on) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            const auto offset = overlapped ? (static_cast<std::uint64_t>(overlapped->OffsetHigh) << 32) | overlapped->Offset : 0;
            Record(opReadIssue, start, end, FileOf(file), offset, size, 0, static_cast<std::uint8_t>(flagOverlapped | (ok ? 0 : flagFailed)),
                reinterpret_cast<std::uintptr_t>(overlapped));
            scope.End();
            SetLastError(error);
        }
        return ok;
    }

    void AfterWait(LPOVERLAPPED overlapped, std::int64_t start, std::int64_t end, BOOL ok, LPDWORD bytes)
    {
        std::pair<std::int64_t, std::uint32_t> issued{ 0, 0 };
        if (ok) {
            std::lock_guard lock(pendingMutex);
            if (const auto it = pending.find(overlapped); it != pending.end()) {
                issued = it->second;
                pending.erase(it);
            }
        }
        // `offset` carries the issue time so the client can compute the full latency.
        Record(opReadWait, start, end, issued.second, static_cast<std::uint64_t>(issued.first), 0, ok && bytes ? *bytes : 0,
            static_cast<std::uint8_t>(ok ? 0 : flagFailed), reinterpret_cast<std::uintptr_t>(overlapped));
    }

    BOOL WINAPI HookedGetOverlappedResult(HANDLE file, LPOVERLAPPED overlapped, LPDWORD bytes, BOOL wait)
    {
        Scope scope;
        const auto start = Now();
        const auto ok = getOverlappedResult.original(file, overlapped, bytes, wait);
        if (scope.on && overlapped) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            AfterWait(overlapped, start, end, ok, bytes);
            scope.End();
            SetLastError(error);
        }
        return ok;
    }

    BOOL WINAPI HookedGetOverlappedResultEx(HANDLE file, LPOVERLAPPED overlapped, LPDWORD bytes, DWORD ms, BOOL alertable)
    {
        Scope scope;
        const auto start = Now();
        const auto ok = getOverlappedResultEx.original(file, overlapped, bytes, ms, alertable);
        if (scope.on && overlapped && depth == 1) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            AfterWait(overlapped, start, end, ok, bytes);
            scope.End();
            SetLastError(error);
        }
        return ok;
    }

    BOOL WINAPI HookedSetFilePointerEx(HANDLE file, LARGE_INTEGER distance, PLARGE_INTEGER newPos, DWORD method)
    {
        Scope scope;
        LARGE_INTEGER local{};
        const auto ok = setFilePointerEx.original(file, distance, newPos ? newPos : &local, method);
        // A query (0 from the current position) moves nothing and is not a seek.
        if (scope.on && ok && depth == 1 && !(method == FILE_CURRENT && distance.QuadPart == 0)) {
            scope.Begin();
            if (const auto id = FileOf(file)) {
                const auto now = Now();
                Record(opSeek, now, now, id, static_cast<std::uint64_t>((newPos ? newPos : &local)->QuadPart), 0, method, 0,
                    reinterpret_cast<std::uintptr_t>(file), false);
            }
            scope.End();
        }
        return ok;
    }

    DWORD WINAPI HookedSetFilePointer(HANDLE file, LONG low, PLONG high, DWORD method)
    {
        Scope scope;
        const auto result = setFilePointer.original(file, low, high, method);
        if (scope.on && result != INVALID_SET_FILE_POINTER && depth == 1 && !(method == FILE_CURRENT && low == 0 && (!high || !*high))) {
            const auto error = GetLastError();
            scope.Begin();
            if (const auto id = FileOf(file)) {
                const auto now = Now();
                const std::uint64_t pos = (high ? static_cast<std::uint64_t>(static_cast<DWORD>(*high)) << 32 : 0) | result;
                Record(opSeek, now, now, id, pos, 0, method, 0, reinterpret_cast<std::uintptr_t>(file), false);
            }
            scope.End();
            SetLastError(error);
        }
        return result;
    }

    void AfterMapping(HANDLE file, HANDLE mapping, std::int64_t start, std::int64_t end, DWORD high, DWORD low)
    {
        const auto id = (file && file != INVALID_HANDLE_VALUE) ? FileOf(file) : 0;
        if (!id) {
            return;  // pagefile-backed sections are shared memory, not file I/O
        }
        if (mapping) {
            std::unique_lock lock(fileMutex);
            mappingFiles[mapping] = id;
        }
        Record(opMapping, start, end, id, (static_cast<std::uint64_t>(high) << 32) | low, 0, 0, mapping ? 0 : flagFailed,
            reinterpret_cast<std::uintptr_t>(mapping));
    }

    HANDLE WINAPI HookedCreateFileMappingW(HANDLE file, LPSECURITY_ATTRIBUTES sa, DWORD protect, DWORD high, DWORD low, LPCWSTR name)
    {
        Scope scope;
        const auto start = Now();
        const auto mapping = createFileMappingW.original(file, sa, protect, high, low, name);
        if (scope.on && depth == 1) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            AfterMapping(file, mapping, start, end, high, low);
            scope.End();
            SetLastError(error);
        }
        return mapping;
    }

    HANDLE WINAPI HookedCreateFileMappingA(HANDLE file, LPSECURITY_ATTRIBUTES sa, DWORD protect, DWORD high, DWORD low, LPCSTR name)
    {
        Scope scope;
        const auto start = Now();
        const auto mapping = createFileMappingA.original(file, sa, protect, high, low, name);
        if (scope.on && depth == 1) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            AfterMapping(file, mapping, start, end, high, low);
            scope.End();
            SetLastError(error);
        }
        return mapping;
    }

    void AfterView(HANDLE mapping, LPVOID view, std::int64_t start, std::int64_t end, DWORD high, DWORD low, SIZE_T bytes)
    {
        std::uint32_t id = 0;
        {
            std::shared_lock lock(fileMutex);
            if (const auto it = mappingFiles.find(mapping); it != mappingFiles.end()) {
                id = it->second;
            }
        }
        if (!id) {
            return;
        }
        Record(opMapView, start, end, id, (static_cast<std::uint64_t>(high) << 32) | low, static_cast<std::uint32_t>(std::min<SIZE_T>(bytes, UINT32_MAX)),
            0, view ? 0 : flagFailed, reinterpret_cast<std::uintptr_t>(view));
    }

    LPVOID WINAPI HookedMapViewOfFile(HANDLE mapping, DWORD access, DWORD high, DWORD low, SIZE_T bytes)
    {
        Scope scope;
        const auto start = Now();
        const auto view = mapViewOfFile.original(mapping, access, high, low, bytes);
        if (scope.on && depth == 1) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            AfterView(mapping, view, start, end, high, low, bytes);
            scope.End();
            SetLastError(error);
        }
        return view;
    }

    LPVOID WINAPI HookedMapViewOfFileEx(HANDLE mapping, DWORD access, DWORD high, DWORD low, SIZE_T bytes, LPVOID address)
    {
        Scope scope;
        const auto start = Now();
        const auto view = mapViewOfFileEx.original(mapping, access, high, low, bytes, address);
        if (scope.on && depth == 1) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            AfterView(mapping, view, start, end, high, low, bytes);
            scope.End();
            SetLastError(error);
        }
        return view;
    }

    BOOL WINAPI HookedCloseHandle(HANDLE handle)
    {
        if (!recording.load(std::memory_order_relaxed) || quiet) {
            return closeHandle.original(handle);
        }
        std::uint32_t id = 0;
        {
            std::unique_lock lock(fileMutex);
            if (const auto it = handleFiles.find(handle); it != handleFiles.end()) {
                id = it->second;
                handleFiles.erase(it);
            } else {
                mappingFiles.erase(handle);
            }
        }
        const auto ok = closeHandle.original(handle);
        if (id) {
            const auto error = GetLastError();
            const auto now = Now();
            Record(opClose, now, now, id, 0, 0, 0, 0, reinterpret_cast<std::uintptr_t>(handle), false);
            SetLastError(error);
        }
        return ok;
    }

    HMODULE WINAPI HookedLoadLibraryExW(LPCWSTR name, HANDLE file, DWORD flags)
    {
        Scope scope;
        const auto start = Now();
        const auto module = loadLibraryExW.original(name, file, flags);
        if (scope.on && name && depth == 1) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            Record(opLoadLibrary, start, end, InternFile(name), 0, 0, flags, module ? 0 : flagFailed, reinterpret_cast<std::uintptr_t>(module));
            scope.End();
            SetLastError(error);
        }
        return module;
    }

    HMODULE WINAPI HookedLoadLibraryExA(LPCSTR name, HANDLE file, DWORD flags)
    {
        Scope scope;
        const auto start = Now();
        const auto module = loadLibraryExA.original(name, file, flags);
        if (scope.on && name && depth == 1) {
            const auto error = GetLastError();
            const auto end = Now();
            scope.Begin();
            wchar_t wide[1024];
            const auto n = MultiByteToWideChar(CP_ACP, 0, name, -1, wide, static_cast<int>(std::size(wide)));
            Record(opLoadLibrary, start, end, InternFile(n > 0 ? std::wstring_view(wide, n - 1) : std::wstring_view(L"?")), 0, 0, flags,
                module ? 0 : flagFailed, reinterpret_cast<std::uintptr_t>(module));
            scope.End();
            SetLastError(error);
        }
        return module;
    }

    // SKSEPlugin_Load is timed through a wrapper handed out in place of the export: SKSE resolves each plugin's
    // entry with GetProcAddress, and one template instance per slot remembers which plugin it stands for.
    using PluginLoadFn = bool (*)(const void*);
    constexpr std::size_t pluginSlots = 512;
    std::array<PluginLoadFn, pluginSlots> pluginTargets{};
    std::array<std::uint32_t, pluginSlots> pluginFiles{};
    std::atomic<std::size_t> pluginCount = 0;

    template <std::size_t I>
    bool PluginLoadWrapper(const void* skse)
    {
        const auto start = Now();
        const auto ok = pluginTargets[I](skse);
        Record(opPluginLoad, start, Now(), pluginFiles[I], 0, 0, ok ? 1 : 0, 0, 0, false);
        return ok;
    }

    template <std::size_t... I>
    constexpr std::array<PluginLoadFn, sizeof...(I)> MakeWrappers(std::index_sequence<I...>)
    {
        return { &PluginLoadWrapper<I>... };
    }
    constexpr auto pluginWrappers = MakeWrappers(std::make_index_sequence<pluginSlots>{});

    FARPROC WINAPI HookedGetProcAddress(HMODULE module, LPCSTR name)
    {
        const auto proc = getProcAddress.original(module, name);
        if (!proc || !recording.load(std::memory_order_relaxed) || reinterpret_cast<std::uintptr_t>(name) <= 0xFFFF ||
            std::strcmp(name, "SKSEPlugin_Load") != 0) {
            return proc;
        }
        const auto error = GetLastError();
        static std::mutex mutex;
        std::lock_guard lock(mutex);
        for (std::size_t i = 0; i < pluginCount; ++i) {
            if (pluginTargets[i] == reinterpret_cast<PluginLoadFn>(proc)) {
                return reinterpret_cast<FARPROC>(pluginWrappers[i]);
            }
        }
        const auto slot = pluginCount.load();
        if (slot >= pluginSlots) {
            return proc;
        }
        wchar_t path[MAX_PATH * 2]{};
        GetModuleFileNameW(module, path, static_cast<DWORD>(std::size(path)));
        pluginFiles[slot] = InternFile(path);
        pluginTargets[slot] = reinterpret_cast<PluginLoadFn>(proc);
        pluginCount = slot + 1;
        SetLastError(error);
        return reinterpret_cast<FARPROC>(pluginWrappers[slot]);
    }

    NTSTATUS NTAPI HookedNtReadFile(HANDLE file, HANDLE event, PIO_APC_ROUTINE apc, PVOID context, PIO_STATUS_BLOCK io, PVOID buffer,
        ULONG size, PLARGE_INTEGER offset, PULONG key)
    {
        // Reads that came through ReadFile are already recorded there.
        if (depth > 0 || quiet || !recording.load(std::memory_order_relaxed)) {
            return ntReadFile.original(file, event, apc, context, io, buffer, size, offset, key);
        }
        Scope scope;
        const auto start = Now();
        const auto status = ntReadFile.original(file, event, apc, context, io, buffer, size, offset, key);
        const auto end = Now();
        scope.Begin();
        if (const auto id = FileOf(file)) {
            const bool inFlight = status == 0x103;  // STATUS_PENDING
            Record(opNtRead, start, end, id, offset ? static_cast<std::uint64_t>(offset->QuadPart) : UINT64_MAX, size,
                (!inFlight && io && status >= 0) ? static_cast<std::uint32_t>(io->Information) : 0,
                static_cast<std::uint8_t>((inFlight ? flagPending : 0) | (status < 0 ? flagFailed : 0)), reinterpret_cast<std::uintptr_t>(file));
        }
        scope.End();
        return status;
    }

    Json hookStatus = Json::object();

    template <class Fn>
    void Install(const char* dll, const char* name, Fn detour, Hook<Fn>& hook)
    {
        auto* target = reinterpret_cast<void*>(GetProcAddress(GetModuleHandleA(dll), name));
        if (!target) {
            hookStatus[name] = "missing";
            return;
        }
        auto status = MH_CreateHook(target, reinterpret_cast<void*>(detour), reinterpret_cast<void**>(&hook.original));
        if (status == MH_OK) {
            status = MH_EnableHook(target);
        }
        hookStatus[name] = status == MH_OK ? Json("ok") : Json(MH_StatusToString(status));
    }

    // ---- counters ----------------------------------------------------------------------------

    struct CounterSample
    {
        std::int64_t at;
        std::uint64_t pageFaults, workingSet, privateBytes, availPhys, readOps, readBytes, otherOps;
        // System-wide memory manager counts (SYSTEM_PERFORMANCE_INFORMATION): hard-fault page reads and the
        // I/Os they took, transition (soft, from standby) faults, demand-zero faults.
        std::uint32_t sysPageReads, sysPageReadIos, sysTransition, sysDemandZero, sysCacheTransition, sysFaults;
    };

    struct ThreadSample
    {
        std::int64_t at;
        DWORD tid;
        std::uint64_t cycles;
        std::uint64_t cpu100ns;
    };

    std::mutex counterMutex;
    std::vector<CounterSample> counters;
    std::vector<ThreadSample> threadSamples;
    std::unordered_map<DWORD, std::wstring> threadNames;
    std::atomic_bool samplerStop = false;
    std::thread sampler;

    using NtQuerySystemInformationFn = NTSTATUS(NTAPI*)(ULONG, PVOID, ULONG, PULONG);

    CounterSample TakeCounters(NtQuerySystemInformationFn query)
    {
        CounterSample s{};
        s.at = Now();
        PROCESS_MEMORY_COUNTERS_EX pmc{};
        pmc.cb = sizeof(pmc);
        if (GetProcessMemoryInfo(GetCurrentProcess(), reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&pmc), sizeof(pmc))) {
            s.pageFaults = pmc.PageFaultCount;
            s.workingSet = pmc.WorkingSetSize;
            s.privateBytes = pmc.PrivateUsage;
        }
        MEMORYSTATUSEX mem{ sizeof(mem) };
        if (GlobalMemoryStatusEx(&mem)) {
            s.availPhys = mem.ullAvailPhys;
        }
        IO_COUNTERS io{};
        if (GetProcessIoCounters(GetCurrentProcess(), &io)) {
            s.readOps = io.ReadOperationCount;
            s.readBytes = io.ReadTransferCount;
            s.otherOps = io.OtherOperationCount;
        }
        if (query) {
            alignas(8) std::uint8_t buffer[1024]{};
            if (query(2 /* SystemPerformanceInformation */, buffer, sizeof(buffer), nullptr) >= 0) {
                const auto at = [&](std::size_t offset) { return *reinterpret_cast<const std::uint32_t*>(buffer + offset); };
                s.sysFaults = at(60);
                s.sysTransition = at(68);
                s.sysCacheTransition = at(72);
                s.sysDemandZero = at(76);
                s.sysPageReads = at(80);
                s.sysPageReadIos = at(84);
            }
        }
        return s;
    }

    void SampleThreads()
    {
        const auto snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if (snapshot == INVALID_HANDLE_VALUE) {
            return;
        }
        const auto pid = GetCurrentProcessId();
        const auto now = Now();
        THREADENTRY32 entry{ sizeof(entry) };
        std::vector<ThreadSample> batch;
        for (auto more = Thread32First(snapshot, &entry); more; more = Thread32Next(snapshot, &entry)) {
            if (entry.th32OwnerProcessID != pid) {
                continue;
            }
            const auto thread = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, FALSE, entry.th32ThreadID);
            if (!thread) {
                continue;
            }
            ThreadSample t{ now, entry.th32ThreadID, 0, 0 };
            ULONG64 cycles = 0;
            QueryThreadCycleTime(thread, &cycles);
            t.cycles = cycles;
            FILETIME c, e, k, u;
            if (GetThreadTimes(thread, &c, &e, &k, &u)) {
                t.cpu100ns = (static_cast<std::uint64_t>(k.dwHighDateTime) << 32 | k.dwLowDateTime) +
                             (static_cast<std::uint64_t>(u.dwHighDateTime) << 32 | u.dwLowDateTime);
            }
            bool named = false;
            {
                std::lock_guard lock(counterMutex);
                named = threadNames.contains(entry.th32ThreadID);
            }
            if (!named) {
                using GetDescriptionFn = HRESULT(WINAPI*)(HANDLE, PWSTR*);
                static const auto getDescription = reinterpret_cast<GetDescriptionFn>(
                    GetProcAddress(GetModuleHandleW(L"kernelbase.dll"), "GetThreadDescription"));
                PWSTR description = nullptr;
                std::wstring name;
                if (getDescription && SUCCEEDED(getDescription(thread, &description)) && description) {
                    name = description;
                    LocalFree(description);
                }
                std::lock_guard lock(counterMutex);
                threadNames[entry.th32ThreadID] = name;
            }
            CloseHandle(thread);
            batch.push_back(t);
        }
        CloseHandle(snapshot);
        std::lock_guard lock(counterMutex);
        threadSamples.insert(threadSamples.end(), batch.begin(), batch.end());
    }

    void SamplerLoop()
    {
        quiet = true;
        using SetDescriptionFn = HRESULT(WINAPI*)(HANDLE, PCWSTR);
        if (const auto set = reinterpret_cast<SetDescriptionFn>(GetProcAddress(GetModuleHandleW(L"kernelbase.dll"), "SetThreadDescription"))) {
            set(GetCurrentThread(), L"devtools iotrace");
        }
        const auto query = reinterpret_cast<NtQuerySystemInformationFn>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQuerySystemInformation"));
        int tick = 0;
        const int threadEvery = std::max(1, settings.threadMs / std::max(1, settings.counterMs));
        while (!samplerStop.load()) {
            const auto sample = TakeCounters(query);
            {
                std::lock_guard lock(counterMutex);
                counters.push_back(sample);
            }
            if (tick++ % threadEvery == 0) {
                SampleThreads();
            }
            Sleep(static_cast<DWORD>(settings.counterMs));
        }
    }

    // ---- marks -------------------------------------------------------------------------------

    std::mutex markMutex;
    std::vector<std::pair<std::int64_t, std::string>> marks;

    void Mark(std::string name)
    {
        if (!settings.armed) {
            return;
        }
        const auto now = Now();
        std::lock_guard lock(markMutex);
        marks.emplace_back(now, std::move(name));
    }

    class MenuSink : public RE::BSTEventSink<RE::MenuOpenCloseEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::MenuOpenCloseEvent* event, RE::BSTEventSource<RE::MenuOpenCloseEvent>*) override
        {
            if (event) {
                const std::string_view menu(event->menuName.c_str());
                if (menu == "Loading Menu" || menu == "Main Menu" || menu == "Fader Menu" || menu == "Mist Menu") {
                    Mark(std::string(menu) + (event->opening ? " open" : " close"));
                }
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };

    const char* SkseMessageName(std::uint32_t type)
    {
        switch (type) {
        case SKSE::MessagingInterface::kPostLoad: return "skse PostLoad";
        case SKSE::MessagingInterface::kPostPostLoad: return "skse PostPostLoad";
        case SKSE::MessagingInterface::kInputLoaded: return "skse InputLoaded";
        case SKSE::MessagingInterface::kDataLoaded: return "skse DataLoaded";
        case SKSE::MessagingInterface::kNewGame: return "skse NewGame";
        case SKSE::MessagingInterface::kPreLoadGame: return "skse PreLoadGame";
        case SKSE::MessagingInterface::kPostLoadGame: return "skse PostLoadGame";
        case SKSE::MessagingInterface::kSaveGame: return "skse SaveGame";
        default: return nullptr;
        }
    }

    // ---- arming ------------------------------------------------------------------------------

    std::string Env(const char* name)
    {
        char buffer[256]{};
        const auto n = GetEnvironmentVariableA(name, buffer, static_cast<DWORD>(std::size(buffer)));
        return n && n < std::size(buffer) ? std::string(buffer, n) : std::string{};
    }

    void ParseSettings(const std::string& text)
    {
        std::size_t at = 0;
        while (at <= text.size()) {
            const auto comma = std::min(text.find(',', at), text.size());
            const auto item = text.substr(at, comma - at);
            const auto eq = item.find('=');
            if (eq != std::string::npos) {
                const auto key = item.substr(0, eq);
                const auto value = std::strtoll(item.c_str() + eq + 1, nullptr, 10);
                if (key == "stack") {
                    settings.stackDepth = static_cast<int>(std::clamp<long long>(value, 0, 32));
                } else if (key == "events") {
                    settings.maxEvents = static_cast<std::uint64_t>(std::clamp<long long>(value, 1000, 50'000'000));
                } else if (key == "counterMs") {
                    settings.counterMs = static_cast<int>(std::clamp<long long>(value, 5, 5000));
                } else if (key == "threadMs") {
                    settings.threadMs = static_cast<int>(std::clamp<long long>(value, 10, 10000));
                }
            }
            at = comma + 1;
        }
    }

    void Arm()
    {
        const auto text = Env("SKYRIM_DEVTOOLS_IOTRACE");
        if (text.empty() || text == "0" || Env("SKYRIM_DEVTOOLS_SESSION").empty()) {
            return;
        }
        ParseSettings(text);
        LARGE_INTEGER f;
        QueryPerformanceFrequency(&f);
        qpcFrequency = f.QuadPart;
        chunks = std::vector<std::atomic<Event*>>((settings.maxEvents + chunkEvents - 1) / chunkEvents);
        fileIds.reserve(1 << 16);
        handleFiles.reserve(1 << 12);
        const auto init = MH_Initialize();
        if (init != MH_OK && init != MH_ERROR_ALREADY_INITIALIZED) {
            hookStatus["MH_Initialize"] = MH_StatusToString(init);
            return;
        }
        const char* kb = "kernelbase.dll";
        Install(kb, "CreateFileW", &HookedCreateFileW, createFileW);
        Install(kb, "CreateFileA", &HookedCreateFileA, createFileA);
        Install(kb, "CreateFile2", &HookedCreateFile2, createFile2);
        Install(kb, "ReadFile", &HookedReadFile, readFile);
        Install(kb, "ReadFileEx", &HookedReadFileEx, readFileEx);
        Install(kb, "GetOverlappedResult", &HookedGetOverlappedResult, getOverlappedResult);
        Install(kb, "GetOverlappedResultEx", &HookedGetOverlappedResultEx, getOverlappedResultEx);
        Install(kb, "SetFilePointerEx", &HookedSetFilePointerEx, setFilePointerEx);
        Install(kb, "SetFilePointer", &HookedSetFilePointer, setFilePointer);
        Install(kb, "CreateFileMappingW", &HookedCreateFileMappingW, createFileMappingW);
        Install(kb, "CreateFileMappingA", &HookedCreateFileMappingA, createFileMappingA);
        Install(kb, "MapViewOfFile", &HookedMapViewOfFile, mapViewOfFile);
        Install(kb, "MapViewOfFileEx", &HookedMapViewOfFileEx, mapViewOfFileEx);
        Install(kb, "CloseHandle", &HookedCloseHandle, closeHandle);
        Install(kb, "LoadLibraryExW", &HookedLoadLibraryExW, loadLibraryExW);
        Install(kb, "LoadLibraryExA", &HookedLoadLibraryExA, loadLibraryExA);
        Install(kb, "GetProcAddress", &HookedGetProcAddress, getProcAddress);
        Install("ntdll.dll", "NtReadFile", &HookedNtReadFile, ntReadFile);
        if (!setFilePointerEx.original) {
            setFilePointerEx.original = &SetFilePointerEx;
        }
        settings.armed = true;
        armedAt = Now();
        armedEpochMs = EpochMs();
        recording = true;
        Mark("iotrace armed");
        sampler = std::thread(SamplerLoop);
    }

    // Runs while the loader maps this DLL, before SKSE calls into it, so later plugins' loads are seen.
    const bool armedAtLoad = [] {
        Arm();
        return true;
    }();

    // ---- dump --------------------------------------------------------------------------------

    std::string Narrow(std::wstring_view w)
    {
        if (w.empty()) {
            return {};
        }
        const auto n = WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
        std::string out(static_cast<std::size_t>(n), '\0');
        WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), out.data(), n, nullptr, nullptr);
        return out;
    }

    struct ModuleInfo
    {
        std::uintptr_t base, end;
        std::string path;
    };

    std::vector<ModuleInfo> Modules()
    {
        std::vector<HMODULE> handles(2048);
        DWORD needed = 0;
        std::vector<ModuleInfo> out;
        if (!EnumProcessModulesEx(GetCurrentProcess(), handles.data(), static_cast<DWORD>(handles.size() * sizeof(HMODULE)), &needed,
                LIST_MODULES_64BIT)) {
            return out;
        }
        handles.resize(std::min<std::size_t>(handles.size(), needed / sizeof(HMODULE)));
        for (const auto h : handles) {
            MODULEINFO info{};
            if (!GetModuleInformation(GetCurrentProcess(), h, &info, sizeof(info))) {
                continue;
            }
            wchar_t path[MAX_PATH * 2]{};
            GetModuleFileNameW(h, path, static_cast<DWORD>(std::size(path)));
            const auto base = reinterpret_cast<std::uintptr_t>(info.lpBaseOfDll);
            out.push_back({ base, base + info.SizeOfImage, Narrow(path) });
        }
        std::ranges::sort(out, {}, &ModuleInfo::base);
        return out;
    }

    // Frames as module index + RVA; game frames also get the nearest Address Library ID at or below them and
    // other modules a DbgHelp name where their PDB or exports know one.
    Json ResolveFrames(const std::vector<std::uintptr_t>& addresses, const std::vector<ModuleInfo>& modules)
    {
        const auto game = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
        std::vector<REL::IDDB::mapping_t> ids;
        try {
            REL::Offset2ID table;
            ids.assign(table.begin(), table.end());
        } catch (...) {
        }
        const auto process = reinterpret_cast<HANDLE>(static_cast<std::uintptr_t>(0x10a7ace));
        std::string searchPath;
        for (const auto& m : modules) {
            const auto slash = m.path.find_last_of("\\/");
            if (slash != std::string::npos) {
                searchPath += m.path.substr(0, slash) + ";";
            }
        }
        const bool sym = SymInitialize(process, searchPath.c_str(), FALSE);
        if (sym) {
            SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_FAIL_CRITICAL_ERRORS | SYMOPT_NO_PROMPTS);
        }
        std::vector<bool> loaded(modules.size(), false);
        auto out = Json::array();
        for (const auto address : addresses) {
            auto it = std::upper_bound(modules.begin(), modules.end(), address, [](std::uintptr_t a, const ModuleInfo& m) { return a < m.base; });
            if (it == modules.begin() || address >= (--it)->end) {
                out.push_back({ { "address", address } });
                continue;
            }
            const auto index = static_cast<std::size_t>(it - modules.begin());
            Json frame{ { "module", index }, { "rva", address - it->base } };
            if (it->base == game && !ids.empty()) {
                const auto rva = address - game;
                auto found = std::upper_bound(ids.begin(), ids.end(), rva, [](std::uintptr_t a, const REL::IDDB::mapping_t& m) { return a < m.offset; });
                if (found != ids.begin()) {
                    --found;
                    frame["id"] = found->id;
                    frame["idOffset"] = rva - found->offset;
                }
            } else if (sym) {
                if (!loaded[index]) {
                    loaded[index] = true;
                    SymLoadModuleEx(process, nullptr, it->path.c_str(), nullptr, it->base, static_cast<DWORD>(it->end - it->base), nullptr, 0);
                }
                alignas(SYMBOL_INFO) char storage[sizeof(SYMBOL_INFO) + 512]{};
                auto* info = reinterpret_cast<SYMBOL_INFO*>(storage);
                info->SizeOfStruct = sizeof(SYMBOL_INFO);
                info->MaxNameLen = 511;
                DWORD64 displacement = 0;
                if (SymFromAddr(process, address, &displacement, info)) {
                    frame["name"] = std::string(info->Name, info->NameLen);
                    frame["offset"] = displacement;
                }
            }
            out.push_back(std::move(frame));
        }
        if (sym) {
            SymCleanup(process);
        }
        return out;
    }

    std::filesystem::path RunDir()
    {
        wchar_t buffer[2048]{};
        const auto n = GetEnvironmentVariableW(L"SKYRIM_DEVTOOLS_RUN_DIR", buffer, static_cast<DWORD>(std::size(buffer)));
        return n && n < std::size(buffer) ? std::filesystem::path(std::wstring(buffer, n)) : std::filesystem::path{};
    }

    std::atomic<int> dumpSerial = 0;

    Json Dump(const std::string& label)
    {
        quiet = true;
        struct Restore { ~Restore() { quiet = false; } } restore;
        const auto dir = RunDir();
        if (dir.empty()) {
            throw RpcError(code::notReady, "SKYRIM_DEVTOOLS_RUN_DIR is not set");
        }
        const auto stem = "iotrace-" + std::to_string(++dumpSerial) + (label.empty() ? "" : "-" + label);
        const auto count = std::min<std::uint64_t>(nextEvent.load(), settings.maxEvents);

        // Events go out as raw records; slots still being written (op 0) are zeroed so the client skips them.
        const auto binPath = dir / (stem + ".bin");
        std::uint64_t written = 0;
        {
            FILE* f = _wfopen(binPath.c_str(), L"wb");
            if (!f) {
                throw RpcError(code::internal, "cannot write " + binPath.string());
            }
            for (std::uint64_t i = 0; i < count; i += chunkEvents) {
                const auto* base = chunks[i / chunkEvents].load(std::memory_order_acquire);
                const auto n = std::min<std::uint64_t>(chunkEvents, count - i);
                if (!base) {
                    continue;
                }
                std::vector<Event> copy(n);
                for (std::uint64_t k = 0; k < n; ++k) {
                    const auto op = base[k].op.load(std::memory_order_acquire);
                    std::memcpy(&copy[k], &base[k], sizeof(Event));
                    if (!op) {
                        std::memset(&copy[k], 0, sizeof(Event));
                    }
                }
                fwrite(copy.data(), sizeof(Event), n, f);
                written += n;
            }
            fclose(f);
        }

        Json meta;
        meta["format"] = 1;
        meta["eventSize"] = sizeof(Event);
        meta["events"] = written;
        meta["dropped"] = dropped.load();
        meta["qpcFrequency"] = qpcFrequency;
        meta["armedAt"] = armedAt;
        meta["armedEpochMs"] = armedEpochMs;
        meta["dumpedAt"] = Now();
        meta["build"] = GetConfig().build;
        meta["settings"] = { { "stackDepth", settings.stackDepth }, { "maxEvents", settings.maxEvents }, { "counterMs", settings.counterMs },
            { "threadMs", settings.threadMs } };
        meta["hooks"] = hookStatus;
        meta["hookOverheadS"] = static_cast<double>(hookTicks.load()) / static_cast<double>(qpcFrequency);
        {
            std::shared_lock lock(fileMutex);
            auto files = Json::array();
            for (std::size_t i = 0; i < fileNames.size(); ++i) {
                files.push_back({ Narrow(fileNames[i]), Narrow(filePhysical[i]) });
            }
            meta["files"] = std::move(files);
        }
        {
            std::lock_guard lock(threadMutex);
            meta["threads"] = threadList;
        }
        const auto modules = Modules();
        {
            auto list = Json::array();
            for (const auto& m : modules) {
                list.push_back({ { "path", m.path }, { "base", m.base }, { "size", m.end - m.base } });
            }
            meta["modules"] = std::move(list);
            meta["gameBase"] = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
        }
        {
            std::vector<std::vector<std::uintptr_t>> stacks;
            {
                std::lock_guard lock(stackMutex);
                stacks = stackFrames;
            }
            std::unordered_map<std::uintptr_t, std::uint32_t> unique;
            std::vector<std::uintptr_t> addresses;
            auto list = Json::array();
            for (const auto& s : stacks) {
                auto row = Json::array();
                for (const auto a : s) {
                    const auto [it, inserted] = unique.emplace(a, static_cast<std::uint32_t>(addresses.size()));
                    if (inserted) {
                        addresses.push_back(a);
                    }
                    row.push_back(it->second);
                }
                list.push_back(std::move(row));
            }
            meta["stacks"] = std::move(list);
            meta["frames"] = ResolveFrames(addresses, modules);
        }
        {
            std::lock_guard lock(counterMutex);
            auto c = Json::array();
            for (const auto& s : counters) {
                c.push_back({ s.at, s.pageFaults, s.workingSet, s.privateBytes, s.availPhys, s.readOps, s.readBytes, s.otherOps, s.sysPageReads,
                    s.sysPageReadIos, s.sysTransition, s.sysDemandZero, s.sysCacheTransition, s.sysFaults });
            }
            meta["counterColumns"] = { "at", "pageFaults", "workingSet", "privateBytes", "availPhys", "readOps", "readBytes", "otherOps",
                "sysPageReads", "sysPageReadIos", "sysTransition", "sysDemandZero", "sysCacheTransition", "sysFaults" };
            meta["counters"] = std::move(c);
            auto t = Json::array();
            for (const auto& s : threadSamples) {
                t.push_back({ s.at, s.tid, s.cycles, s.cpu100ns });
            }
            meta["threadSamples"] = std::move(t);
            auto names = Json::object();
            for (const auto& [tid, name] : threadNames) {
                names[std::to_string(tid)] = Narrow(name);
            }
            meta["threadNames"] = std::move(names);
        }
        {
            std::lock_guard lock(markMutex);
            auto m = Json::array();
            for (const auto& [at, name] : marks) {
                m.push_back({ at, name });
            }
            meta["marks"] = std::move(m);
        }
        const auto jsonPath = dir / (stem + ".json");
        {
            FILE* f = _wfopen(jsonPath.c_str(), L"wb");
            if (!f) {
                throw RpcError(code::internal, "cannot write " + jsonPath.string());
            }
            const auto text = meta.dump();
            fwrite(text.data(), 1, text.size(), f);
            fclose(f);
        }
        return { { "bin", binPath.string() }, { "json", jsonPath.string() }, { "events", written }, { "dropped", dropped.load() },
            { "files", meta["files"].size() }, { "stacks", meta["stacks"].size() } };
    }

    Json Status()
    {
        return { { "armed", settings.armed }, { "recording", recording.load() }, { "events", std::min<std::uint64_t>(nextEvent.load(), settings.maxEvents) },
            { "dropped", dropped.load() }, { "hooks", hookStatus },
            { "hookOverheadS", static_cast<double>(hookTicks.load()) / static_cast<double>(qpcFrequency) }, { "files", fileNames.size() } };
    }
}

DEVTOOLS_MODULE(IoTrace)
{
    SetCapability("iotrace", settings.armed);
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (const auto name = SkseMessageName(message.type)) {
            Mark(name);
        }
        if (settings.armed && message.type == SKSE::MessagingInterface::kInputLoaded) {
            static MenuSink sink;
            if (const auto ui = RE::UI::GetSingleton()) {
                ui->AddEventSink<RE::MenuOpenCloseEvent>(&sink);
            }
        }
    });
    Register("IoTrace.status", [](const Json& params) {
        AllowOnly(params, {});
        return Status();
    }, { .thread = Thread::Any });
    Register("IoTrace.mark", [](const Json& params) {
        AllowOnly(params, { "name" });
        if (!settings.armed) {
            throw RpcError(code::notReady, "I/O trace is off; start the game with SKYRIM_DEVTOOLS_IOTRACE=1");
        }
        Mark(Require<std::string>(params, "name"));
        return Json{ { "ok", true } };
    }, { .thread = Thread::Any });
    Register("IoTrace.pause", [](const Json& params) {
        AllowOnly(params, { "paused" });
        if (!settings.armed) {
            throw RpcError(code::notReady, "I/O trace is off; start the game with SKYRIM_DEVTOOLS_IOTRACE=1");
        }
        recording = !Optional<bool>(params, "paused", true);
        Mark(recording ? "iotrace resumed" : "iotrace paused");
        return Status();
    }, { .thread = Thread::Any });
    Register("IoTrace.dump", [](const Json& params) {
        AllowOnly(params, { "label" });
        if (!settings.armed) {
            throw RpcError(code::notReady, "I/O trace is off; start the game with SKYRIM_DEVTOOLS_IOTRACE=1");
        }
        auto label = Optional<std::string>(params, "label", "");
        for (auto& c : label) {
            if (!std::isalnum(static_cast<unsigned char>(c)) && c != '-' && c != '_') {
                c = '_';
            }
        }
        return Dump(label.substr(0, 40));
    }, { .thread = Thread::Any, .timeout = std::chrono::milliseconds(120000) });
}
