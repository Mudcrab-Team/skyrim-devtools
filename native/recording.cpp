// SPDX-License-Identifier: GPL-3.0-or-later
// Record and replay of a play-through: player pose and camera at a fixed interval, cell transitions, console
// commands and the entry point, written to a portable JSON file (docs/record-replay.md). A replay puts the
// scene back first (save or coc, a settle delay, time and weather when the entry is young enough) and then
// walks the player along the recorded poses on the recording's own clock, so two runs follow the same path.
// This is separate from Record.* (nav_record.cpp), which logs JSONL for diagnostics and has no scene.
#include "console.h"
#include "events.h"
#include "methods.h"
#include "nav_common.h"

#include <algorithm>
#include <fstream>

namespace devtools::nav
{
    Json CameraSnapshot();
}

namespace
{
    using namespace devtools;
    using namespace devtools::nav;

    constexpr int formatVersion = 1;
    constexpr const char* formatName = "skyrim-devtools-recording";
    constexpr std::size_t maxSamples = 60000;
    constexpr double maxRecordSeconds = 1800.0;
    // A restore step that has not finished after this long is a failure, not a slow load.
    constexpr double restoreTimeoutS = 120.0;
    constexpr double transitMinS = 0.6;

    struct Config
    {
        int recordHotkey = 0;  // Windows virtual-key code; 0 disables
        bool recordHotkeyShift = false;
        int replayHotkey = 0;
        bool replayHotkeyShift = false;
        std::string replayPath;
        bool replayRestoreScene = true;
        int recordIntervalMs = 50;
        std::string autoRunPath;
        bool autoRunRestoreScene = true;
        int loadSettleMs = 3000;
        int couplingAnchorMs = 10000;
        int couplingCellMs = 60000;
        bool cleanTransition = true;
        std::string cleanTransitionCell = "QASmoke";
    } cfg;

    // How the game was put into the recorded scene, tracked from loads and coc/cow so a recording knows its entry
    // point and how long before recording began it was set up.
    struct Entry
    {
        std::string kind = "unknown";  // save, coc or unknown
        std::string save;
        std::string coc;  // the console arguments as typed
        double at = 0.0;
    } entry;

    std::atomic_bool loadDone = true;
    std::atomic_bool loadOk = true;
    std::atomic_bool autoRunPending = false;
    bool autoRunConsumed = false;
    double lastEntryCommandAt = -1e9;
    std::atomic_bool postLoadSeen = false;

    struct Sample
    {
        double t;  // ms on the recording clock
        float x, y, z;
        float yaw, pitch;  // degrees
        std::string pov;
        float cx, cy, cz, cyaw, cpitch;
    };

    struct Event
    {
        double t;
        Json data;  // carries "type"
    };

    struct Recorder
    {
        std::string name;
        std::filesystem::path path;
        double started = 0.0;
        double lastSample = -1e9;
        int intervalMs = 50;
        std::vector<Sample> samples;
        std::vector<Event> events;
        Json entry;
        Json meta;
        RE::FormID lastCell = 0;
        double distance = 0.0;
        std::optional<RE::NiPoint3> last;
        bool limitReached = false;
    };

    enum class Phase { Restore, Settle, Run, Transit };

    struct Replay
    {
        std::string name;
        std::vector<Sample> samples;
        std::vector<Event> events;
        std::vector<int> segment;
        Json doc;
        std::string tier;
        bool restoreScene = true;
        int settleMs = 3000;
        bool cleanTransition = true;
        std::string cleanCell;
        bool force = false;
        int stepEveryMs = 1000;
        bool replayCommands = true;
        bool applyPov = true;
        bool autorun = false;
        std::uint64_t id = 0;
        std::vector<std::string> beforeRun;
        Phase phase = Phase::Restore;
        std::string step = "start";
        double phaseStart = 0.0;
        double stepStart = 0.0;
        double started = 0.0;
        double paused = 0.0;
        double transitStart = 0.0;
        double lastStepEvent = -1e9;
        std::size_t cursor = 0;
        std::size_t nextEvent = 0;
        int currentSegment = 0;
        std::string targetCellEdid;
        RE::FormID targetCell = 0;
        std::string targetWorld;
        std::optional<RE::NiPoint3> lastApplied;
        bool justSnapped = true;
        std::string lastPov;
        std::vector<double> drift;
        Json observed = Json::array();
        double lastObserved = -1e9;
        std::size_t commandsRun = 0;
        Json commandErrors = Json::array();
        Json warnings = Json::array();
        Json restore = Json::object();
        int cellMismatches = 0;
    };

    std::optional<Recorder> recorder;
    std::optional<Replay> replay;
    std::optional<Json> lastReplay;
    Json lastTrace = nullptr;
    std::string lastRecordingPath;
    std::uint64_t replayCounter = 0;

    // ---- helpers -----------------------------------------------------------------------------------

    std::filesystem::path RecordingsDir()
    {
        const auto run = RunDir();
        if (run.empty()) {
            throw RpcError(code::notReady, "The plugin was started without a run directory");
        }
        return run / "recordings";
    }

    std::string SafeName(const std::string& name)
    {
        if (name.empty() || name.size() > 80 || name.find("..") != std::string::npos ||
            name.find_first_of("/\\:*?\"<>|") != std::string::npos) {
            throw RpcError(code::invalidParams, "name is a plain file name");
        }
        return name.ends_with(".json") ? name.substr(0, name.size() - 5) : name;
    }

    std::string Lower(std::string text)
    {
        std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }

    std::string Trim(std::string_view text)
    {
        const auto first = text.find_first_not_of(" \t");
        if (first == std::string_view::npos) {
            return {};
        }
        const auto last = text.find_last_not_of(" \t");
        return std::string(text.substr(first, last - first + 1));
    }

    Json CellJson(RE::TESObjectCELL* cell)
    {
        if (!cell) {
            return nullptr;
        }
        Json out{ { "formId", Hex(cell->GetFormID()) }, { "interior", cell->IsInteriorCell() } };
        if (const auto id = cell->GetFormEditorID(); id && *id) {
            out["editorId"] = id;
        }
        if (const auto name = cell->GetName(); name && *name) {
            out["name"] = name;
        }
        if (!cell->IsInteriorCell()) {
            if (const auto coords = cell->GetCoordinates()) {
                out["coords"] = Json::array({ coords->cellX, coords->cellY });
            }
        }
        return out;
    }

    Json WorldName(RE::PlayerCharacter& player)
    {
        return DescribeWorld(player.GetWorldspace());
    }

    double Median(std::vector<double> values)
    {
        if (values.empty()) {
            return 0.0;
        }
        std::sort(values.begin(), values.end());
        return values[values.size() / 2];
    }

