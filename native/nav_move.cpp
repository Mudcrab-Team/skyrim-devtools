// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// The per-poll movement driver plus Move.* and Camera.*. Movement holds ordinary controls
// (Forward, Strafe Left, Sprint ...) through the input module, so the game moves the player
// with its own animation, collision and speed; only facing is written directly.
#include "events.h"
#include "methods.h"
#include "nav_common.h"
#include "speed.h"
#include "speed_math.h"

#include <Windows.h>

#include <algorithm>
#include <array>
#include <vector>

namespace devtools::nav
{
    namespace
    {
        std::vector<Ticker>& Tickers()
        {
            static std::vector<Ticker> tickers;
            return tickers;
        }
    }

    std::recursive_mutex& Lock()
    {
        static std::recursive_mutex lock;
        return lock;
    }

    void AddTicker(Ticker ticker)
    {
        std::lock_guard lock(Lock());
        Tickers().push_back(std::move(ticker));
    }

    double Now()
    {
        return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
    }

    RE::ThirdPersonState* ThirdPerson()
    {
        const auto camera = RE::PlayerCamera::GetSingleton();
        if (!camera || !camera->currentState || camera->currentState->id != RE::CameraState::kThirdPerson) {
            return nullptr;
        }
        return static_cast<RE::ThirdPersonState*>(camera->currentState.get());
    }

    void FaceHeading(RE::PlayerCharacter& player, double headingRad)
    {
        player.SetHeading(static_cast<float>(WrapPi(headingRad)));
        if (const auto third = ThirdPerson()) {
            // A third-person camera orbiting freely would make Forward walk along the camera,
            // not the new heading.
            third->freeRotation.x = 0.0F;
        }
    }

    void FacePitch(RE::PlayerCharacter& player, double pitchRad)
    {
        const auto limit = Rad(85.0);
        player.SetLooking(static_cast<float>(std::clamp(pitchRad, -limit, limit)));
        if (const auto third = ThirdPerson()) {
            third->freeRotation.y = 0.0F;
        }
    }

    DriveState& CurrentDrive()
    {
        static DriveState state;
        return state;
    }

    std::filesystem::path RunDir()
    {
        std::array<wchar_t, 4096> buffer{};
        const auto size = GetEnvironmentVariableW(L"SKYRIM_DEVTOOLS_RUN_DIR", buffer.data(), static_cast<DWORD>(buffer.size()));
        return size && size < buffer.size() ? std::filesystem::path(std::wstring(buffer.data(), size)) : std::filesystem::path{};
    }
}

namespace
{
    using namespace devtools;
    using namespace devtools::nav;
    using namespace std::chrono_literals;

    struct Walk
    {
        std::string control;
        bool sprint = false;
        std::optional<bool> restoreRunning;
        double started = 0.0;
        double duration = 0.0;  // seconds; 0 when the walk ends by distance
        double distance = 0.0;  // game units; 0 when the walk ends by time
        RE::NiPoint3 start;
        RE::NiPoint3 last;
        double travelled = 0.0;
        devtools::speed::math::SpeedSamples samples;
        stuck::Detector det;
        ReplyPtr reply;
    };

    struct Turn
    {
        double target = 0.0;  // radians
        double rate = 0.0;    // radians per second
        ReplyPtr reply;
    };

    struct Sneak
    {
        bool on = false;
        int polls = 0;
        bool tapped = false;
        ReplyPtr reply;
    };

    std::optional<Walk> walk;
    std::optional<Turn> turn;
    std::optional<Sneak> sneak;
    int jumpPolls = 0;

    bool* Running()
    {
        const auto controls = RE::PlayerControls::GetSingleton();
        return controls ? &controls->data.running : nullptr;
    }

    Json HeadingJson(RE::PlayerCharacter& player)
    {
        return { { "headingDeg", Round(Deg(player.GetAngleZ()), 100.0) }, { "pitchDeg", Round(Deg(player.GetAngleX()), 100.0) } };
    }

