// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Trajectory recording to JSONL in the run directory, and playback of a recording as a camera
// flythrough or (best effort) as player input.
#include "events.h"
#include "methods.h"
#include "nav_common.h"
#include "screen.h"

#include <fstream>
#include <set>

namespace devtools::nav
{
    Json CameraSnapshot();
}

namespace
{
    using namespace devtools;
    using namespace devtools::nav;

    struct Recorder
    {
        std::string name;
        std::filesystem::path path;
        std::ofstream out;
        std::string buffer;
        double hz = 20.0;
        std::set<std::string> fields;
        double started = 0.0;
        double lastSample = -1e9;
        double lastFlush = 0.0;
        std::uint64_t firstFrame = 0;
        std::size_t samples = 0;
        double distance = 0.0;
        double maxSpeed = 0.0;
        std::optional<RE::NiPoint3> last;
        double lastT = 0.0;
        int stuckEvents = 0;
    };

    struct Keyframe
    {
        double t;
        RE::NiPoint3 pos;
        double yaw;    // radians
        double pitch;  // radians
        std::vector<std::string> input;
    };

    struct Replay
    {
        std::string kind;  // camera or input
        std::string name;
        std::string method;
        std::vector<Keyframe> frames;
        double speed = 1.0;
        double started = 0.0;
        std::size_t cursor = 0;
        bool startedFree = false;
        RE::NiPoint3 recordedEnd;
    };