    double Percentile(std::vector<double> values, double p)
    {
        if (values.empty()) {
            return 0.0;
        }
        std::sort(values.begin(), values.end());
        return values[std::min(values.size() - 1, static_cast<std::size_t>(p * static_cast<double>(values.size())))];
    }

    void Hud(const std::string& text)
    {
        RE::SendHUDMessage::ShowHUDMessage(text.c_str());
    }

    // ---- configuration -----------------------------------------------------------------------------

    template <class T>
    void Take(const Json& in, const char* key, T& out, T lo, T hi)
    {
        if (!in.contains(key) || in[key].is_null()) {
            return;
        }
        try {
            const auto value = in[key].get<T>();
            if (value < lo || value > hi) {
                throw RpcError(code::invalidParams, std::string(key) + " is out of range");
            }
            out = value;
        } catch (const Json::exception&) {
            throw RpcError(code::invalidParams, std::string("Parameter '") + key + "' has the wrong type");
        }
    }

    template <class T>
    void TakeAny(const Json& in, const char* key, T& out)
    {
        if (!in.contains(key) || in[key].is_null()) {
            return;
        }
        try {
            out = in[key].get<T>();
        } catch (const Json::exception&) {
            throw RpcError(code::invalidParams, std::string("Parameter '") + key + "' has the wrong type");
        }
    }

    void ApplyConfig(const Json& in)
    {
        Take(in, "recordHotkey", cfg.recordHotkey, 0, 255);
        TakeAny(in, "recordHotkeyShift", cfg.recordHotkeyShift);
        Take(in, "replayHotkey", cfg.replayHotkey, 0, 255);
        TakeAny(in, "replayHotkeyShift", cfg.replayHotkeyShift);
        TakeAny(in, "replayPath", cfg.replayPath);
        TakeAny(in, "replayRestoreScene", cfg.replayRestoreScene);
        Take(in, "recordIntervalMs", cfg.recordIntervalMs, 10, 1800000);
        TakeAny(in, "autoRunPath", cfg.autoRunPath);
        TakeAny(in, "autoRunRestoreScene", cfg.autoRunRestoreScene);
        Take(in, "loadSettleMs", cfg.loadSettleMs, 0, 120000);
        Take(in, "couplingAnchorMs", cfg.couplingAnchorMs, 0, 86400000);
        Take(in, "couplingCellMs", cfg.couplingCellMs, 0, 86400000);
        TakeAny(in, "cleanTransition", cfg.cleanTransition);
        TakeAny(in, "cleanTransitionCell", cfg.cleanTransitionCell);
    }

    Json ConfigJson()
    {
        return { { "recordHotkey", cfg.recordHotkey }, { "recordHotkeyShift", cfg.recordHotkeyShift },
            { "replayHotkey", cfg.replayHotkey }, { "replayHotkeyShift", cfg.replayHotkeyShift },
            { "replayPath", cfg.replayPath }, { "replayRestoreScene", cfg.replayRestoreScene },
            { "recordIntervalMs", cfg.recordIntervalMs }, { "autoRunPath", cfg.autoRunPath },
            { "autoRunRestoreScene", cfg.autoRunRestoreScene }, { "loadSettleMs", cfg.loadSettleMs },
            { "couplingAnchorMs", cfg.couplingAnchorMs }, { "couplingCellMs", cfg.couplingCellMs },
            { "cleanTransition", cfg.cleanTransition }, { "cleanTransitionCell", cfg.cleanTransitionCell } };
    }

    void LoadConfigFile()
    {
        const auto run = RunDir();
        if (run.empty()) {
            return;
        }
        std::ifstream in(run / "recording.json");
        if (!in) {
            return;
        }
        const auto parsed = Json::parse(in, nullptr, false);
        if (!parsed.is_object()) {
            return;
        }
        try {
            ApplyConfig(parsed);
        } catch (const RpcError&) {
            // a bad value leaves the defaults for the keys after it; Recording.configure reports the problem
        }
    }

    // ---- scene coupling ----------------------------------------------------------------------------

    // anchored: the entry was set up moments before recording began, so time and weather are part of the scene;
    // cell: restore the entry only; worldspace: do not restore, only check where the player is.
    std::string CouplingTier(const Json& doc)
    {
        const auto& meta = doc.contains("meta") ? doc["meta"] : Json::object();
        const auto& over = meta.contains("coupling") && meta["coupling"].is_object() ? meta["coupling"] : Json::object();
        const auto& ent = doc.contains("entry") ? doc["entry"] : Json::object();
        if (over.contains("tier") && over["tier"].is_string()) {
            return over["tier"].get<std::string>();
        }
        if (!ent.is_object() || ent.value("kind", "unknown") == "unknown" || !ent.contains("ageMs") || !ent["ageMs"].is_number()) {
            return "worldspace";
        }
        const auto age = ent["ageMs"].get<double>();
        const auto anchor = over.value("anchorMs", cfg.couplingAnchorMs);
        const auto cellMs = over.value("cellMs", cfg.couplingCellMs);
        return age <= anchor ? "anchored" : age <= cellMs ? "cell" : "worldspace";
    }

    // ---- file format -------------------------------------------------------------------------------

    Json SampleRow(const Sample& s)
    {
        return Json::array({ std::round(s.t), Round(s.x, 100.0), Round(s.y, 100.0), Round(s.z, 100.0), Round(s.yaw, 100.0),
            Round(s.pitch, 100.0), s.pov, Round(s.cx, 10.0), Round(s.cy, 10.0), Round(s.cz, 10.0), Round(s.cyaw, 100.0),
            Round(s.cpitch, 100.0) });
    }

    // One sample per line keeps a recording diffable and hand-editable.
    void WriteFile(const std::filesystem::path& path, const Json& head, const std::vector<Sample>& samples, const Json& events)
    {
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        if (!out) {
            throw RpcError(code::notReady, "Cannot write " + path.string());
        }
        auto text = head.dump();
        text.pop_back();  // the closing brace: samples and events follow
        out << text << ",\"events\":" << events.dump() << ",\"samples\":[\n";
        for (std::size_t i = 0; i < samples.size(); ++i) {
            out << SampleRow(samples[i]).dump() << (i + 1 < samples.size() ? ",\n" : "\n");
        }
        out << "]}\n";
        if (!out) {
            throw RpcError(code::notReady, "Writing " + path.string() + " failed");
        }
    }