    // A walk that ended because the player stopped getting anywhere: the reply is the `stuck` error.
    void StopStuck(RE::PlayerCharacter& player, const stuck::Verdict& verdict)
    {
        if (walk->restoreRunning) {
            if (const auto running = Running()) {
                *running = *walk->restoreRunning;
            }
        }
        const auto pos = player.GetPosition();
        auto data = stuck::Report(verdict, player, nullptr);
        data["direction"] = walk->control;
        data["distance"] = Round(walk->travelled);
        data["displacement"] = Round(Dist2D(walk->start, pos));
        data["durationMs"] = static_cast<std::int64_t>((Now() - walk->started) * 1000.0);
        data["start"] = Point(walk->start);
        walk->reply->Fail(code::stuck, "stuck", std::move(data));
        walk.reset();
        if (CurrentDrive().owner == "walk") {
            CurrentDrive().owner.clear();
        }
    }

    void FinishWalk(RE::PlayerCharacter& player, bool timedOut)
    {
        if (walk->restoreRunning) {
            if (const auto running = Running()) {
                *running = *walk->restoreRunning;
            }
        }
        Json result{ { "direction", walk->control }, { "distance", Round(walk->travelled) },
            { "displacement", Round(Dist2D(walk->start, player.GetPosition())) },
            { "durationMs", static_cast<std::int64_t>((Now() - walk->started) * 1000.0) },
            { "start", Point(walk->start) }, { "end", Point(player.GetPosition()) }, { "stoppedByLimit", timedOut } };
        // The first moments are acceleration and the engine adopting a new walk speed; the median after them is
        // the speed the movement code delivers, which a single distance over a short walk (obstacles, ramp) hides.
        if (walk->samples.Count() >= 8) {
            const double steady = walk->samples.Steady(0.9);
            result["speed"] = { { "steadyUnitsPerSecond", Round(steady, 1) }, { "peakUnitsPerSecond", Round(walk->samples.Peak(), 1) },
                { "walkPercent", Round(devtools::speed::AppliedPercent()) }, { "samples", walk->samples.Count() } };
            devtools::speed::Observe(steady);
        }
        walk->reply->Ok(std::move(result));
        walk.reset();
        if (CurrentDrive().owner == "walk") {
            CurrentDrive().owner.clear();
        }
    }