    std::optional<Recorder> recorder;
    std::optional<Replay> replay;
    std::optional<Json> lastReplay;

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
        return name.ends_with(".jsonl") ? name.substr(0, name.size() - 6) : name;
    }

    void Flush(Recorder& r)
    {
        if (!r.buffer.empty()) {
            r.out << r.buffer;
            r.out.flush();
            r.buffer.clear();
        }
    }

    Json Sample(Recorder& r, Tick& tick, RE::PlayerCharacter& player)
    {
        const auto t = tick.now - r.started;
        Json s{ { "t", Round(t, 1000.0) }, { "frame", perf::FrameNumber() - r.firstFrame } };
        if (const auto calendar = RE::Calendar::GetSingleton()) {
            s["gameTime"] = Round(calendar->GetCurrentGameTime(), 1e6);
        }
        const auto pos = player.GetPosition();
        double speed = 0.0;
        if (r.last) {
            const auto dt = t - r.lastT;
            const auto d = Dist3D(pos, *r.last);
            if (d < 1500.0) {
                r.distance += d;
                speed = dt > 0 ? d / dt : 0.0;
                if (dt > 0) {
                    s["vel"] = Json::array({ Round((pos.x - r.last->x) / dt), Round((pos.y - r.last->y) / dt), Round((pos.z - r.last->z) / dt) });
                }
            } else {
                s["teleport"] = true;
            }
        }
        r.maxSpeed = std::max(r.maxSpeed, speed);
        r.last = pos;
        r.lastT = t;
        if (r.fields.contains("player")) {
            s["pos"] = Point(pos);
            s["rot"] = Json::array({ Round(Deg(player.GetAngleZ()), 100.0), Round(Deg(player.GetAngleX()), 100.0) });
            s["speed"] = Round(speed);
            const auto cell = player.GetParentCell();
            s["cell"] = cell ? Json(Hex(cell->GetFormID())) : Json(nullptr);
            s["world"] = DescribeWorld(player.GetWorldspace());
        }
        if (r.fields.contains("camera")) {
            try {
                const auto cam = CameraSnapshot();
                s["cam"] = { { "pos", cam.value("pos", Json(nullptr)) }, { "yaw", cam.value("yawDeg", Json(nullptr)) },
                    { "pitch", cam.value("pitchDeg", Json(nullptr)) }, { "mode", cam.value("mode", Json(nullptr)) },
                    { "fov", cam.value("worldFov", Json(nullptr)) } };
            } catch (const RpcError&) {
                s["cam"] = nullptr;
            }
        }
        if (r.fields.contains("state")) {
            const auto state = player.AsActorState();
            s["state"] = { { "running", player.IsRunning() }, { "sprinting", state && state->IsSprinting() },
                { "sneaking", player.IsSneaking() }, { "swimming", state && state->IsSwimming() },
                { "midair", player.IsInMidair() }, { "combat", player.IsInCombat() },
                { "driver", CurrentDrive().owner.empty() ? Json(nullptr) : Json(CurrentDrive().owner) } };
            const auto nav = NavSummary();
            if (!nav.is_null()) {
                s["nav"] = nav;
                if (nav.value("state", "") == "recovering") {
                    s["stuck"] = true;
                }
            }
        }
        if (r.fields.contains("input")) {
            s["input"] = input::DrivenControls();
        }
        if (r.fields.contains("perf")) {
            s["frameMs"] = Round(tick.dt * 1000.0, 100.0);
        }
        return s;
    }

    Json StopRecording()
    {
        auto& r = *recorder;
        Flush(r);
        r.out.close();
        Json out{ { "name", r.name }, { "file", r.path.filename().string() }, { "path", r.path.string() },
            { "samples", r.samples }, { "durationS", Round(r.lastT, 100.0) }, { "distance", Round(r.distance) },
            { "maxSpeed", Round(r.maxSpeed) }, { "hz", r.hz } };
        recorder.reset();
        if (events::Subscribed("recordStopped")) {
            events::Emit("recordStopped", out);
        }
        return out;
    }

    std::optional<double> Num(const Json& v)
    {
        return v.is_number() ? std::optional<double>(v.get<double>()) : std::nullopt;
    }

    std::vector<Keyframe> LoadFrames(const std::string& name, bool camera)
    {
        const auto path = RecordingsDir() / (SafeName(name) + ".jsonl");
        std::ifstream in(path);
        if (!in) {
            throw RpcError(code::notFound, "No recording " + name);
        }
        std::vector<Keyframe> frames;
        std::string line;
        while (std::getline(in, line)) {
            const auto s = Json::parse(line, nullptr, false);
            if (!s.is_object() || !s.contains("t")) {
                continue;
            }
            Keyframe k{ s["t"].get<double>(), {}, 0.0, 0.0, {} };
            const auto& src = camera ? s.value("cam", Json(nullptr)) : s;
            const auto& pos = src.is_object() ? src.value("pos", Json(nullptr)) : Json(nullptr);
            if (!pos.is_array() || pos.size() != 3) {
                continue;
            }
            k.pos = { pos[0].get<float>(), pos[1].get<float>(), pos[2].get<float>() };
            if (camera) {
                k.yaw = Rad(Num(src.value("yaw", Json(nullptr))).value_or(0.0));
                k.pitch = Rad(Num(src.value("pitch", Json(nullptr))).value_or(0.0));
            } else if (s.contains("rot") && s["rot"].is_array() && s["rot"].size() == 2) {
                k.yaw = Rad(s["rot"][0].get<double>());
                k.pitch = Rad(s["rot"][1].get<double>());
            }
            if (s.contains("input") && s["input"].is_array()) {
                for (const auto& c : s["input"]) {
                    k.input.push_back(c.get<std::string>());
                }
            }
            frames.push_back(std::move(k));
        }
        if (frames.size() < 2) {
            throw RpcError(code::invalidParams, camera ? "The recording has no camera samples (record with fields camera)"
                                                       : "The recording has no player samples");
        }
        return frames;
    }

    RE::FreeCameraState* FreeCamera()
    {
        const auto camera = RE::PlayerCamera::GetSingleton();
        if (!camera || !camera->currentState || camera->currentState->id != RE::CameraState::kFree) {
            return nullptr;
        }
        return static_cast<RE::FreeCameraState*>(camera->currentState.get());
    }

    Json ReplayStatus(const Replay& p, bool done)
    {
        const auto elapsed = Now() - p.started;
        return { { "kind", p.kind }, { "name", p.name }, { "method", p.method }, { "elapsedS", Round(elapsed, 100.0) },
            { "lengthS", Round(p.frames.back().t / p.speed, 100.0) }, { "frame", p.cursor }, { "frames", p.frames.size() },
            { "done", done } };
    }

    void EndReplay(bool finished)
    {
        auto& p = *replay;
        const auto camera = RE::PlayerCamera::GetSingleton();
        if (p.kind == "camera" && p.method == "free" && camera && camera->IsInFreeCameraMode() && !p.startedFree) {
            camera->ToggleFreeCameraMode(false);
        }
        auto summary = ReplayStatus(p, true);
        summary["finished"] = finished;
        if (p.kind == "input") {
            if (const auto player = RE::PlayerCharacter::GetSingleton()) {
                summary["endError"] = Round(Dist3D(player->GetPosition(), p.recordedEnd));
                summary["end"] = Point(player->GetPosition());
                summary["recordedEnd"] = Point(p.recordedEnd);
            }
        }
        if (CurrentDrive().owner == "replay") {
            CurrentDrive() = {};
        }
        lastReplay = summary;
        replay.reset();
        if (events::Subscribed("replayFinished")) {
            events::Emit("replayFinished", summary);
        }
    }

    void TickReplay(Tick& tick)
    {
        if (!replay) {
            return;
        }
        auto& p = *replay;
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return;
        }
        const auto t = (tick.now - p.started) * p.speed;
        while (p.cursor + 1 < p.frames.size() && p.frames[p.cursor + 1].t <= t) {
            ++p.cursor;
        }
        if (p.cursor + 1 >= p.frames.size()) {
            EndReplay(true);
            return;
        }
        const auto& a = p.frames[p.cursor];
        const auto& b = p.frames[p.cursor + 1];
        const auto span = b.t - a.t;
        const auto f = span > 0 ? std::clamp((t - a.t) / span, 0.0, 1.0) : 0.0;
        const RE::NiPoint3 pos{ static_cast<float>(a.pos.x + (b.pos.x - a.pos.x) * f), static_cast<float>(a.pos.y + (b.pos.y - a.pos.y) * f),
            static_cast<float>(a.pos.z + (b.pos.z - a.pos.z) * f) };
        const auto yaw = a.yaw + WrapPi(b.yaw - a.yaw) * f;
        const auto pitch = a.pitch + (b.pitch - a.pitch) * f;
        if (p.kind == "camera") {
            if (p.method == "free") {
                if (const auto free = FreeCamera()) {
                    free->translation = pos;
                    free->rotation.x = static_cast<float>(pitch);
                    free->rotation.y = static_cast<float>(yaw);
                }
            } else {
                // Moves the player itself along the camera path; the camera follows naturally.
                player->SetPosition(pos, true);
                FaceHeading(*player, yaw);
                FacePitch(*player, pitch);
            }
            return;
        }
        FaceHeading(*player, yaw);
        FacePitch(*player, pitch);
        for (const auto& control : a.input) {
            tick.drive.hold.push_back(control);
        }
    }
}