    Json ReadDoc(const std::filesystem::path& path)
    {
        std::ifstream in(path, std::ios::binary);
        if (!in) {
            throw RpcError(code::notFound, "No recording at " + path.string());
        }
        auto doc = Json::parse(in, nullptr, false);
        if (!doc.is_object() || doc.value("format", "") != formatName) {
            throw RpcError(code::invalidParams, path.filename().string() + " is not a " + formatName + " file");
        }
        if (doc.value("version", 0) != formatVersion) {
            throw RpcError(code::invalidParams, "Unsupported recording version " + doc.value("version", Json(nullptr)).dump());
        }
        return doc;
    }

    std::filesystem::path ResolvePath(const std::string& given)
    {
        std::filesystem::path path = given;
        if (!path.has_parent_path()) {
            path = RecordingsDir() / (SafeName(given) + ".json");
        }
        return path;
    }

    std::filesystem::path NewestRecording()
    {
        std::filesystem::path best;
        std::filesystem::file_time_type bestTime{};
        std::error_code ec;
        for (const auto& item : std::filesystem::directory_iterator(RecordingsDir(), ec)) {
            if (item.path().extension() == ".json" && (best.empty() || item.last_write_time() > bestTime)) {
                best = item.path();
                bestTime = item.last_write_time();
            }
        }
        return best;
    }

    void ParseDoc(Replay& r)
    {
        const auto& rows = r.doc.contains("samples") ? r.doc["samples"] : Json::array();
        if (!rows.is_array() || rows.size() < 2) {
            throw RpcError(code::invalidParams, "The recording has fewer than two samples");
        }
        for (const auto& row : rows) {
            if (!row.is_array() || row.size() < 6) {
                throw RpcError(code::invalidParams, "A sample row has fewer than 6 columns");
            }
            Sample s{};
            s.t = row[0].get<double>();
            s.x = row[1].get<float>();
            s.y = row[2].get<float>();
            s.z = row[3].get<float>();
            s.yaw = row[4].get<float>();
            s.pitch = row[5].get<float>();
            s.pov = row.size() > 6 && row[6].is_string() ? row[6].get<std::string>() : "";
            r.samples.push_back(std::move(s));
        }
        if (r.doc.contains("events") && r.doc["events"].is_array()) {
            for (const auto& e : r.doc["events"]) {
                if (e.is_object() && e.contains("t") && e["t"].is_number() && e.contains("type")) {
                    r.events.push_back({ e["t"].get<double>(), e });
                }
            }
        }
        std::stable_sort(r.events.begin(), r.events.end(), [](const Event& a, const Event& b) { return a.t < b.t; });
        int segment = 0;
        std::size_t next = 0;
        for (const auto& s : r.samples) {
            while (next < r.events.size() && r.events[next].t <= s.t) {
                if (r.events[next].data.value("type", "") == "cell") {
                    ++segment;
                }
                ++next;
            }
            r.segment.push_back(segment);
        }
    }

    // ---- recording ---------------------------------------------------------------------------------

    Json EntryJson(RE::PlayerCharacter& player, double now)
    {
        Json out{ { "kind", entry.kind } };
        std::string save = entry.save;
        if (entry.kind == "save" && save.empty()) {
            save = LastLoadedSave();
        }
        if (entry.kind == "save" && !save.empty()) {
            out["save"] = save;
        }
        if (entry.kind == "coc") {
            out["coc"] = entry.coc;
        }
        if (entry.kind != "unknown") {
            out["ageMs"] = std::round((now - entry.at) * 1000.0);
        }
        out["cell"] = CellJson(player.GetParentCell());
        out["worldspace"] = WorldName(player);
        out["pos"] = Point(player.GetPosition());
        out["rot"] = Json::array({ Round(Deg(player.GetAngleZ()), 100.0), Round(Deg(player.GetAngleX()), 100.0) });
        if (const auto calendar = RE::Calendar::GetSingleton()) {
            out["gameHour"] = Round(calendar->GetHour(), 1000.0);
            out["gameTime"] = Round(calendar->GetCurrentGameTime(), 1e6);
        }
        if (const auto sky = RE::Sky::GetSingleton(); sky && sky->currentWeather) {
            out["weather"] = Hex(sky->currentWeather->GetFormID());
        }
        return out;
    }

    void CaptureSample(Recorder& r, double now, RE::PlayerCharacter& player)
    {
        const auto t = (now - r.started) * 1000.0;
        const auto pos = player.GetPosition();
        Sample s{};
        s.t = t;
        s.x = pos.x;
        s.y = pos.y;
        s.z = pos.z;
        s.yaw = static_cast<float>(Deg(player.GetAngleZ()));
        s.pitch = static_cast<float>(Deg(player.GetAngleX()));
        try {
            const auto cam = CameraSnapshot();
            s.pov = cam.value("mode", std::string("unknown"));
            if (cam.contains("pos") && cam["pos"].is_array() && cam["pos"].size() == 3) {
                s.cx = cam["pos"][0].get<float>();
                s.cy = cam["pos"][1].get<float>();
                s.cz = cam["pos"][2].get<float>();
            }
            s.cyaw = cam.value("yawDeg", 0.0F);
            s.cpitch = cam.value("pitchDeg", 0.0F);
        } catch (const std::exception&) {
            s.pov = "unknown";
        }
        if (r.last) {
            const auto d = Dist3D(pos, *r.last);
            if (d < 1500.0) {
                r.distance += d;
            }
        }
        r.last = pos;
        r.samples.push_back(std::move(s));
    }

    void NoteCellChange(Recorder& r, double now, RE::PlayerCharacter& player)
    {
        const auto cell = player.GetParentCell();
        const auto id = cell ? cell->GetFormID() : 0;
        if (id == r.lastCell) {
            return;
        }
        const auto previous = r.lastCell;
        r.lastCell = id;
        if (!previous || !id) {
            return;
        }
        // A guess from what was seen shortly before: a coc/cow command, a load, else a door or script move.
        std::string via = "door";
        if (now - lastEntryCommandAt < 8.0) {
            via = "coc";
        } else if (postLoadSeen.exchange(false)) {
            via = "load";
        }
        Json e{ { "type", "cell" }, { "t", std::round((now - r.started) * 1000.0) }, { "via", via },
            { "from", Hex(previous) }, { "to", CellJson(cell) }, { "world", WorldName(player) },
            { "pos", Point(player.GetPosition()) } };
        r.events.push_back({ (now - r.started) * 1000.0, e });
    }

    Json StatusJson(const Recorder& r, double now)
    {
        return { { "name", r.name }, { "samples", r.samples.size() }, { "events", r.events.size() },
            { "durationMs", std::round((now - r.started) * 1000.0) }, { "distance", Round(r.distance) },
            { "intervalMs", r.intervalMs } };
    }

