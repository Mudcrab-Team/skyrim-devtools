// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Console commands with captured output. ConsoleLog::VPrint is not virtual, so its output is
// observed through a MinHook detour rather than a vtable patch.
#include "console.h"

#include "events.h"
#include "game.h"
#include "methods.h"

#include <MinHook.h>

#include <cstdarg>
#include <cstdio>
#include <deque>
#include <mutex>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    using VPrint = void (*)(RE::ConsoleLog*, const char*, std::va_list);
    VPrint originalVPrint = nullptr;
    bool hooked = false;

    thread_local std::vector<std::string>* capture = nullptr;
    // Set while Run executes a script, so the compiler detour does not report it a second time as typed.
    thread_local bool insideRun = false;

    console::CommandObserver observer;
    bool compilerHooked = false;

    using CompileAndRun = void (*)(RE::Script*, RE::ScriptCompiler*, RE::COMPILER_NAME, RE::TESObjectREFR*);
    CompileAndRun originalCompile = nullptr;

    void HookedCompile(RE::Script* script, RE::ScriptCompiler* compiler, RE::COMPILER_NAME name, RE::TESObjectREFR* target)
    {
        // The console window compiles with the system-window compiler; script and quest compilers are not typed input.
        if (!insideRun && observer && script && name == RE::COMPILER_NAME::kSystemWindowCompiler) {
            const auto command = script->GetCommand();
            if (!command.empty()) {
                observer(command, true);
            }
        }
        originalCompile(script, compiler, name, target);
    }

    bool InstallCompileHook()
    {
        REL::Relocation<std::uintptr_t> target{ RELOCATION_ID(21416, 21890) };
        auto* address = reinterpret_cast<void*>(target.address());
        return MH_CreateHook(address, reinterpret_cast<void*>(&HookedCompile), reinterpret_cast<void**>(&originalCompile)) == MH_OK &&
               MH_EnableHook(address) == MH_OK;
    }

    std::mutex historyMutex;
    std::deque<std::pair<std::uint64_t, std::string>> history;
    std::uint64_t lineSequence = 0;
    constexpr std::size_t historyLimit = 1000;

    void Record(std::string_view text)
    {
        while (!text.empty()) {
            const auto end = text.find('\n');
            auto line = std::string(text.substr(0, end));
            text = end == std::string_view::npos ? std::string_view{} : text.substr(end + 1);
            if (!line.empty() && line.back() == '\r') {
                line.pop_back();
            }
            if (capture) {
                capture->push_back(line);
            }
            std::uint64_t seq = 0;
            {
                std::lock_guard lock(historyMutex);
                seq = ++lineSequence;
                history.emplace_back(seq, line);
                if (history.size() > historyLimit) {
                    history.pop_front();
                }
            }
            events::Emit("console", { { "line", line }, { "lineSeq", seq } });
        }
    }

    void HookedVPrint(RE::ConsoleLog* log, const char* format, std::va_list args)
    {
        if (format) {
            std::va_list copy;
            va_copy(copy, args);
            char buffer[8192];
            const int written = std::vsnprintf(buffer, sizeof(buffer), format, copy);
            va_end(copy);
            if (written > 0) {
                Record(std::string_view(buffer, std::min<std::size_t>(written, sizeof(buffer) - 1)));
            }
        }
        originalVPrint(log, format, args);
    }

    bool InstallHook()
    {
        REL::Relocation<std::uintptr_t> target{ RELOCATION_ID(50180, 51110) };
        auto* address = reinterpret_cast<void*>(target.address());
        return MH_CreateHook(address, reinterpret_cast<void*>(&HookedVPrint),
                   reinterpret_cast<void**>(&originalVPrint)) == MH_OK &&
               MH_EnableHook(address) == MH_OK;
    }

    RE::TESObjectREFR* Target(const Json& params, RE::NiPointer<RE::TESObjectREFR>& hold)
    {
        const auto target = Optional<std::string>(params, "target", "selected");
        if (target == "player") {
            return RE::PlayerCharacter::GetSingleton();
        }
        if (target == "none") {
            return nullptr;
        }
        if (target != "selected") {
            throw RpcError(code::invalidParams, "target is selected, player or none");
        }
        hold = RE::Console::GetSelectedRef();
        return hold.get();
    }
}

namespace devtools::console
{
    void SetCommandObserver(CommandObserver next) { observer = std::move(next); }

    std::vector<std::string> Run(std::string_view command, RE::TESObjectREFR* target)
    {
        if (!hooked) {
            throw RpcError(code::consoleUnavailable, "The console output hook is not installed");
        }
        if (command.empty() || command.size() > 1000 || command.find('\n') != std::string_view::npos) {
            throw RpcError(code::invalidParams, "A command is one non-empty line");
        }
        const auto factory = RE::IFormFactory::GetConcreteFormFactoryByType<RE::Script>();
        const auto script = factory ? factory->Create() : nullptr;
        if (!script) {
            throw RpcError(code::consoleUnavailable, "The script factory is unavailable");
        }
        std::vector<std::string> lines;
        capture = &lines;
        insideRun = true;
        script->SetCommand(command);
        if (observer) {
            observer(command, false);
        }
        script->CompileAndRun(target);
        insideRun = false;
        capture = nullptr;
        delete script;
        return lines;
    }
}

DEVTOOLS_MODULE(Console)
{
    events::Declare("console", "A line printed to the game console: {line, lineSeq}");
    hooked = InstallHook();
    compilerHooked = InstallCompileHook();
    SetCapability("consoleTyped", compilerHooked);
    SetCapability("console", hooked);

    Register("Console.execute", [](const Json& params) {
        AllowOnly(params, { "command", "target", "loadEpoch" });
        if (params.contains("loadEpoch")) {
            RequirePlayable(params);
        }
        RE::NiPointer<RE::TESObjectREFR> hold;
        const auto target = Target(params, hold);
        const auto command = Require<std::string>(params, "command");
        return Json{ { "command", command }, { "lines", console::Run(command, target) } };
    }, { .thread = Thread::Main, .timeout = 15s });
    Register("Console.history", [](const Json& params) {
        AllowOnly(params, { "since" });
        const auto since = Optional<std::uint64_t>(params, "since", 0);
        std::lock_guard lock(historyMutex);
        auto lines = Json::array();
        for (const auto& [seq, line] : history) {
            if (seq > since) {
                lines.push_back({ { "lineSeq", seq }, { "line", line } });
            }
        }
        return Json{ { "lines", std::move(lines) }, { "lastSeq", lineSequence } };
    }, { .thread = Thread::Any });
}
