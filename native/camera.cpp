// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Camera control beyond Camera.set: switching the point of view (including the idle orbit camera),
// the free camera with an explicit pose, and the switch that keeps the idle orbit camera from
// starting. Camera.state (nav_move.cpp) stays the way to read the pose.
#include "state_common.h"

#include <cmath>

namespace devtools::nav
{
    Json CameraSnapshot();  // nav_move.cpp
}

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using devtools::nav::CameraSnapshot;

    RE::PlayerCamera& RequireCamera()
    {
        RequireUI();
        const auto camera = RE::PlayerCamera::GetSingleton();
        if (!camera || !camera->currentState) {
            throw RpcError(code::notReady, "No player camera");
        }
        return *camera;
    }

    RE::FreeCameraState* FreeState(RE::PlayerCamera& camera)
    {
        if (camera.currentState && camera.currentState->id == RE::CameraState::kFree) {
            return static_cast<RE::FreeCameraState*>(camera.currentState.get());
        }
        return nullptr;
    }

    Json Pose(RE::PlayerCamera& camera)
    {
        auto out = CameraSnapshot();
        out["free"] = camera.IsInFreeCameraMode();
        out["pov"] = camera.IsInFirstPerson() ? "first" : camera.IsInThirdPerson() ? "third" : "other";
        out["autoVanityAllowed"] = camera.allowAutoVanityMode;
        return out;
    }

    float Radians(double degrees)
    {
        return static_cast<float>(degrees * 3.14159265358979323846 / 180.0);
    }

    bool HasPose(const Json& params)
    {
        return params.contains("pos") || params.contains("yawDeg") || params.contains("pitchDeg");
    }

    // Free-camera pose fields use Camera.state's convention: heading clockwise from north, pitch positive down.
    void ApplyPose(RE::FreeCameraState& state, const Json& params)
    {
        if (params.contains("pos")) {
            const auto pos = Require<std::vector<float>>(params, "pos");
            if (pos.size() != 3) {
                throw RpcError(code::invalidParams, "pos is [x, y, z]");
            }
            state.translation = { pos[0], pos[1], pos[2] };
        }
        if (params.contains("yawDeg")) {
            state.rotation.y = Radians(Require<double>(params, "yawDeg"));
        }
        if (params.contains("pitchDeg")) {
            const auto pitch = Require<double>(params, "pitchDeg");
            if (pitch < -89.0 || pitch > 89.0) {
                throw RpcError(code::invalidParams, "pitchDeg is -89 to 89");
            }
            state.rotation.x = Radians(pitch);
        }
    }

    void ApplyFov(RE::PlayerCamera& camera, const Json& params)
    {
        if (params.contains("fov")) {
            const auto fov = Require<float>(params, "fov");
            if (fov < 5.0F || fov > 150.0F) {
                throw RpcError(code::invalidParams, "fov is 5 to 150 degrees");
            }
            camera.GetRuntimeData2().worldFOV = fov;
        }
    }
}

DEVTOOLS_MODULE(CameraControl)
{
    Register("Camera.pose", [](const Json& params) {
        AllowOnly(params, {});
        return Pose(RequireCamera());
    }, { .thread = Thread::Main });

    // first, third, vanity (the orbit camera) or free. Camera.set only covers first and third.
    Register("Camera.mode", [](const Json& params) {
        AllowOnly(params, { "mode", "freezeTime", "loadEpoch" });
        RequirePlayable(params);
        auto& camera = RequireCamera();
        const auto mode = Require<std::string>(params, "mode");
        const bool free = camera.IsInFreeCameraMode();
        if (mode != "free" && free) {
            camera.ToggleFreeCameraMode(false);
        }
        if (mode == "first") {
            camera.ForceFirstPerson();
        } else if (mode == "third") {
            camera.ForceThirdPerson();
        } else if (mode == "vanity") {
            const auto& state = camera.cameraStates[RE::CameraState::kAutoVanity];
            if (!state) {
                throw RpcError(code::notReady, "The orbit camera state is not available");
            }
            camera.SetState(state.get());
        } else if (mode == "free") {
            if (!free) {
                camera.ToggleFreeCameraMode(Optional<bool>(params, "freezeTime", false));
            }
        } else {
            throw RpcError(code::invalidParams, "mode is first, third, vanity or free");
        }
        return Pose(camera);
    }, { .thread = Thread::Main });

    // on=true enters the free camera (once) and applies the pose given; calling again while it is on only moves it.
    Register("Camera.free", [](const Json& params) {
        AllowOnly(params, { "on", "freezeTime", "pos", "yawDeg", "pitchDeg", "fov", "loadEpoch" });
        RequirePlayable(params);
        auto& camera = RequireCamera();
        const auto on = Require<bool>(params, "on");
        const bool inFree = camera.IsInFreeCameraMode();
        if (!on) {
            if (inFree) {
                camera.ToggleFreeCameraMode(false);
            }
            return Pose(camera);
        }
        if (!inFree) {
            camera.ToggleFreeCameraMode(Optional<bool>(params, "freezeTime", false));
        }
        ApplyFov(camera, params);
        auto result = Json::object();
        if (const auto state = FreeState(camera)) {
            if (HasPose(params)) {
                ApplyPose(*state, params);
            }
            result = Pose(camera);
            result["applied"] = true;
        } else {
            // The state switch is not visible yet; a second call with on=true applies the pose.
            result = Pose(camera);
            result["applied"] = !HasPose(params);
            result["pending"] = HasPose(params);
        }
        return result;
    }, { .thread = Thread::Main });

    // The orbit camera starts after the idle delay; with allow=false it never does.
    Register("Camera.autoVanity", [](const Json& params) {
        AllowOnly(params, { "allow" });
        auto& camera = RequireCamera();
        if (params.contains("allow")) {
            const bool allow = Require<bool>(params, "allow");
            camera.allowAutoVanityMode = allow;
            if (!allow && camera.currentState->id == RE::CameraState::kAutoVanity) {
                camera.ForceThirdPerson();
            }
        }
        return Json{ { "allow", camera.allowAutoVanityMode }, { "mode", camera.currentState->id == RE::CameraState::kAutoVanity ? "vanity" : "other" } };
    }, { .thread = Thread::Main });
}