    Json StopRecording()
    {
        auto& r = *recorder;
        const auto now = Now();
        const auto durationMs = std::round((now - r.started) * 1000.0);
        Json meta = r.meta;
        meta["durationMs"] = durationMs;
        meta["sampleCount"] = r.samples.size();
        meta["distance"] = Round(r.distance);
        Json head{ { "format", formatName }, { "version", formatVersion }, { "meta", meta }, { "entry", r.entry },
            { "sampleColumns", Json::array({ "tMs", "x", "y", "z", "yawDeg", "pitchDeg", "pov", "camX", "camY", "camZ", "camYawDeg", "camPitchDeg" }) } };
        Json events = Json::array();
        for (const auto& e : r.events) {
            events.push_back(e.data);
        }
        std::filesystem::create_directories(r.path.parent_path());
        WriteFile(r.path, head, r.samples, events);
        Json out{ { "name", r.name }, { "path", r.path.string() }, { "samples", r.samples.size() }, { "events", r.events.size() },
            { "durationMs", durationMs }, { "distance", Round(r.distance) }, { "limitReached", r.limitReached } };
        lastRecordingPath = r.path.string();
        recorder.reset();
        events::Emit("record.stopped", out);
        return out;
    }

    Json StartRecording(const Json& params)
    {
        if (recorder) {
            throw RpcError(code::busy, "Recording " + recorder->name + " is running; Recording.stop it first");
        }
        if (replay) {
            throw RpcError(code::busy, "A replay is running");
        }
        auto& player = Player();
        Recorder r;
        r.intervalMs = Optional<int>(params, "intervalMs", cfg.recordIntervalMs);
        if (r.intervalMs < 10 || r.intervalMs > 1800000) {
            throw RpcError(code::invalidParams, "intervalMs is 10 to 1800000");
        }
        const auto now = Now();
        r.name = params.contains("name") ? SafeName(Require<std::string>(params, "name"))
                                         : std::format("recording-{}", std::chrono::system_clock::now().time_since_epoch().count() / 10000000);
        r.path = RecordingsDir() / (r.name + ".json");
        r.started = now;
        r.entry = EntryJson(player, now);
        r.meta = { { "name", r.name }, { "intervalMs", r.intervalMs }, { "runtime", "se" },
            { "pluginBuild", GetConfig().build }, { "notes", Optional<std::string>(params, "notes", "") } };
        if (params.contains("coupling")) {
            r.meta["coupling"] = params["coupling"];
        }
        if (const auto cell = player.GetParentCell()) {
            r.lastCell = cell->GetFormID();
        }
        recorder = std::move(r);
        CaptureSample(*recorder, now, player);
        recorder->lastSample = now;
        Json out = StatusJson(*recorder, now);
        out["entry"] = recorder->entry;
        events::Emit("record.started", out);
        return out;
    }

    void OnCommand(std::string_view text, bool typed)
    {
        std::lock_guard lock(Lock());
        const auto command = Trim(text);
        if (command.empty()) {
            return;
        }
        const auto lower = Lower(command);
        const auto word = lower.substr(0, lower.find(' '));
        const auto now = Now();
        if (word == "coc" || word == "centeroncell" || word == "cow") {
            if (!replay) {
                entry = { "coc", "", Trim(command.substr(std::min(command.size(), word.size()))), now };
                if (word == "cow") {
                    entry.coc = "cow " + entry.coc;
                }
            }
            lastEntryCommandAt = now;
        }
        if (recorder) {
            recorder->events.push_back({ (now - recorder->started) * 1000.0,
                { { "type", "command" }, { "t", std::round((now - recorder->started) * 1000.0) }, { "command", command },
                    { "source", typed ? "typed" : "api" } } });
        }
    }

    // ---- replay ------------------------------------------------------------------------------------

    Json ReplayStatus(const Replay& r, bool done)
    {
        const char* phase = r.phase == Phase::Restore ? "restore" : r.phase == Phase::Settle ? "settle" : r.phase == Phase::Run ? "run" : "transit";
        return { { "id", r.id }, { "name", r.name }, { "tier", r.tier }, { "phase", done ? "done" : phase }, { "step", r.step },
            { "cursor", r.cursor }, { "samples", r.samples.size() }, { "done", done } };
    }

    Json DriftJson(const std::vector<double>& drift)
    {
        return { { "count", drift.size() }, { "medianUnits", Round(Median(drift), 100.0) },
            { "p95Units", Round(Percentile(drift, 0.95), 100.0) },
            { "maxUnits", Round(drift.empty() ? 0.0 : *std::max_element(drift.begin(), drift.end()), 100.0) } };
    }

    void EndReplay(bool finished, const std::string& error = {})
    {
        auto& r = *replay;
        Json summary{ { "id", r.id }, { "name", r.name }, { "finished", finished }, { "tier", r.tier }, { "restore", r.restore },
            { "samples", r.samples.size() }, { "cursor", r.cursor }, { "drift", DriftJson(r.drift) },
            { "cellMismatches", r.cellMismatches }, { "commandsRun", r.commandsRun }, { "commandErrors", r.commandErrors },
            { "warnings", r.warnings }, { "autorun", r.autorun } };
        if (r.started > 0.0) {
            summary["durationMs"] = std::round((Now() - r.started - r.paused) * 1000.0);
        }
        if (!error.empty()) {
            summary["error"] = error;
        }
        lastTrace = { { "name", r.name }, { "columns", Json::array({ "tMs", "x", "y", "z", "cell" }) }, { "samples", r.observed } };
        lastReplay = summary;
        if (CurrentDrive().owner == "recording-replay") {
            CurrentDrive() = {};
        }
        replay.reset();
        events::Emit("replay.finished", summary);
        Hud(finished ? "Replay finished" : "Replay stopped");
    }

    void Fail(const std::string& error)
    {
        EndReplay(false, error);
    }

    bool WorldPlayable()
    {
        auto& ui = RequireUI();
        return Playable(ui);
    }

    std::string CellEditorId(RE::PlayerCharacter& player)
    {
        const auto cell = player.GetParentCell();
        const auto id = cell ? cell->GetFormEditorID() : nullptr;
        return id ? id : "";
    }

    bool InTargetCell(Replay& r, RE::PlayerCharacter& player)
    {
        const auto cell = player.GetParentCell();
        if (!cell) {
            return false;
        }
        if (r.targetCell) {
            return cell->GetFormID() == r.targetCell;
        }
        if (!r.targetCellEdid.empty()) {
            return Lower(CellEditorId(player)) == Lower(r.targetCellEdid);
        }
        return true;
    }

