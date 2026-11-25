// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Papyrus output taken from the script VM's own log event, log file tails and the plugin log.
#include "logs.h"

#include "events.h"
#include "game.h"
#include "methods.h"

#include <Windows.h>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <deque>
#include <fstream>
#include <mutex>
#include <regex>

namespace
{
    using namespace devtools;
    using RE::BSScript::ErrorLogger;

    enum Level : int
    {
        info = 0,
        warning = 1,
        error = 2,
        fatal = 3
    };
    constexpr const char* levelNames[] = { "info", "warning", "error", "fatal" };

    std::int64_t UnixMs()
    {
        using namespace std::chrono;
        return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
    }

    Level ParseLevel(const std::string& name)
    {
        for (int i = 0; i < 4; ++i) {
            if (name == levelNames[i]) {
                return static_cast<Level>(i);
            }
        }
        throw RpcError(code::invalidParams, "level is info, warning, error or fatal");
    }

    // The game writes localized text in the system code page; a reply must be valid UTF-8.
    std::string Scrub(std::string_view text)
    {
        std::string out;
        out.reserve(text.size());
        for (std::size_t i = 0; i < text.size();) {
            const auto c = static_cast<unsigned char>(text[i]);
            const std::size_t length = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 0;
            bool valid = length && i + length <= text.size();
            for (std::size_t k = 1; valid && k < length; ++k) {
                valid = (static_cast<unsigned char>(text[i + k]) & 0xC0) == 0x80;
            }
            if (valid) {
                out.append(text.substr(i, length));
                i += length;
            } else {
                out += "\xEF\xBF\xBD";
                ++i;
            }
        }
        return out;
    }

    struct Line
    {
        std::uint64_t seq;
        std::int64_t ms;
        Level level;
        std::string text;
    };

    // One bounded history shared by every line source.
    class History
    {
    public:
        explicit History(std::size_t limit) : limit_(limit) {}

        std::uint64_t Add(Level level, std::string text)
        {
            std::lock_guard lock(mutex_);
            const auto seq = ++seq_;
            ++counts_[level];
            lines_.push_back({ seq, UnixMs(), level, Scrub(text) });
            if (lines_.size() > limit_) {
                lines_.pop_front();
                ++dropped_;
            }
            return seq;
        }

        Json Query(std::uint64_t since, Level minimum, std::size_t limit, const std::string& contains)
        {
            std::lock_guard lock(mutex_);
            auto out = Json::array();
            for (const auto& line : lines_) {
                if (line.seq > since && line.level >= minimum &&
                    (contains.empty() || line.text.find(contains) != std::string::npos)) {
                    out.push_back({ { "seq", line.seq }, { "ms", line.ms }, { "severity", levelNames[line.level] },
                        { "text", line.text } });
                }
            }
            // Keep the newest lines when a limit applies.
            if (out.size() > limit) {
                out.erase(out.begin(), out.begin() + (out.size() - limit));
            }
            return { { "lines", std::move(out) }, { "lastSeq", seq_ }, { "dropped", dropped_ },
                { "counts", CountsLocked() } };
        }

        Json Counts()
        {
            std::lock_guard lock(mutex_);
            return CountsLocked();
        }

        std::vector<std::string> Tail(std::size_t count)
        {
            std::lock_guard lock(mutex_);
            std::vector<std::string> out;
            const auto first = lines_.size() > count ? lines_.size() - count : 0;
            for (auto i = first; i < lines_.size(); ++i) {
                out.push_back(std::string(levelNames[lines_[i].level]) + ": " + lines_[i].text);
            }
            return out;
        }

    private:
        Json CountsLocked() const
        {
            Json counts = Json::object();
            for (int i = 0; i < 4; ++i) {
                counts[levelNames[i]] = counts_[i];
            }
            return counts;
        }

        std::mutex mutex_;
        std::size_t limit_;
        std::deque<Line> lines_;
        std::uint64_t seq_ = 0;
        std::uint64_t dropped_ = 0;
        std::uint64_t counts_[4] = {};
    };

    History papyrus(5000);
    History plugin(500);

    std::mutex pluginFileMutex;

    std::filesystem::path RunDir()
    {
        wchar_t buffer[1024];
        const auto length = GetEnvironmentVariableW(L"SKYRIM_DEVTOOLS_RUN_DIR", buffer, 1024);
        return length > 0 && length < 1024 ? std::filesystem::path(std::wstring(buffer, length)) : std::filesystem::path{};
    }

    // The documents folder the game writes to; the host maps it onto the isolated profile.
    std::filesystem::path GameDocuments()
    {
        const auto skse = SKSE::log::log_directory();
        return skse ? skse->parent_path() : std::filesystem::path{};
    }