    void TickMove(Tick& tick)
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return;
        }
        if (walk) {
            if (walk->reply->Done()) {
                FinishWalk(*player, true);
            } else {
                const auto pos = player->GetPosition();
                const auto step = Dist3D(pos, walk->last);
                walk->travelled += step < 1000.0 ? step : 0.0;  // a teleport is not walking
                if (step < 1000.0) {
                    walk->samples.Add(tick.now - walk->started, tick.dt, Dist2D(pos, walk->last));
                }
                walk->last = pos;
                if (step >= 1000.0) {
                    walk->det.Reset(tick.now);
                }
                const auto elapsed = tick.now - walk->started;
                const bool done = (walk->duration > 0.0 && elapsed >= walk->duration) ||
                                  (walk->distance > 0.0 && walk->travelled >= walk->distance);
                if (done || elapsed > 120.0) {
                    FinishWalk(*player, !done);
                } else if (stuck::Paused()) {
                    walk->det.Reset(tick.now);
                    tick.drive.hold.push_back(walk->control);
                } else if (walk->det.Recovering(tick.now)) {
                    const auto controls = walk->det.RecoveryControls(tick.now);
                    tick.drive.hold.insert(tick.drive.hold.end(), controls.hold.begin(), controls.hold.end());
                    if (controls.tapJump) {
                        tick.drive.tap.push_back("Jump");
                    }
                } else {
                    const auto verdict = walk->det.Update(tick.now, pos.x, pos.y, -1.0, &stuck::Doors());
                    if (verdict.unstuck) {
                        stuck::EmitUnstuck("walk", verdict, pos);
                    }
                    if (verdict.step == stuck::Step::abort) {
                        stuck::EmitStuck("walk", verdict, stuck::Report(verdict, *player, nullptr));
                        StopStuck(*player, verdict);
                    } else {
                        if (verdict.step != stuck::Step::none) {
                            stuck::EmitStuck("walk", verdict, stuck::Report(verdict, *player, nullptr));
                            if (verdict.step == stuck::Step::replan) {
                                // Nothing to replan on a fixed heading: back off the other way instead.
                                walk->det.BeginRecovery(tick.now, stuck::Step::back, 1.0);
                            }
                        }
                        if (walk->det.Recovering(tick.now)) {
                            const auto controls = walk->det.RecoveryControls(tick.now);
                            tick.drive.hold.insert(tick.drive.hold.end(), controls.hold.begin(), controls.hold.end());
                            if (controls.tapJump) {
                                tick.drive.tap.push_back("Jump");
                            }
                        } else {
                            tick.drive.hold.push_back(walk->control);
                            if (walk->sprint) {
                                tick.drive.hold.push_back("Sprint");
                            }
                        }
                    }
                }
            }
        }
        if (turn) {
            const auto current = static_cast<double>(player->GetAngleZ());
            const auto error = WrapPi(turn->target - current);
            const auto step = turn->rate * std::max(0.001, static_cast<double>(tick.dt));
            if (std::abs(error) <= step || turn->reply->Done()) {
                FaceHeading(*player, turn->target);
                turn->reply->Ok(HeadingJson(*player));
                turn.reset();
            } else {
                FaceHeading(*player, current + std::copysign(step, error));
            }
        }
        if (jumpPolls > 0) {
            if (jumpPolls-- == 3) {
                tick.drive.tap.push_back("Jump");
            }
        }
        if (sneak) {
            ++sneak->polls;
            const bool sneaking = player->IsSneaking();
            if (!sneak->tapped && sneaking != sneak->on) {
                tick.drive.tap.push_back("Sneak");
                sneak->tapped = true;
            } else if (sneaking == sneak->on || sneak->polls > 60 || sneak->reply->Done()) {
                sneak->reply->Ok({ { "sneaking", sneaking }, { "changed", sneak->tapped } });
                sneak.reset();
            }
        }
    }

    std::string DirectionControl(const std::string& direction)
    {
        if (direction == "forward") return "Forward";
        if (direction == "back") return "Back";
        if (direction == "left") return "Strafe Left";
        if (direction == "right") return "Strafe Right";
        throw RpcError(code::invalidParams, "direction is forward, back, left or right");
    }

    // The heading a turn or look request points at, in radians.
    double ResolveHeading(const Json& params, RE::PlayerCharacter& player)
    {
        if (params.contains("headingDeg")) {
            return Rad(Require<double>(params, "headingDeg"));
        }
        if (params.contains("refID")) {
            return HeadingTo(player.GetPosition(), RequireRef(params["refID"])->GetPosition());
        }
        if (params.contains("x") && params.contains("y")) {
            RE::NiPoint3 to{ Require<float>(params, "x"), Require<float>(params, "y"), 0.0F };
            return HeadingTo(player.GetPosition(), to);
        }
        throw RpcError(code::invalidParams, "Give headingDeg, x and y, or refID");
    }

    constexpr std::array<const char*, 13> modeNames{ "firstPerson", "autoVanity", "vats", "free", "ironSights",
        "furniture", "transition", "tween", "animated", "thirdPerson", "mount", "bleedout", "dragon" };

    Json CameraState()
    {
        const auto camera = RE::PlayerCamera::GetSingleton();
        if (!camera) {
            throw RpcError(code::notReady, "No player camera");
        }
        Json out;
        const auto id = camera->currentState ? static_cast<int>(camera->currentState->id) : -1;
        out["mode"] = id >= 0 && id < static_cast<int>(modeNames.size()) ? Json(modeNames[id]) : Json(nullptr);
        out["stateId"] = id;
        if (const auto root = camera->cameraRoot.get()) {
            const auto& world = root->world;
            out["pos"] = Point(world.translate);
            // Column 1 of the world rotation is the camera's forward axis.
            const double fx = world.rotate.entry[0][1];
            const double fy = world.rotate.entry[1][1];
            const double fz = world.rotate.entry[2][1];
            out["yawDeg"] = Round(Deg(std::atan2(fx, fy)), 100.0);
            out["pitchDeg"] = Round(Deg(-std::asin(std::clamp(fz, -1.0, 1.0))), 100.0);
        }
        const auto& data = camera->GetRuntimeData2();
        out["worldFov"] = Round(data.worldFOV, 100.0);
        out["firstPersonFov"] = Round(data.firstPersonFOV, 100.0);
        if (const auto third = ThirdPerson()) {
            out["zoom"] = Round(third->currentZoomOffset, 1000.0);
            out["targetZoom"] = Round(third->targetZoomOffset, 1000.0);
            out["freeRotationDeg"] = Json::array({ Round(Deg(third->freeRotation.x), 100.0), Round(Deg(third->freeRotation.y), 100.0) });
        } else {
            out["zoom"] = nullptr;
        }
        if (const auto player = RE::PlayerCharacter::GetSingleton()) {
            out["player"] = HeadingJson(*player);
        }
        return out;
    }
}