    // Console moves for a recorded cell change: coc by editor id for an interior, cow with grid coordinates outside.
    bool IssueMove(Replay& r, const Json& cell, const Json& world)
    {
        r.targetCell = 0;
        r.targetCellEdid.clear();
        r.targetWorld.clear();
        try {
            if (cell.is_object() && cell.contains("formId")) {
                r.targetCell = ParseFormId(cell["formId"], "cell");
            }
            const auto interior = cell.is_object() && cell.value("interior", false);
            if (cell.is_object() && cell.contains("editorId") && interior) {
                r.targetCellEdid = cell["editorId"].get<std::string>();
                console::Run("coc " + r.targetCellEdid, nullptr);
                return true;
            }
            if (cell.is_object() && cell.contains("coords") && world.is_string()) {
                const auto& c = cell["coords"];
                r.targetWorld = world.get<std::string>();
                console::Run(std::format("cow {} {} {}", r.targetWorld, c[0].get<int>(), c[1].get<int>()), nullptr);
                return true;
            }
        } catch (const std::exception& error) {
            r.warnings.push_back(std::string("move failed: ") + error.what());
        }
        r.warnings.push_back("a recorded cell change could not be reproduced: the cell has no editor id or coordinates");
        return false;
    }

    void ApplyTimeAndWeather(Replay& r)
    {
        const auto& ent = r.doc.contains("entry") ? r.doc["entry"] : Json::object();
        Json applied = Json::object();
        if (ent.is_object() && ent.contains("gameHour") && ent["gameHour"].is_number()) {
            try {
                console::Run(std::format("set GameHour to {}", ent["gameHour"].get<double>()), nullptr);
                applied["gameHour"] = ent["gameHour"];
            } catch (const std::exception& error) {
                r.warnings.push_back(std::string("time not restored: ") + error.what());
            }
        }
        if (ent.is_object() && ent.contains("weather") && ent["weather"].is_string()) {
            const auto sky = RE::Sky::GetSingleton();
            const auto weather = RE::TESForm::LookupByID<RE::TESWeather>(ParseFormId(ent["weather"], "weather"));
            if (sky && weather) {
                sky->ForceWeather(weather, true);
                applied["weather"] = ent["weather"];
            } else {
                r.warnings.push_back("weather not restored: the weather form is not in this load order");
            }
        }
        r.restore["anchoredApplied"] = applied;
    }

    // Where the player must be before the trajectory starts; a mismatch aborts unless forced. An interior must be the
    // same cell; outside only the worldspace is compared, because a few seconds of walking between the entry and the
    // first sample can cross a cell border.
    bool AssertScene(Replay& r, RE::PlayerCharacter& player)
    {
        const auto& ent = r.doc.contains("entry") ? r.doc["entry"] : Json::object();
        if (!ent.is_object() || !ent.contains("cell") || !ent["cell"].is_object()) {
            return true;
        }
        const auto& cell = ent["cell"];
        const auto here = player.GetParentCell();
        std::string problem;
        if (cell.value("interior", false)) {
            if (!here || cell.value("formId", "") != Hex(here->GetFormID())) {
                problem = "the recording starts in an interior cell the player is not in";
            }
        } else if (WorldName(player) != ent.value("worldspace", Json(nullptr)) || (here && here->IsInteriorCell())) {
            problem = "the player is not in the recorded worldspace";
        }
        if (problem.empty()) {
            return true;
        }
        r.restore["sceneMismatch"] = { { "problem", problem }, { "recorded", cell }, { "actual", CellJson(here) } };
        if (r.force) {
            r.warnings.push_back(problem + " (forced)");
            return true;
        }
        Fail("sceneMismatch: " + problem);
        return false;
    }

    void BeginRun(Replay& r, RE::PlayerCharacter& player, double now)
    {
        if (!AssertScene(r, player)) {
            return;
        }
        // Run before the clock starts so a toggle is in place from the first pose, and after any load that would reset it.
        for (const auto& command : r.beforeRun) {
            try {
                console::Run(command, nullptr);
            } catch (const std::exception& error) {
                r.commandErrors.push_back({ { "command", command }, { "error", error.what() } });
            }
        }
        r.phase = Phase::Run;
        r.started = Now();
        r.paused = 0.0;
        r.cursor = 0;
        r.nextEvent = 0;
        r.currentSegment = r.segment.empty() ? 0 : r.segment.front();
        r.justSnapped = true;
        r.lastApplied.reset();
        CurrentDrive().owner = "recording-replay";
        events::Emit("replay.started", { { "name", r.name }, { "tier", r.tier }, { "samples", r.samples.size() },
            { "durationMs", r.samples.back().t }, { "autorun", r.autorun }, { "restore", r.restore } });
        Hud("Replay started");
    }

    void TickRestore(Replay& r, RE::PlayerCharacter& player, double now)
    {
        const auto& ent = r.doc.contains("entry") ? r.doc["entry"] : Json::object();
        const auto kind = ent.is_object() ? ent.value("kind", "unknown") : std::string("unknown");
        if (now - r.phaseStart > restoreTimeoutS) {
            Fail("restoreTimeout: step " + r.step + " did not finish in " + std::to_string(static_cast<int>(restoreTimeoutS)) + " s");
            return;
        }
        auto begin = [&](const char* step) {
            r.step = step;
            r.stepStart = now;
        };
        if (r.step == "start") {
            r.restore["tier"] = r.tier;
            r.restore["entryKind"] = kind;
            const bool restore = r.restoreScene && r.tier != "worldspace" && kind != "unknown";
            r.restore["restored"] = restore;
            if (!restore) {
                BeginRun(r, player, now);
                return;
            }
            if (kind == "save") {
                const auto save = ent.value("save", "");
                const auto& dir = GetConfig().savesDir;
                if (save.empty() || (!dir.empty() && !std::filesystem::is_regular_file(dir / (save + ".ess")))) {
                    Fail("noSave: the entry save '" + save + "' is not in the Saves directory");
                    return;
                }
                const auto manager = RE::BGSSaveLoadManager::GetSingleton();
                if (!manager) {
                    Fail("The save manager is unavailable");
                    return;
                }
                NotePendingSave(save);
                ++state::epoch;
                loadDone = false;
                manager->Load(save.c_str());
                r.restore["save"] = save;
                begin("waitLoad");
                return;
            }
            const auto target = ent.value("coc", "");
            if (target.empty()) {
                Fail("noCoc: the entry has no coc target");
                return;
            }
            if (r.cleanTransition && !r.cleanCell.empty() && Lower(CellEditorId(player)) != Lower(r.cleanCell)) {
                try {
                    console::Run("coc " + r.cleanCell, nullptr);
                } catch (const std::exception& error) {
                    r.warnings.push_back(std::string("clean transition skipped: ") + error.what());
                    begin("coc");
                    return;
                }
                r.targetCell = 0;
                r.targetCellEdid = r.cleanCell;
                r.restore["cleanTransition"] = r.cleanCell;
                begin("waitBounce");
                return;
            }
            begin("coc");
            return;
        }
        if (r.step == "waitLoad") {
            if (!loadDone) {
                return;
            }
            if (!loadOk) {
                Fail("loadFailed: the entry save failed to load");
                return;
            }
            if (now - r.stepStart < 0.5 || !WorldPlayable()) {
                return;
            }
            r.phase = Phase::Settle;
            r.phaseStart = now;
            return;
        }
        if (r.step == "waitBounce") {
            const auto timedOut = now - r.stepStart > 45.0;
            if ((now - r.stepStart > 1.0 && InTargetCell(r, player) && WorldPlayable()) || timedOut) {
                if (timedOut) {
                    r.warnings.push_back("the clean-transition cell did not load; went on to the entry directly");
                }
                begin("coc");
            }
            return;
        }
        if (r.step == "coc") {
            const auto target = ent.value("coc", "");
            try {
                console::Run(target.starts_with("cow ") ? target : "coc " + target, nullptr);
            } catch (const std::exception& error) {
                Fail(std::string("cocFailed: ") + error.what());
                return;
            }
            r.targetCell = 0;
            r.targetCellEdid.clear();
            r.restore["coc"] = target;
            begin("waitCoc");
            return;
        }
        if (r.step == "waitCoc") {
            // coc names no cell id the recording can check, so wait for any change away from the previous cell plus a load-free HUD.
            if (now - r.stepStart > 2.0 && WorldPlayable()) {
                r.phase = Phase::Settle;
                r.phaseStart = now;
            }
            return;
        }
    }