    std::filesystem::path NewestPapyrusLog()
    {
        const auto directory = GameDocuments() / "Logs" / "Script";
        std::error_code ignored;
        std::filesystem::path newest;
        std::filesystem::file_time_type newestTime{};
        for (const auto& entry : std::filesystem::directory_iterator(directory, ignored)) {
            const auto name = entry.path().filename().string();
            if (entry.is_regular_file(ignored) && name.starts_with("Papyrus.") && name.ends_with(".log")) {
                const auto time = entry.last_write_time(ignored);
                if (newest.empty() || time > newestTime) {
                    newest = entry.path();
                    newestTime = time;
                }
            }
        }
        return newest;
    }

    // The log text carries its own "error:" or "warning:" marker; the VM's severity can lag behind it.
    Level Classify(Level reported, const std::string& text)
    {
        static const std::regex marker(R"(^\s*(?:\[[^\]]*\]\s*)?(error|warning|fatal)\s*:)", std::regex::icase);
        std::smatch match;
        if (std::regex_search(text, match, marker)) {
            std::string word = match[1];
            std::ranges::transform(word, word.begin(), [](unsigned char c) { return std::tolower(c); });
            return std::max(reported, ParseLevel(word));
        }
        return reported;
    }

    Level FromSeverity(ErrorLogger::Severity severity)
    {
        switch (severity) {
        case ErrorLogger::Severity::kWarning:
            return warning;
        case ErrorLogger::Severity::kError:
            return error;
        case ErrorLogger::Severity::kFatal:
            return fatal;
        default:
            return info;
        }
    }

    class LogSink final : public RE::BSTEventSink<RE::BSScript::LogEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::BSScript::LogEvent* event,
            RE::BSTEventSource<RE::BSScript::LogEvent>*) override
        {
            if (event && event->errorMsg) {
                std::string text = event->errorMsg;
                while (!text.empty() && (text.back() == '\n' || text.back() == '\r')) {
                    text.pop_back();
                }
                const auto level = Classify(FromSeverity(event->severity), text);
                const auto seq = papyrus.Add(level, text);
                if (events::Subscribed("papyrus")) {
                    events::Emit("papyrus", { { "seq", seq }, { "severity", levelNames[level] }, { "text", text } });
                }
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };

    class StatsSink final : public RE::BSTEventSink<RE::BSScript::StatsEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::BSScript::StatsEvent* event,
            RE::BSTEventSource<RE::BSScript::StatsEvent>*) override
        {
            if (event) {
                running = event->runningStacksCount;
                suspended = event->suspendedStacksCount;
                functionMessages = event->functionMsgCount;
                detachedObjects = event->detachedObjectCount;
                updates = updates.load() + 1;
                lastMs = UnixMs();
            }
            return RE::BSEventNotifyControl::kContinue;
        }

        std::atomic<std::uint32_t> running = 0, suspended = 0, functionMessages = 0, detachedObjects = 0;
        std::atomic<std::uint64_t> updates = 0;
        std::atomic<std::int64_t> lastMs = 0;
    };

    LogSink logSink;
    StatsSink statsSink;
    std::mutex hookMutex;
    std::atomic_bool hooked = false;

    // The VM may not exist yet when the first listener runs, so every entry point retries.
    bool EnsureHooked()
    {
        if (hooked) {
            return true;
        }
        std::lock_guard lock(hookMutex);
        if (hooked) {
            return true;
        }
        const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
        if (!vm) {
            return false;
        }
        auto* iface = static_cast<RE::BSScript::IVirtualMachine*>(vm);
        iface->RegisterForLogEvent(&logSink);
        iface->RegisterForStatsEvent(&statsSink);
        hooked = true;
        SetCapability("papyrusLog", true);
        logs::Plugin("info", "Papyrus log and stats sinks registered");
        return true;
    }

    // Reads the last lines of a file the game may still be writing.
    Json TailFile(const std::filesystem::path& path, std::size_t count)
    {
        std::ifstream file(path, std::ios::binary);
        if (!file) {
            return nullptr;
        }
        file.seekg(0, std::ios::end);
        const std::uint64_t size = static_cast<std::uint64_t>(file.tellg());
        constexpr std::uint64_t window = 512 * 1024;
        const auto start = size > window ? size - window : 0;
        file.seekg(static_cast<std::streamoff>(start));
        std::string data(static_cast<std::size_t>(size - start), '\0');
        file.read(data.data(), static_cast<std::streamsize>(data.size()));
        data.resize(static_cast<std::size_t>(file.gcount()));

        std::vector<std::string> lines;
        std::size_t position = start ? data.find('\n') + 1 : 0;  // the first line of a window is partial
        if (start && position == 0) {
            position = data.size();
        }
        while (position < data.size()) {
            auto end = data.find('\n', position);
            if (end == std::string::npos) {
                end = data.size();
            }
            auto line = data.substr(position, end - position);
            if (!line.empty() && line.back() == '\r') {
                line.pop_back();
            }
            lines.push_back(Scrub(line));
            position = end + 1;
        }
        const bool truncated = lines.size() > count || start > 0;
        if (lines.size() > count) {
            lines.erase(lines.begin(), lines.end() - count);
        }
        return { { "lines", std::move(lines) }, { "bytes", size }, { "truncated", truncated } };
    }
}