DEVTOOLS_MODULE(NavRecord)
{
    events::Declare("recordStopped", "A recording stopped: {name, file, samples, durationS, distance}");
    events::Declare("replayFinished", "Record.replayCamera/replayInput ended: {kind, name, finished, endError?}");

    AddTicker([](Tick& tick) {
        TickReplay(tick);
        if (!recorder) {
            return;
        }
        auto& r = *recorder;
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player || tick.now - r.lastSample < 1.0 / r.hz - 0.002) {
            return;
        }
        r.lastSample = tick.now;
        r.buffer += Sample(r, tick, *player).dump();
        r.buffer += '\n';
        ++r.samples;
        if (tick.now - r.lastFlush > 1.0) {
            r.lastFlush = tick.now;
            Flush(r);
        }
    });

    Register("Record.start", [](const Json& params) {
        AllowOnly(params, { "hz", "fields", "name" });
        std::lock_guard lock(Lock());
        if (recorder) {
            throw RpcError(code::busy, "Recording " + recorder->name + " is running; Record.stop it first");
        }
        Recorder r;
        r.hz = Optional<double>(params, "hz", 20.0);
        if (r.hz < 1.0 || r.hz > 60.0) {
            throw RpcError(code::invalidParams, "hz is 1 to 60");
        }
        static const std::set<std::string> known{ "player", "camera", "state", "input", "perf" };
        if (params.contains("fields")) {
            for (const auto& f : params["fields"]) {
                const auto name = f.get<std::string>();
                if (!known.contains(name)) {
                    throw RpcError(code::invalidParams, "fields are player, camera, state, input and perf");
                }
                r.fields.insert(name);
            }
        } else {
            r.fields = known;
        }
        r.fields.insert("player");
        const auto dir = RecordingsDir();
        std::filesystem::create_directories(dir);
        r.name = params.contains("name") ? SafeName(Require<std::string>(params, "name"))
                                         : std::format("rec-{}", std::chrono::system_clock::now().time_since_epoch().count() / 10000000);
        r.path = dir / (r.name + ".jsonl");
        r.out.open(r.path, std::ios::binary | std::ios::trunc);
        if (!r.out) {
            throw RpcError(code::notReady, "Cannot write " + r.path.string());
        }
        r.started = r.lastFlush = Now();
        r.firstFrame = perf::FrameNumber();
        recorder = std::move(r);
        return Json{ { "name", recorder->name }, { "file", recorder->path.filename().string() }, { "hz", recorder->hz },
            { "fields", recorder->fields } };
    }, { .thread = Thread::Main });

    Register("Record.stop", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        if (!recorder) {
            throw RpcError(code::notFound, "No recording is running");
        }
        return StopRecording();
    }, { .thread = Thread::Main });

    Register("Record.status", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        Json out{ { "recording", nullptr }, { "replay", nullptr }, { "lastReplay", lastReplay ? *lastReplay : Json(nullptr) } };
        if (recorder) {
            out["recording"] = { { "name", recorder->name }, { "samples", recorder->samples },
                { "durationS", Round(Now() - recorder->started, 10.0) }, { "distance", Round(recorder->distance) } };
        }
        if (replay) {
            out["replay"] = ReplayStatus(*replay, false);
        }
        return out;
    }, { .thread = Thread::Main });

    Register("Record.list", [](const Json& params) {
        AllowOnly(params, {});
        Json list = Json::array();
        const auto dir = RecordingsDir();
        std::error_code ec;
        for (const auto& entry : std::filesystem::directory_iterator(dir, ec)) {
            if (entry.path().extension() != ".jsonl") {
                continue;
            }
            list.push_back({ { "name", entry.path().stem().string() }, { "file", entry.path().filename().string() },
                { "bytes", entry.file_size() }, { "path", entry.path().string() } });
        }
        return Json{ { "dir", dir.string() }, { "recordings", list } };
    }, { .thread = Thread::Any });

    Register("Record.replayCamera", [](const Json& params) {
        AllowOnly(params, { "name", "speed", "method", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        if (replay) {
            throw RpcError(code::busy, "A replay is running");
        }
        Replay p;
        p.kind = "camera";
        p.name = SafeName(Require<std::string>(params, "name"));
        p.method = Optional<std::string>(params, "method", "free");
        if (p.method != "free" && p.method != "player") {
            throw RpcError(code::invalidParams, "method is free or player");
        }
        p.speed = Optional<double>(params, "speed", 1.0);
        if (p.speed <= 0.05 || p.speed > 20.0) {
            throw RpcError(code::invalidParams, "speed is 0.05 to 20");
        }
        p.frames = LoadFrames(p.name, p.method == "free");
        if (p.method == "free") {
            const auto camera = RE::PlayerCamera::GetSingleton();
            p.startedFree = camera->IsInFreeCameraMode();
            if (!p.startedFree) {
                camera->ToggleFreeCameraMode(false);
            }
        }
        p.started = Now();
        auto reply = ReplayStatus(p, false);
        replay = std::move(p);
        return reply;
    }, { .thread = Thread::Main });

    Register("Record.replayInput", [](const Json& params) {
        AllowOnly(params, { "name", "teleport", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        if (replay) {
            throw RpcError(code::busy, "A replay is running");
        }
        if (!CurrentDrive().owner.empty()) {
            throw RpcError(code::busy, "The player is being driven by " + CurrentDrive().owner);
        }
        Replay p;
        p.kind = "input";
        p.method = "input";
        p.name = SafeName(Require<std::string>(params, "name"));
        p.frames = LoadFrames(p.name, false);
        p.recordedEnd = p.frames.back().pos;
        auto& player = Player();
        if (Optional<bool>(params, "teleport", true)) {
            player.SetPosition(p.frames.front().pos, true);
            FaceHeading(player, p.frames.front().yaw);
        }
        p.started = Now();
        CurrentDrive().owner = "replay";
        auto reply = ReplayStatus(p, false);
        replay = std::move(p);
        return reply;
    }, { .thread = Thread::Main });

    Register("Record.stopReplay", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        if (!replay) {
            return Json{ { "stopped", false } };
        }
        EndReplay(false);
        return Json{ { "stopped", true }, { "last", *lastReplay } };
    }, { .thread = Thread::Main });
}
