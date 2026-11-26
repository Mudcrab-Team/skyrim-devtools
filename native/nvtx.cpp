// SPDX-License-Identifier: GPL-3.0-or-later
#include "nvtx.h"

#include <Windows.h>

#include <mutex>
#include <string>

namespace devtools::nvtx
{
    namespace
    {
        using RangeStartFn = std::uint64_t(__cdecl*)(const char*);
        using RangeEndFn = void(__cdecl*)(std::uint64_t);
        using MarkFn = void(__cdecl*)(const char*);

        RangeStartFn rangeStart = nullptr;
        RangeEndFn rangeEnd = nullptr;
        MarkFn mark = nullptr;
        std::once_flag loaded;

        HMODULE Find()
        {
            constexpr auto name = L"nvToolsExt64_1.dll";
            if (const HMODULE module = GetModuleHandleW(name)) {
                return module;
            }
            // The NVTX installer sets NVTOOLSEXT_PATH to its root; the DLL sits under bin\x64.
            wchar_t root[MAX_PATH];
            const DWORD length = GetEnvironmentVariableW(L"NVTOOLSEXT_PATH", root, MAX_PATH);
            if (length && length < MAX_PATH) {
                std::wstring path(root, length);
                if (!path.ends_with(L"\\")) {
                    path += L"\\";
                }
                path += L"bin\\x64\\";
                path += name;
                if (const HMODULE module = LoadLibraryW(path.c_str())) {
                    return module;
                }
            }
            return LoadLibraryExW(name, nullptr, LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        }

        void Load()
        {
            std::call_once(loaded, [] {
                const HMODULE module = Find();
                if (!module) {
                    return;
                }
                rangeStart = reinterpret_cast<RangeStartFn>(GetProcAddress(module, "nvtxRangeStartA"));
                rangeEnd = reinterpret_cast<RangeEndFn>(GetProcAddress(module, "nvtxRangeEnd"));
                mark = reinterpret_cast<MarkFn>(GetProcAddress(module, "nvtxMarkA"));
                if (!rangeStart || !rangeEnd || !mark) {
                    rangeStart = nullptr;
                    rangeEnd = nullptr;
                    mark = nullptr;
                }
            });
        }
    }

    bool Available()
    {
        Load();
        return mark != nullptr;
    }

    std::uint64_t RangeStart(std::string_view name)
    {
        if (!Available()) {
            return 0;
        }
        return rangeStart(std::string(name).c_str());
    }

    void RangeEnd(std::uint64_t id)
    {
        if (id && Available()) {
            rangeEnd(id);
        }
    }

    void Mark(std::string_view name)
    {
        if (Available()) {
            mark(std::string(name).c_str());
        }
    }
}