    void TickTransit(Replay& r, RE::PlayerCharacter& player, double now)
    {
        if (now - r.transitStart > restoreTimeoutS) {
            Fail("transitTimeout: the recorded cell change did not complete");
            return;
        }
        if (now - r.transitStart < transitMinS || !WorldPlayable() || !InTargetCell(r, player)) {
            return;
        }
        r.paused += now - r.transitStart;
        // Resume at the first sample after the change; the recorded poses before it belong to the old cell.
        const auto next = r.currentSegment + 1;
        std::size_t index = r.cursor;
        while (index < r.samples.size() && r.segment[index] < next) {
            ++index;
        }
        r.cursor = std::min(index, r.samples.size() - 1);
        r.currentSegment = r.segment[r.cursor];
        r.justSnapped = true;
        r.lastApplied.reset();
        r.phase = Phase::Run;
    }

    void ApplyPose(Replay& r, RE::PlayerCharacter& player, double tMs, double now)
    {
        const auto& a = r.samples[r.cursor];
        const Sample* b = r.cursor + 1 < r.samples.size() && r.segment[r.cursor + 1] == r.segment[r.cursor] ? &r.samples[r.cursor + 1] : nullptr;
        double f = 0.0;
        if (b && b->t > a.t) {
            f = std::clamp((tMs - a.t) / (b->t - a.t), 0.0, 1.0);
        }
        const auto lerp = [&](float u, float v) { return static_cast<float>(u + (v - u) * f); };
        const RE::NiPoint3 pos{ b ? lerp(a.x, b->x) : a.x, b ? lerp(a.y, b->y) : a.y, b ? lerp(a.z, b->z) : a.z };
        const auto yaw = Rad(a.yaw) + (b ? WrapPi(Rad(b->yaw) - Rad(a.yaw)) * f : 0.0);
        const auto pitch = Rad(a.pitch) + (b ? (Rad(b->pitch) - Rad(a.pitch)) * f : 0.0);

        const auto actual = player.GetPosition();
        if (r.lastApplied && !r.justSnapped) {
            r.drift.push_back(Dist3D(actual, *r.lastApplied));
        }
        if (tMs - r.lastObserved >= 20.0) {
            r.lastObserved = tMs;
            if (r.observed.size() < maxSamples) {
                const auto cell = player.GetParentCell();
                r.observed.push_back(Json::array({ std::round(tMs), Round(actual.x, 100.0), Round(actual.y, 100.0),
                    Round(actual.z, 100.0), cell ? Hex(cell->GetFormID()) : std::string() }));
            }
        }
        player.SetPosition(pos, true);
        FaceHeading(player, yaw);
        FacePitch(player, pitch);
        r.lastApplied = pos;
        r.justSnapped = false;
        if (r.applyPov && !a.pov.empty() && a.pov != r.lastPov) {
            r.lastPov = a.pov;
            if (const auto camera = RE::PlayerCamera::GetSingleton()) {
                if (a.pov == "firstPerson" && !camera->IsInFirstPerson()) {
                    camera->ForceFirstPerson();
                } else if (a.pov == "thirdPerson" && !camera->IsInThirdPerson()) {
                    camera->ForceThirdPerson();
                }
            }
        }
        if (static_cast<int>(tMs - r.lastStepEvent) >= r.stepEveryMs) {
            r.lastStepEvent = tMs;
            events::Emit("replay.step", { { "tMs", std::round(tMs) }, { "index", r.cursor }, { "of", r.samples.size() },
                { "pos", Point(pos) } });
        }
    }

    bool ReplayableCommand(const std::string& command)
    {
        const auto word = Lower(command.substr(0, command.find(' ')));
        // Cell changes are reproduced from the cell events; running the command too would load twice.
        return word != "coc" && word != "centeroncell" && word != "cow";
    }

    void TickRun(Replay& r, RE::PlayerCharacter& player, double now)
    {
        const auto tMs = (now - r.started - r.paused) * 1000.0;
        while (r.nextEvent < r.events.size() && r.events[r.nextEvent].t <= tMs) {
            const auto& e = r.events[r.nextEvent].data;
            ++r.nextEvent;
            const auto type = e.value("type", "");
            if (type == "command" && r.replayCommands) {
                const auto command = e.value("command", "");
                if (ReplayableCommand(command)) {
                    try {
                        console::Run(command, nullptr);
                        ++r.commandsRun;
                    } catch (const std::exception& error) {
                        r.commandErrors.push_back({ { "command", command }, { "error", error.what() } });
                    }
                }
            } else if (type == "cell") {
                if (IssueMove(r, e.value("to", Json(nullptr)), e.value("world", Json(nullptr)))) {
                    r.phase = Phase::Transit;
                    r.transitStart = now;
                    return;
                }
                ++r.cellMismatches;
                // Without a move the poses after this event are in another cell; skip to the next recorded cell change.
                std::size_t skip = r.cursor;
                const auto from = r.currentSegment;
                while (skip < r.samples.size() && r.segment[skip] <= from) {
                    ++skip;
                }
                r.cursor = std::min(skip, r.samples.size() - 1);
                r.currentSegment = r.segment[r.cursor];
                r.justSnapped = true;
            } else if (type == "load") {
                r.warnings.push_back("a mid-recording load was not reproduced");
            }
        }
        while (r.cursor + 1 < r.samples.size() && r.samples[r.cursor + 1].t <= tMs && r.segment[r.cursor + 1] == r.segment[r.cursor]) {
            ++r.cursor;
        }
        if (r.cursor + 1 >= r.samples.size() && tMs >= r.samples.back().t) {
            EndReplay(true);
            return;
        }
        ApplyPose(r, player, tMs, now);
    }

