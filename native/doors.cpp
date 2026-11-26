// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Door details for reference views: where a teleport door leads and how it is locked.
#include "state_common.h"

namespace devtools::view
{
    namespace
    {
        const char* LockName(RE::LOCK_LEVEL level)
        {
            switch (level) {
            case RE::LOCK_LEVEL::kUnlocked: return "unlocked";
            case RE::LOCK_LEVEL::kVeryEasy: return "veryEasy";
            case RE::LOCK_LEVEL::kEasy: return "easy";
            case RE::LOCK_LEVEL::kAverage: return "average";
            case RE::LOCK_LEVEL::kHard: return "hard";
            case RE::LOCK_LEVEL::kVeryHard: return "veryHard";
            case RE::LOCK_LEVEL::kRequiresKey: return "requiresKey";
            }
            return "unknown";
        }
    }

    void AddDoorDetails(RE::TESObjectREFR* ref, Json& out)
    {
        out["locked"] = ref->IsLocked();
        if (ref->IsLocked()) {
            out["lockLevel"] = LockName(ref->GetLockLevel());
        }
        const auto teleport = ref->extraList.GetByType<RE::ExtraTeleport>();
        if (!teleport || !teleport->teleportData) {
            out["teleports"] = false;
            return;
        }
        out["teleports"] = true;
        Json destination{ { "resolved", false } };
        if (const auto linked = teleport->teleportData->linkedDoor.get()) {
            destination["resolved"] = true;
            destination["doorRefID"] = Hex(linked->GetFormID());
            if (const auto cell = linked->GetParentCell()) {
                destination["cell"] = Name(cell->GetName());
                destination["cellEditorID"] = Name(cell->GetFormEditorID());
                destination["interior"] = cell->IsInteriorCell();
            }
            if (const auto location = linked->GetCurrentLocation()) {
                destination["location"] = Name(location->GetName());
            }
            // An exterior cell often has no name of its own; the worldspace names the place then.
            if (const auto space = linked->GetWorldspace()) {
                destination["worldspace"] = Name(space->GetName());
            }
        }
        destination["pos"] = Vec3(teleport->teleportData->position);
        out["destination"] = std::move(destination);
    }
}
