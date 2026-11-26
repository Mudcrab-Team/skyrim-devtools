// SPDX-License-Identifier: GPL-3.0-or-later
// OS runtime tracing: waits, sleeps and file reads made by the game executable. These are patched in the
// executable's import table rather than inline, so calls from the driver, the plugin itself and the sampler
// thread are never touched and a wait inside the profiler cannot recurse into the trace.
//
// CreateFile is patched when the plugin loads so handles opened before a profile (archives open at startup)
// still map to a file category; the other imports are patched on the first traced profile.
#include "trace.h"

#include "logs.h"

#include <Windows.h>

#include <algorithm>
#include <cctype>
#include <cstring>
#include <mutex>
#include <string>
#include <unordered_map>

namespace devtools::trace
{
    namespace
    {
        using WaitForSingleObjectFn = DWORD(WINAPI*)(HANDLE, DWORD);
        using WaitForSingleObjectExFn = DWORD(WINAPI*)(HANDLE, DWORD, BOOL);
        using WaitForMultipleObjectsFn = DWORD(WINAPI*)(DWORD, const HANDLE*, BOOL, DWORD);
        using WaitForMultipleObjectsExFn = DWORD(WINAPI*)(DWORD, const HANDLE*, BOOL, DWORD, BOOL);
        using SleepFn = void(WINAPI*)(DWORD);
        using SleepExFn = DWORD(WINAPI*)(DWORD, BOOL);
        using SwitchToThreadFn = BOOL(WINAPI*)();
        using SleepConditionVariableCSFn = BOOL(WINAPI*)(PCONDITION_VARIABLE, PCRITICAL_SECTION, DWORD);
        using SleepConditionVariableSRWFn = BOOL(WINAPI*)(PCONDITION_VARIABLE, PSRWLOCK, DWORD, ULONG);
        using ReadFileFn = BOOL(WINAPI*)(HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED);
        using CreateFileAFn = HANDLE(WINAPI*)(LPCSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
        using CreateFileWFn = HANDLE(WINAPI*)(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);

        WaitForSingleObjectFn originalWaitForSingleObject = &WaitForSingleObject;
        WaitForSingleObjectExFn originalWaitForSingleObjectEx = &WaitForSingleObjectEx;
        WaitForMultipleObjectsFn originalWaitForMultipleObjects = &WaitForMultipleObjects;
        WaitForMultipleObjectsExFn originalWaitForMultipleObjectsEx = &WaitForMultipleObjectsEx;
        SleepFn originalSleep = &Sleep;
        SleepExFn originalSleepEx = &SleepEx;
        SwitchToThreadFn originalSwitchToThread = &SwitchToThread;
        SleepConditionVariableCSFn originalSleepConditionVariableCS = &SleepConditionVariableCS;
        SleepConditionVariableSRWFn originalSleepConditionVariableSRW = &SleepConditionVariableSRW;
        ReadFileFn originalReadFile = &ReadFile;
        CreateFileAFn originalCreateFileA = &CreateFileA;
        CreateFileWFn originalCreateFileW = &CreateFileW;

        // File categories; the index is the event's `b` value and the name suffix.
        constexpr const char* categories[] = { "other", "archive", "plugin", "save", "ini", "dll", "texture", "mesh", "sound",
            "script" };
        constexpr std::uint8_t otherCategory = 0;

        std::mutex handleMutex;
        std::unordered_map<HANDLE, std::uint8_t> handleCategories;

        bool waitsPatched = false;
        bool filesPatched = false;
        Json patched = Json::object();

        struct WaitNames
        {
            std::uint32_t single, singleEx, multiple, multipleEx, sleep, sleepEx, switchThread, cvCs, cvSrw;
            std::uint32_t read[std::size(categories)];
        } names{};

        template <class Char>
        std::uint8_t Categorize(const Char* path)
        {
            if (!path) {
                return otherCategory;
            }
            std::string ext;
            for (const Char* p = path; *p; ++p) {
                if (*p == '.') {
                    ext.clear();
                } else if (*p == '\\' || *p == '/') {
                    ext.clear();
                } else if (ext.size() < 8) {
                    ext += static_cast<char>(std::tolower(static_cast<unsigned char>(*p & 0x7F)));
                }
            }
            if (ext == "bsa" || ext == "ba2") {
                return 1;
            }
            if (ext == "esm" || ext == "esp" || ext == "esl") {
                return 2;
            }
            if (ext == "ess" || ext == "skse" || ext == "tmp") {
                return 3;
            }
            if (ext == "ini") {
                return 4;
            }
            if (ext == "dll") {
                return 5;
            }
            if (ext == "dds") {
                return 6;
            }
            if (ext == "nif" || ext == "hkx" || ext == "tri" || ext == "btr" || ext == "bto") {
                return 7;
            }
            if (ext == "wav" || ext == "xwm" || ext == "fuz" || ext == "lip") {
                return 8;
            }
            if (ext == "pex" || ext == "psc") {
                return 9;
            }
            return otherCategory;
        }

        void Remember(HANDLE handle, std::uint8_t category)
        {
            if (handle == INVALID_HANDLE_VALUE || !handle) {
                return;
            }
            std::lock_guard lock(handleMutex);
            handleCategories[handle] = category;
        }

        std::uint8_t CategoryOf(HANDLE handle)
        {
            std::lock_guard lock(handleMutex);
            const auto it = handleCategories.find(handle);
            return it == handleCategories.end() ? otherCategory : it->second;
        }

        void Wait(std::uint32_t name, std::int64_t start, std::uint64_t timeout, std::uint64_t result)
        {
            const auto end = Ticks();
            if (end - start < Current().waitMinTicks) {
                CountShortWait();
                return;
            }
            Complete(name, start, end, timeout, result);
        }

        bool Recording()
        {
            return Active() && Current().os;
        }

        DWORD WINAPI HookedWaitForSingleObject(HANDLE handle, DWORD ms)
        {
            if (!Recording()) {
                return originalWaitForSingleObject(handle, ms);
            }
            const auto start = Ticks();
            const auto result = originalWaitForSingleObject(handle, ms);
            Wait(names.single, start, ms, result);
            return result;
        }

        DWORD WINAPI HookedWaitForSingleObjectEx(HANDLE handle, DWORD ms, BOOL alertable)
        {
            if (!Recording()) {
                return originalWaitForSingleObjectEx(handle, ms, alertable);
            }
            const auto start = Ticks();
            const auto result = originalWaitForSingleObjectEx(handle, ms, alertable);
            Wait(names.singleEx, start, ms, result);
            return result;
        }

        DWORD WINAPI HookedWaitForMultipleObjects(DWORD count, const HANDLE* handles, BOOL all, DWORD ms)
        {
            if (!Recording()) {
                return originalWaitForMultipleObjects(count, handles, all, ms);
            }
            const auto start = Ticks();
            const auto result = originalWaitForMultipleObjects(count, handles, all, ms);
            Wait(names.multiple, start, ms, result);
            return result;
        }

        DWORD WINAPI HookedWaitForMultipleObjectsEx(DWORD count, const HANDLE* handles, BOOL all, DWORD ms, BOOL alertable)
        {
            if (!Recording()) {
                return originalWaitForMultipleObjectsEx(count, handles, all, ms, alertable);
            }
            const auto start = Ticks();
            const auto result = originalWaitForMultipleObjectsEx(count, handles, all, ms, alertable);
            Wait(names.multipleEx, start, ms, result);
            return result;
        }

        void WINAPI HookedSleep(DWORD ms)
        {
            if (!Recording()) {
                return originalSleep(ms);
            }
            const auto start = Ticks();
            originalSleep(ms);
            Wait(names.sleep, start, ms, 0);
        }

        DWORD WINAPI HookedSleepEx(DWORD ms, BOOL alertable)
        {
            if (!Recording()) {
                return originalSleepEx(ms, alertable);
            }
            const auto start = Ticks();
            const auto result = originalSleepEx(ms, alertable);
            Wait(names.sleepEx, start, ms, result);
            return result;
        }

        BOOL WINAPI HookedSwitchToThread()
        {
            if (!Recording()) {
                return originalSwitchToThread();
            }
            const auto start = Ticks();
            const auto result = originalSwitchToThread();
            Wait(names.switchThread, start, 0, result);
            return result;
        }

        BOOL WINAPI HookedSleepConditionVariableCS(PCONDITION_VARIABLE cv, PCRITICAL_SECTION cs, DWORD ms)
        {
            if (!Recording()) {
                return originalSleepConditionVariableCS(cv, cs, ms);
            }
            const auto start = Ticks();
            const auto result = originalSleepConditionVariableCS(cv, cs, ms);
            Wait(names.cvCs, start, ms, result);
            return result;
        }

        BOOL WINAPI HookedSleepConditionVariableSRW(PCONDITION_VARIABLE cv, PSRWLOCK lock, DWORD ms, ULONG flags)
        {
            if (!Recording()) {
                return originalSleepConditionVariableSRW(cv, lock, ms, flags);
            }
            const auto start = Ticks();
            const auto result = originalSleepConditionVariableSRW(cv, lock, ms, flags);
            Wait(names.cvSrw, start, ms, result);
            return result;
        }

        BOOL WINAPI HookedReadFile(HANDLE file, LPVOID buffer, DWORD size, LPDWORD read, LPOVERLAPPED overlapped)
        {
            if (!Recording()) {
                return originalReadFile(file, buffer, size, read, overlapped);
            }
            const auto start = Ticks();
            const auto result = originalReadFile(file, buffer, size, read, overlapped);
            const auto category = CategoryOf(file);
            // Overlapped reads return before the data arrives; the requested size is the best available.
            const std::uint64_t bytes = (!overlapped && read && result) ? *read : size;
            Complete(names.read[category], start, Ticks(), bytes, category | (overlapped ? 0x100 : 0));
            return result;
        }

        HANDLE WINAPI HookedCreateFileA(LPCSTR path, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES security, DWORD disposition,
            DWORD flags, HANDLE source)
        {
            const auto handle = originalCreateFileA(path, access, share, security, disposition, flags, source);
            Remember(handle, Categorize(path));
            return handle;
        }

        HANDLE WINAPI HookedCreateFileW(LPCWSTR path, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES security, DWORD disposition,
            DWORD flags, HANDLE source)
        {
            const auto handle = originalCreateFileW(path, access, share, security, disposition, flags, source);
            Remember(handle, Categorize(path));
            return handle;
        }

        // Swaps every import of `function` (by name, from any DLL) in the executable. Returns how many slots changed.
        template <class Fn>
        int PatchImport(const char* function, Fn detour, Fn& original)
        {
            const auto base = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
            const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
            const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
            const auto& directory = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
            if (!directory.VirtualAddress) {
                return 0;
            }
            int count = 0;
            for (auto* import = reinterpret_cast<const IMAGE_IMPORT_DESCRIPTOR*>(base + directory.VirtualAddress); import->Name; ++import) {
                if (!import->OriginalFirstThunk) {
                    continue;
                }
                auto* lookup = reinterpret_cast<const IMAGE_THUNK_DATA64*>(base + import->OriginalFirstThunk);
                auto* slots = reinterpret_cast<IMAGE_THUNK_DATA64*>(base + import->FirstThunk);
                for (; lookup->u1.AddressOfData; ++lookup, ++slots) {
                    if (IMAGE_SNAP_BY_ORDINAL64(lookup->u1.Ordinal)) {
                        continue;
                    }
                    const auto* byName = reinterpret_cast<const IMAGE_IMPORT_BY_NAME*>(base + lookup->u1.AddressOfData);
                    if (std::strcmp(reinterpret_cast<const char*>(byName->Name), function) != 0) {
                        continue;
                    }
                    DWORD protection = 0;
                    if (!VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), PAGE_READWRITE, &protection)) {
                        continue;
                    }
                    if (slots->u1.Function != reinterpret_cast<ULONGLONG>(detour)) {
                        // Another hook may already sit in the slot; chaining to it keeps that hook working.
                        original = reinterpret_cast<Fn>(slots->u1.Function);
                        slots->u1.Function = reinterpret_cast<ULONGLONG>(detour);
                    }
                    VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), protection, &protection);
                    ++count;
                }
            }
            patched[function] = count;
            return count;
        }
    }

    void InstallFileMapHooks()
    {
        if (filesPatched) {
            return;
        }
        filesPatched = true;
        PatchImport("CreateFileA", &HookedCreateFileA, originalCreateFileA);
        PatchImport("CreateFileW", &HookedCreateFileW, originalCreateFileW);
    }

    Json InstallOsHooks()
    {
        if (!waitsPatched) {
            waitsPatched = true;
            names.single = Name("WaitForSingleObject", "wait");
            names.singleEx = Name("WaitForSingleObjectEx", "wait");
            names.multiple = Name("WaitForMultipleObjects", "wait");
            names.multipleEx = Name("WaitForMultipleObjectsEx", "wait");
            names.sleep = Name("Sleep", "wait");
            names.sleepEx = Name("SleepEx", "wait");
            names.switchThread = Name("SwitchToThread", "wait");
            names.cvCs = Name("SleepConditionVariableCS", "wait");
            names.cvSrw = Name("SleepConditionVariableSRW", "wait");
            for (std::size_t i = 0; i < std::size(categories); ++i) {
                names.read[i] = Name(std::string("ReadFile ") + categories[i], "io");
            }
            PatchImport("WaitForSingleObject", &HookedWaitForSingleObject, originalWaitForSingleObject);
            PatchImport("WaitForSingleObjectEx", &HookedWaitForSingleObjectEx, originalWaitForSingleObjectEx);
            PatchImport("WaitForMultipleObjects", &HookedWaitForMultipleObjects, originalWaitForMultipleObjects);
            PatchImport("WaitForMultipleObjectsEx", &HookedWaitForMultipleObjectsEx, originalWaitForMultipleObjectsEx);
            PatchImport("Sleep", &HookedSleep, originalSleep);
            PatchImport("SleepEx", &HookedSleepEx, originalSleepEx);
            PatchImport("SwitchToThread", &HookedSwitchToThread, originalSwitchToThread);
            PatchImport("SleepConditionVariableCS", &HookedSleepConditionVariableCS, originalSleepConditionVariableCS);
            PatchImport("SleepConditionVariableSRW", &HookedSleepConditionVariableSRW, originalSleepConditionVariableSRW);
            PatchImport("ReadFile", &HookedReadFile, originalReadFile);
        }
        return OsStatus();
    }

    Json OsStatus()
    {
        std::size_t handles = 0;
        {
            std::lock_guard lock(handleMutex);
            handles = handleCategories.size();
        }
        return Json{ { "imports", patched }, { "knownHandles", handles } };
    }
}