    void TickReplay(Tick&)
    {
        if (!replay) {
            return;
        }
        auto& r = *replay;
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return;
        }
        const auto now = Now();
        switch (r.phase) {
        case Phase::Restore:
            TickRestore(r, *player, now);
            break;
        case Phase::Settle:
            if (now - r.phaseStart >= r.settleMs / 1000.0 && WorldPlayable()) {
                if (r.tier == "anchored") {
                    ApplyTimeAndWeather(r);
                }
                BeginRun(r, *player, now);
            }
            break;
        case Phase::Transit:
            TickTransit(r, *player, now);
            break;
        case Phase::Run:
            TickRun(r, *player, now);
            break;
        }
    }

    Json StartReplay(const Json& params, bool autorun)
    {
        if (recorder) {
            throw RpcError(code::busy, "Recording " + recorder->name + " is running");
        }
        if (replay) {
            throw RpcError(code::busy, "A replay is running");
        }
        if (!CurrentDrive().owner.empty()) {
            throw RpcError(code::busy, "The player is being driven by " + CurrentDrive().owner);
        }
        std::filesystem::path path;
        if (params.contains("path") && params["path"].is_string()) {
            path = ResolvePath(params["path"].get<std::string>());
        } else if (params.contains("name") && params["name"].is_string()) {
            path = ResolvePath(params["name"].get<std::string>());
        } else if (!cfg.replayPath.empty()) {
            path = ResolvePath(cfg.replayPath);
        } else {
            path = NewestRecording();
        }
        if (path.empty()) {
            throw RpcError(code::notFound, "No recording to replay");
        }
        Replay r;
        r.doc = ReadDoc(path);
        ParseDoc(r);
        r.name = r.doc["meta"].value("name", path.stem().string());
        r.tier = CouplingTier(r.doc);
        if (params.contains("coupling") && params["coupling"].is_string()) {
            r.tier = params["coupling"].get<std::string>();
        }
        if (r.tier != "anchored" && r.tier != "cell" && r.tier != "worldspace") {
            throw RpcError(code::invalidParams, "coupling is anchored, cell or worldspace");
        }
        r.restoreScene = Optional<bool>(params, "restoreScene", autorun ? cfg.autoRunRestoreScene : cfg.replayRestoreScene);
        r.settleMs = Optional<int>(params, "settleMs", cfg.loadSettleMs);
        r.cleanTransition = Optional<bool>(params, "cleanTransition", cfg.cleanTransition);
        r.cleanCell = Optional<std::string>(params, "cleanTransitionCell", cfg.cleanTransitionCell);
        r.force = Optional<bool>(params, "force", false);
        r.stepEveryMs = Optional<int>(params, "stepEveryMs", 1000);
        r.replayCommands = Optional<bool>(params, "replayCommands", true);
        r.applyPov = Optional<bool>(params, "applyPov", true);
        r.autorun = autorun;
        r.id = ++replayCounter;
        if (params.contains("beforeRun")) {
            if (!params["beforeRun"].is_array() || params["beforeRun"].size() > 50) {
                throw RpcError(code::invalidParams, "beforeRun is a list of up to 50 console commands");
            }
            for (const auto& command : params["beforeRun"]) {
                if (!command.is_string()) {
                    throw RpcError(code::invalidParams, "beforeRun holds strings");
                }
                r.beforeRun.push_back(command.get<std::string>());
            }
        }
        if (r.settleMs < 0 || r.settleMs > 120000 || r.stepEveryMs < 50) {
            throw RpcError(code::invalidParams, "settleMs is 0 to 120000 and stepEveryMs at least 50");
        }
        if (!r.restoreScene || r.tier == "worldspace") {
            RequirePlayable(params);
        }
        r.phaseStart = Now();
        r.restore["path"] = path.string();
        Json out = ReplayStatus(r, false);
        out["durationMs"] = r.samples.back().t;
        replay = std::move(r);
        // The run clock only starts after the restore, so a replay that restores announces itself at replay.started.
        return out;
    }

    // ---- hotkeys -----------------------------------------------------------------------------------

    bool KeyDown(int vk)
    {
        return vk > 0 && (GetAsyncKeyState(vk) & 0x8000) != 0;
    }

    void PollHotkeys(double now)
    {
        static bool recordHeld = false;
        static bool replayHeld = false;
        if (!GameIsForeground()) {
            return;
        }
        const auto ui = RE::UI::GetSingleton();
        if (ui && ui->IsMenuOpen("Console")) {
            return;
        }
        const bool shift = KeyDown(VK_SHIFT);
        const bool record = KeyDown(cfg.recordHotkey) && shift == cfg.recordHotkeyShift;
        const bool play = KeyDown(cfg.replayHotkey) && shift == cfg.replayHotkeyShift;
        try {
            if (record && !recordHeld) {
                if (recorder) {
                    const auto out = StopRecording();
                    Hud("Recording stopped: " + out.value("name", ""));
                } else {
                    StartRecording(Json::object());
                    Hud("Recording started");
                }
            }
            if (play && !replayHeld) {
                if (replay) {
                    EndReplay(false);
                } else {
                    StartReplay(Json::object(), false);
                }
            }
        } catch (const std::exception& error) {
            Hud(std::string("Recording: ") + error.what());
        }
        recordHeld = record;
        replayHeld = play;
        (void)now;
    }
}