namespace devtools::logs
{
    void Plugin(std::string_view level, std::string_view message)
    {
        const auto lvl = level == "error" ? error : level == "warning" ? warning : info;
        plugin.Add(lvl, std::string(message));
        const auto directory = RunDir();
        if (directory.empty()) {
            return;
        }
        std::lock_guard lock(pluginFileMutex);
        std::ofstream file(directory / "devtools.log", std::ios::app);
        file << UnixMs() << ' ' << level << ' ' << message << '\n';
    }
}

DEVTOOLS_MODULE(Logs)
{
    events::Declare("papyrus", "A Papyrus VM log line: {seq, severity: info|warning|error|fatal, text}");
    SetCapability("papyrusLog", false);
    SetCapability("logs", true);
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        switch (message.type) {
        case SKSE::MessagingInterface::kPostLoad:
        case SKSE::MessagingInterface::kPostPostLoad:
        case SKSE::MessagingInterface::kInputLoaded:
        case SKSE::MessagingInterface::kDataLoaded:
        case SKSE::MessagingInterface::kPostLoadGame:
        case SKSE::MessagingInterface::kNewGame:
            EnsureHooked();
            break;
        default:
            break;
        }
    });
    logs::Plugin("info", "Plugin started");

    Register("Papyrus.log", [](const Json& params) {
        AllowOnly(params, { "since", "level", "limit", "contains" });
        const auto level = ParseLevel(Optional<std::string>(params, "level", "info"));
        // The registration itself happens on game threads; this handler only reads the history.
        auto result = papyrus.Query(Optional<std::uint64_t>(params, "since", 0), level,
            Optional<std::size_t>(params, "limit", 500), Optional<std::string>(params, "contains", ""));
        result["hooked"] = hooked.load();
        return result;
    }, { .thread = Thread::Any });

    // Reads VM state, so it runs on the main thread where the script VM is stable.
    Register("Papyrus.stats", [](const Json& params) {
        AllowOnly(params, {});
        const bool ready = EnsureHooked();
        Json out{ { "hooked", ready }, { "logCounts", papyrus.Counts() } };
        const auto updates = statsSink.updates.load();
        if (updates) {
            out["runningStacks"] = statsSink.running.load();
            out["suspendedStacks"] = statsSink.suspended.load();
            out["functionMessages"] = statsSink.functionMessages.load();
            out["detachedObjects"] = statsSink.detachedObjects.load();
            out["statsAgeMs"] = UnixMs() - statsSink.lastMs.load();
        }
        out["statsUpdates"] = updates;
        if (const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton()) {
            // The live table is authoritative; the stats event only refreshes while the VM publishes it.
            RE::BSSpinLockGuard lock(vm->runningStacksLock);
            out["activeStacks"] = vm->allRunningStacks.size();
            out["overstressed"] = vm->overstressed;
        }
        return out;
    }, { .thread = Thread::Main });

    Register("Logs.tail", [](const Json& params) {
        AllowOnly(params, { "source", "lines" });
        const auto source = Optional<std::string>(params, "source", "papyrus");
        const auto count = Optional<std::size_t>(params, "lines", 100);
        if (count == 0 || count > 5000) {
            throw RpcError(code::invalidParams, "lines is between 1 and 5000");
        }
        std::filesystem::path path;
        if (source == "papyrus") {
            path = NewestPapyrusLog();
        } else if (source == "skse") {
            path = GameDocuments() / "SKSE" / "skse64.log";
        } else if (source == "plugin") {
            path = RunDir() / "devtools.log";
        } else if (source == "rpc") {
            path = RunDir() / "rpc.jsonl";
        } else {
            throw RpcError(code::invalidParams, "source is papyrus, skse, plugin or rpc");
        }
        Json out = path.empty() ? Json(nullptr) : TailFile(path, count);
        if (out.is_null()) {
            // The Papyrus file appears only once the game opens it; the VM history is the fallback.
            if (source == "papyrus") {
                return Json{ { "source", source }, { "origin", "memory" }, { "lines", papyrus.Tail(count) } };
            }
            if (source == "plugin") {
                return Json{ { "source", source }, { "origin", "memory" }, { "lines", plugin.Tail(count) } };
            }
            throw RpcError(code::notFound, "No " + source + " log exists yet");
        }
        out["source"] = source;
        out["origin"] = "file";
        out["name"] = path.filename().string();
        return out;
    }, { .thread = Thread::Any });
}
