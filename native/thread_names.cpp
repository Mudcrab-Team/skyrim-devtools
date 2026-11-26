// SPDX-License-Identifier: GPL-3.0-or-later
#include "thread_names.h"

#include "methods.h"

#include <array>
#include <mutex>
#include <shared_mutex>
#include <unordered_map>

namespace devtools::threads
{
    namespace
    {
        constexpr DWORD msvcSetThreadName = 0x406D1388;
        constexpr DWORD threadNameInfoType = 0x1000;
        constexpr ULONG threadQuerySetWin32StartAddress = 9;

        std::shared_mutex namesMutex;
        // Leaked on purpose: the handler can run during process teardown, after static destructors.
        auto* exceptionNames = new std::unordered_map<DWORD, std::string>;

        // Only plain locals here, so the structured handler can guard a name pointer from the game.
        bool CopyName(const char* text, char (&out)[64])
        {
            __try {
                std::size_t i = 0;
                for (; i + 1 < sizeof(out) && text[i]; ++i) {
                    out[i] = text[i];
                }
                out[i] = 0;
                return i > 0;
            } __except (EXCEPTION_EXECUTE_HANDLER) {
                return false;
            }
        }

        // The game's own handler still runs (CONTINUE_SEARCH), so naming behaves as without the plugin.
        LONG CALLBACK OnException(EXCEPTION_POINTERS* info)
        {
            const auto* record = info->ExceptionRecord;
            if (record->ExceptionCode != msvcSetThreadName || record->NumberParameters < 3) {
                return EXCEPTION_CONTINUE_SEARCH;
            }
            // THREADNAME_INFO on x64: {DWORD type; LPCSTR name; DWORD threadId; DWORD flags} in three words;
            // the high halves of the first and last word are padding and flags.
            const auto* words = record->ExceptionInformation;
            if (static_cast<DWORD>(words[0]) != threadNameInfoType || !words[1]) {
                return EXCEPTION_CONTINUE_SEARCH;
            }
            char copy[64];
            if (!CopyName(reinterpret_cast<const char*>(words[1]), copy)) {
                return EXCEPTION_CONTINUE_SEARCH;
            }
            DWORD tid = static_cast<DWORD>(words[2]);
            if (tid == 0xFFFFFFFF) {
                tid = GetCurrentThreadId();
            }
            std::unique_lock lock(namesMutex);
            (*exceptionNames)[tid] = copy;
            return EXCEPTION_CONTINUE_SEARCH;
        }

        std::string Narrow(PCWSTR wide)
        {
            const int size = WideCharToMultiByte(CP_UTF8, 0, wide, -1, nullptr, 0, nullptr, nullptr);
            std::string out;
            if (size > 1) {
                out.resize(static_cast<std::size_t>(size - 1));
                WideCharToMultiByte(CP_UTF8, 0, wide, -1, out.data(), size, nullptr, nullptr);
            }
            return out;
        }

        std::string Description(HANDLE thread)
        {
            PWSTR wide = nullptr;
            std::string out;
            if (SUCCEEDED(GetThreadDescription(thread, &wide)) && wide) {
                out = Narrow(wide);
                LocalFree(wide);
            }
            return out;
        }

        std::pair<std::uintptr_t, std::uintptr_t> ExecutableRange()
        {
            const auto base = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(nullptr));
            const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
            const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
            return { base, base + nt->OptionalHeader.SizeOfImage };
        }
    }

    Name NameOf(DWORD tid, HANDLE thread)
    {
        HANDLE opened = nullptr;
        if (!thread) {
            thread = opened = OpenThread(THREAD_QUERY_LIMITED_INFORMATION, FALSE, tid);
        }
        Name out;
        if (thread) {
            out.text = Description(thread);
            if (!out.text.empty()) {
                out.source = "description";
            }
        }
        if (opened) {
            CloseHandle(opened);
        }
        if (out.text.empty()) {
            std::shared_lock lock(namesMutex);
            if (const auto found = exceptionNames->find(tid); found != exceptionNames->end()) {
                out.text = found->second;
                out.source = "exception";
            }
        }
        return out;
    }

    std::uintptr_t StartAddress(HANDLE thread)
    {
        using Query = LONG(NTAPI*)(HANDLE, ULONG, PVOID, ULONG, PULONG);
        static const auto query = reinterpret_cast<Query>(
            GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQueryInformationThread"));
        PVOID start = nullptr;
        if (!thread || !query || query(thread, threadQuerySetWin32StartAddress, &start, sizeof(start), nullptr) < 0) {
            return 0;
        }
        return reinterpret_cast<std::uintptr_t>(start);
    }

    std::uintptr_t RootGameFrame()
    {
        static const auto range = ExecutableRange();
        std::array<PVOID, 128> frames{};
        const USHORT count = RtlCaptureStackBackTrace(0, static_cast<DWORD>(frames.size()), frames.data(), nullptr);
        for (USHORT i = count; i-- > 0;) {
            const auto address = reinterpret_cast<std::uintptr_t>(frames[i]);
            if (address >= range.first && address < range.second) {
                return address;
            }
        }
        return 0;
    }

    Json Where(std::uintptr_t address)
    {
        HMODULE module = nullptr;
        if (!address || !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                reinterpret_cast<LPCWSTR>(address), &module)) {
            return nullptr;
        }
        wchar_t path[MAX_PATH]{};
        const DWORD size = GetModuleFileNameW(module, path, MAX_PATH);
        std::wstring_view view(path, size);
        const auto slash = view.find_last_of(L"\\/");
        const std::wstring file(slash == std::wstring_view::npos ? view : view.substr(slash + 1));
        return Json{ { "module", Narrow(file.c_str()) }, { "rva", address - reinterpret_cast<std::uintptr_t>(module) } };
    }

    void Describe(Json& out, DWORD tid, HANDLE thread, std::uintptr_t start, std::uintptr_t root, const Name& early)
    {
        auto name = NameOf(tid, thread);
        if (name.text.empty()) {
            name = early;
        }
        out["name"] = name.text;
        out["nameSource"] = name.source;
        out["start"] = Where(start);
        out["root"] = Where(root);
    }
}

DEVTOOLS_MODULE(ThreadNames)
{
    // First in the chain so the name is seen before any frame handler swallows the exception.
    AddVectoredExceptionHandler(1, devtools::threads::OnException);
}