namespace devtools::nav
{
    Json CameraSnapshot() { return CameraState(); }
}

DEVTOOLS_MODULE(NavMove)
{
    input::SetPollListener([](float seconds, input::Drive& drive) {
        std::lock_guard lock(Lock());
        Tick tick{ seconds, Now(), drive };
        for (auto& ticker : Tickers()) {
            ticker(tick);
        }
    });
    AddTicker(TickMove);
    SetCapability("movement", input::Hooked());

    const MethodSpec main{ .thread = Thread::Main };

    RegisterDeferred("Move.walk", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "direction", "durationMs", "distance", "run", "sprint", "stuck", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        if (walk || CurrentDrive().owner == "nav") {
            throw RpcError(code::busy, "The player is already being driven (Move.walk or Nav.goto)");
        }
        Walk next;
        next.control = DirectionControl(Optional<std::string>(params, "direction", "forward"));
        next.duration = Optional<double>(params, "durationMs", 0.0) / 1000.0;
        next.distance = Optional<double>(params, "distance", 0.0);
        if ((next.duration > 0.0) == (next.distance > 0.0) || next.duration > 60.0 || next.distance > 20000.0) {
            throw RpcError(code::invalidParams, "Give durationMs (up to 60000) or distance (up to 20000 units)");
        }
        next.sprint = Optional<bool>(params, "sprint", false);
        next.det = stuck::Detector(stuck::ParseConfig(params));
        if (params.contains("run")) {
            if (const auto running = Running()) {
                next.restoreRunning = *running;
                *running = Require<bool>(params, "run");
            }
        }
        auto& player = Player();
        next.start = next.last = player.GetPosition();
        next.started = Now();
        next.det.Reset(next.started);
        next.reply = std::move(reply);
        walk = std::move(next);
        CurrentDrive().owner = "walk";
    }, { .thread = Thread::Main, .timeout = 130s });

    RegisterDeferred("Move.turnTo", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "headingDeg", "x", "y", "refID", "smooth", "degPerSec", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        auto& player = Player();
        const auto target = ResolveHeading(params, player);
        if (!Optional<bool>(params, "smooth", true)) {
            FaceHeading(player, target);
            reply->Ok(HeadingJson(player));
            return;
        }
        const auto rate = Optional<double>(params, "degPerSec", 180.0);
        if (rate < 10.0 || rate > 3600.0) {
            throw RpcError(code::invalidParams, "degPerSec is 10 to 3600");
        }
        if (turn) {
            turn->reply->Fail(code::busy, "Replaced by a newer Move.turnTo");
        }
        turn = Turn{ target, Rad(rate), std::move(reply) };
    }, { .thread = Thread::Main, .timeout = 30s });

    Register("Move.jump", [](const Json& params) {
        AllowOnly(params, { "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        jumpPolls = 3;
        return Json{ { "accepted", true } };
    }, main);

    RegisterDeferred("Move.sneak", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "on", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        if (sneak) {
            sneak->reply->Fail(code::busy, "Replaced by a newer Move.sneak");
        }
        sneak = Sneak{ Require<bool>(params, "on"), 0, false, std::move(reply) };
    }, { .thread = Thread::Main, .timeout = 10s });

    Register("Move.stop", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        const bool hadWalk = walk.has_value();
        if (walk) {
            if (const auto player = RE::PlayerCharacter::GetSingleton()) {
                FinishWalk(*player, true);
            }
        }
        if (turn) {
            turn->reply->Fail(code::busy, "Stopped by Move.stop");
            turn.reset();
        }
        return Json{ { "stoppedWalk", hadWalk } };
    }, main);

    Register("Camera.state", [](const Json& params) {
        AllowOnly(params, {});
        RequireUI();
        std::lock_guard lock(Lock());
        return CameraState();
    }, main);

    Register("Camera.set", [](const Json& params) {
        AllowOnly(params, { "mode", "yawDeg", "pitchDeg", "zoom", "fov", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        auto& player = Player();
        const auto camera = RE::PlayerCamera::GetSingleton();
        if (params.contains("mode")) {
            const auto mode = Require<std::string>(params, "mode");
            if (mode == "first") {
                camera->ForceFirstPerson();
            } else if (mode == "third") {
                camera->ForceThirdPerson();
            } else {
                throw RpcError(code::invalidParams, "mode is first or third");
            }
        }
        if (params.contains("yawDeg")) {
            FaceHeading(player, Rad(Require<double>(params, "yawDeg")));
        }
        if (params.contains("pitchDeg")) {
            FacePitch(player, Rad(Require<double>(params, "pitchDeg")));
        }
        if (params.contains("zoom")) {
            const auto zoom = Require<float>(params, "zoom");
            if (zoom < 0.0F || zoom > 1.0F) {
                throw RpcError(code::invalidParams, "zoom is 0 (closest) to 1 (farthest)");
            }
            if (const auto third = ThirdPerson()) {
                third->targetZoomOffset = zoom;
            } else {
                throw RpcError(code::notReady, "zoom needs the third-person camera");
            }
        }
        if (params.contains("fov")) {
            const auto fov = Require<float>(params, "fov");
            if (fov < 5.0F || fov > 150.0F) {
                throw RpcError(code::invalidParams, "fov is 5 to 150 degrees");
            }
            camera->GetRuntimeData2().worldFOV = fov;
        }
        return Json{ { "requested", params } };
    }, main);

    Register("Camera.lookAt", [](const Json& params) {
        AllowOnly(params, { "refID", "x", "y", "z", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        auto& player = Player();
        RE::NiPoint3 target;
        Json describe = nullptr;
        if (params.contains("refID")) {
            const auto ref = RequireRef(params["refID"]);
            target = ref->GetPosition();
            // Chest height for an actor; the middle of the visible model for anything else,
            // since many origins sit on the floor or at a corner of the mesh.
            if (ref->Is(RE::FormType::ActorCharacter)) {
                target.z += 90.0F;
            } else if (const auto root = ref->Get3D(); root && root->worldBound.radius > 0.0F) {
                target = root->worldBound.center;
            }
            describe = DescribeRef(ref);
        } else {
            target = { Require<float>(params, "x"), Require<float>(params, "y"), Require<float>(params, "z") };
        }
        auto eye = player.GetPosition();
        eye.z += 110.0F;
        const auto heading = HeadingTo(eye, target);
        const auto pitch = std::atan2(static_cast<double>(eye.z - target.z), Dist2D(eye, target));
        FaceHeading(player, heading);
        FacePitch(player, pitch);
        return Json{ { "target", describe.is_null() ? Point(target) : describe }, { "headingDeg", Round(Deg(heading), 100.0) },
            { "pitchDeg", Round(Deg(pitch), 100.0) }, { "distance", Round(Dist3D(eye, target)) } };
    }, main);
}