DEVTOOLS_MODULE(Recording)
{
    events::Declare("record.started", "A recording began: {name, samples, intervalMs, entry}");
    events::Declare("record.stopped", "A recording was written: {name, path, samples, events, durationMs, distance, limitReached}");
    events::Declare("replay.started", "The run clock of a replay started, after its scene was restored: {name, tier, samples, durationMs, autorun, restore}");
    events::Declare("replay.step", "Replay progress about every stepEveryMs: {tMs, index, of, pos}");
    events::Declare("replay.finished", "A replay ended: {name, finished, tier, drift, cellMismatches, commandsRun, durationMs, error?}");

    LoadConfigFile();
    console::SetCommandObserver(OnCommand);

    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kPreLoadGame) {
            loadDone = false;
            return;
        }
        if (message.type == SKSE::MessagingInterface::kNewGame) {
            std::lock_guard lock(Lock());
            entry = { "unknown", "", "", Now() };
            return;
        }
        if (message.type != SKSE::MessagingInterface::kPostLoadGame) {
            return;
        }
        const bool success = message.data != nullptr;
        loadOk = success;
        loadDone = true;
        postLoadSeen = true;
        std::lock_guard lock(Lock());
        if (success) {
            entry = { "save", "", "", Now() };
            if (recorder) {
                recorder->events.push_back({ (Now() - recorder->started) * 1000.0,
                    { { "type", "load" }, { "t", std::round((Now() - recorder->started) * 1000.0) } } });
            }
            if (!autoRunConsumed && !cfg.autoRunPath.empty() && !replay && !recorder) {
                autoRunConsumed = true;
                autoRunPending = true;
            }
        }
    });

    AddTicker([](Tick& tick) {
        if (autoRunPending.exchange(false)) {
            try {
                Json params = Json::object();
                params["path"] = cfg.autoRunPath;
                // A save that was just loaded is already the entry the recording wants: loading it again would only
                // repeat the wait.
                const auto doc = ReadDoc(ResolvePath(cfg.autoRunPath));
                const auto save = doc.contains("entry") ? doc["entry"].value("save", "") : std::string();
                if (!save.empty() && save == LastLoadedSave()) {
                    params["restoreScene"] = false;
                }
                StartReplay(params, true);
            } catch (const std::exception& error) {
                Hud(std::string("Autorun: ") + error.what());
            }
        }
        TickReplay(tick);
        const auto now = tick.now;
        PollHotkeys(now);
        if (!recorder) {
            return;
        }
        auto& r = *recorder;
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return;
        }
        NoteCellChange(r, now, *player);
        if (now - r.lastSample < r.intervalMs / 1000.0 - 0.002) {
            return;
        }
        r.lastSample = now;
        CaptureSample(r, now, *player);
        if (r.samples.size() >= maxSamples || now - r.started >= maxRecordSeconds) {
            r.limitReached = true;
            try {
                StopRecording();
            } catch (const std::exception& error) {
                recorder.reset();
                Hud(std::string("Recording lost: ") + error.what());
            }
        }
    });

    const MethodSpec main{ .thread = Thread::Main };

    Register("Recording.start", [](const Json& params) {
        AllowOnly(params, { "name", "intervalMs", "notes", "coupling", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        return StartRecording(params);
    }, main);

    Register("Recording.stop", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        if (!recorder) {
            throw RpcError(code::notFound, "No recording is running");
        }
        return StopRecording();
    }, main);

    Register("Recording.status", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        Json out{ { "recording", nullptr }, { "replay", nullptr }, { "lastReplay", lastReplay ? *lastReplay : Json(nullptr) },
            { "lastRecording", lastRecordingPath.empty() ? Json(nullptr) : Json(lastRecordingPath) } };
        if (recorder) {
            out["recording"] = StatusJson(*recorder, Now());
        }
        if (replay) {
            out["replay"] = ReplayStatus(*replay, false);
        }
        return out;
    }, main);

    Register("Recording.list", [](const Json& params) {
        AllowOnly(params, {});
        Json list = Json::array();
        const auto dir = RecordingsDir();
        std::error_code ec;
        for (const auto& item : std::filesystem::directory_iterator(dir, ec)) {
            if (item.path().extension() != ".json") {
                continue;
            }
            Json row{ { "name", item.path().stem().string() }, { "path", item.path().string() }, { "bytes", item.file_size() } };
            if (const auto doc = [&] { std::ifstream in(item.path(), std::ios::binary); return Json::parse(in, nullptr, false); }();
                doc.is_object() && doc.value("format", "") == formatName) {
                const auto& meta = doc.value("meta", Json::object());
                row["durationMs"] = meta.value("durationMs", Json(nullptr));
                row["entryKind"] = doc.contains("entry") && doc["entry"].is_object() ? doc["entry"].value("kind", Json(nullptr)) : Json(nullptr);
                row["tier"] = CouplingTier(doc);
            }
            list.push_back(std::move(row));
        }
        return Json{ { "dir", dir.string() }, { "recordings", list } };
    }, { .thread = Thread::Any });

    Register("Recording.replay", [](const Json& params) {
        AllowOnly(params, { "path", "name", "restoreScene", "settleMs", "coupling", "cleanTransition", "cleanTransitionCell",
            "force", "stepEveryMs", "replayCommands", "applyPov", "beforeRun", "loadEpoch" });
        std::lock_guard lock(Lock());
        return StartReplay(params, false);
    }, main);

    Register("Recording.stopReplay", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        if (!replay) {
            return Json{ { "stopped", false } };
        }
        EndReplay(false);
        return Json{ { "stopped", true }, { "last", *lastReplay } };
    }, main);

    Register("Recording.replayTrace", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        if (lastTrace.is_null()) {
            throw RpcError(code::notFound, "No replay has finished yet");
        }
        return lastTrace;
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(15) });

    Register("Recording.read", [](const Json& params) {
        AllowOnly(params, { "path", "name" });
        std::string given;
        TakeAny(params, "path", given);
        TakeAny(params, "name", given);
        // Only the recordings directory is readable this way; a full path would let any caller read any JSON file.
        const auto path = given.empty() ? NewestRecording()
                                        : RecordingsDir() / (SafeName(std::filesystem::path(given).stem().string()) + ".json");
        if (path.empty()) {
            throw RpcError(code::notFound, "No recording to read");
        }
        return ReadDoc(path);
    }, { .thread = Thread::Any, .timeout = std::chrono::seconds(20) });

    Register("Recording.configure", [](const Json& params) {
        AllowOnly(params, { "recordHotkey", "recordHotkeyShift", "replayHotkey", "replayHotkeyShift", "replayPath", "replayRestoreScene",
            "recordIntervalMs", "autoRunPath", "autoRunRestoreScene", "loadSettleMs", "couplingAnchorMs", "couplingCellMs",
            "cleanTransition", "cleanTransitionCell" });
        std::lock_guard lock(Lock());
        ApplyConfig(params);
        return ConfigJson();
    }, main);

    SetCapability("recording", input::Hooked());
}
