// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Ref.activate: activate a reference the way the player's activate key would, without aiming at it.
#include "game.h"
#include "methods.h"
#include "state_common.h"

#include <cmath>

namespace
{
    using namespace devtools;
    using namespace devtools::view;

    // About how far the player can reach to activate something; the engine's own pick length is shorter
    // but measured from the camera to the surface, while this is measured to the nearest of the origin
    // and the bounds centre because a door's origin sits at its hinge.
    constexpr float defaultReach = 300.0f;

    RE::NiPoint3 BoundsCentre(RE::TESObjectREFR* ref)
    {
        const auto local = (ref->GetBoundMin() + ref->GetBoundMax()) * 0.5f;
        const auto yaw = ref->GetAngleZ();
        const auto sine = std::sin(yaw);
        const auto cosine = std::cos(yaw);
        // Reference rotation about Z is clockwise in game space.
        return ref->GetPosition() + RE::NiPoint3{ local.x * cosine + local.y * sine, local.y * cosine - local.x * sine, local.z };
    }
}

DEVTOOLS_MODULE(Activate)
{
    Register("Ref.activate", [](const Json& params) {
        AllowOnly(params, { "refID", "maxDistance", "force" });
        RequirePlayable(params);
        auto& player = RequirePlayer();
        const auto ref = RequireRef(params, "refID");
        const bool force = Optional<bool>(params, "force", false);
        const auto reach = std::clamp(Optional<float>(params, "maxDistance", defaultReach), 1.0f, 5000.0f);

        if (ref->IsDisabled() || ref->IsDeleted() || !ref->Is3DLoaded()) {
            throw RpcError(code::notFound, "That reference is disabled or not loaded (move closer or into its cell)");
        }
        const auto here = player.GetPosition();
        const auto distance = std::min(here.GetDistance(ref->GetPosition()), here.GetDistance(BoundsCentre(ref)));
        Json out{ { "refID", Hex(ref->GetFormID()) }, { "type", RefCategory(ref) }, { "name", Name(RefName(ref)) },
            { "distance", Round(distance) }, { "reach", Round(reach) }, { "reachChecked", !force },
            // Only distance is tested; walls between the player and the target are not.
            { "lineOfSightChecked", false } };
        if (!force && distance > reach) {
            out["activated"] = false;
            out["reason"] = "out of reach";
            return out;
        }
        const bool locked = ref->IsLocked();
        out["wasLocked"] = locked;
        out["activated"] = ref->ActivateRef(&player, 0, nullptr, 1, false);
        if (locked) {
            out["locked"] = ref->IsLocked();
        }
        return out;
    }, { .thread = Thread::Main });
}
