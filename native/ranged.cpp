// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Ranged combat inputs: what bow, ammunition or projectile spell the player has, whether a straight line to a
// target is open, where the player's own projectiles are, and an absolute aim. The ballistic maths and the shooting
// loop live in the Python client (skyrim_devtools/use/ranged.py) so they can be unit tested without a game.
#include "game.h"
#include "methods.h"
#include "state_common.h"

#include <algorithm>
#include <cmath>

#undef GetObject

namespace
{
    using namespace devtools;
    using namespace devtools::view;

    constexpr float radiansToDegrees = 57.29577951308232f;
    constexpr float eyeHeight = 110.0f;

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return std::tolower(c); });
        return text;
    }

    RE::NiPoint3 PointOf(const Json& value, const char* what)
    {
        if (!value.is_array() || value.size() != 3) {
            throw RpcError(code::invalidParams, std::string(what) + " is [x, y, z]");
        }
        return { value[0].get<float>(), value[1].get<float>(), value[2].get<float>() };
    }

    // Aim points on an actor, from its feet position and height.
    RE::NiPoint3 Torso(RE::TESObjectREFR& ref)
    {
        auto pos = ref.GetPosition();
        pos.z += ref.GetHeight() * 0.62f;
        return pos;
    }

    RE::NiPoint3 Head(RE::TESObjectREFR& ref)
    {
        auto pos = ref.GetPosition();
        pos.z += ref.GetHeight() * 0.93f;
        return pos;
    }

    bool IsActorLayer(RE::COL_LAYER layer)
    {
        using L = RE::COL_LAYER;
        return layer == L::kBiped || layer == L::kCharController || layer == L::kDeadBip || layer == L::kBipedNoCC;
    }

    struct Ray
    {
        bool blocked = false;
        float fraction = 1.0f;
        float distance = 0.0f;
        int layer = 0;
        RE::NiPoint3 point;
        bool noWorld = false;
    };

    // A straight line against what blocks sight. Actors near the end of the line are the target itself, not a blocker.
    Ray CastLine(RE::TESObjectCELL* cell, const RE::NiPoint3& from, const RE::NiPoint3& to)
    {
        Ray out;
        const auto world = cell ? cell->GetbhkWorld() : nullptr;
        if (!world) {
            out.noWorld = true;
            return out;
        }
        const auto scale = RE::bhkWorld::GetWorldScale();
        RE::bhkPickData pick;
        pick.rayInput.from = RE::hkVector4(from * scale);
        pick.rayInput.to = RE::hkVector4(to * scale);
        pick.rayInput.filterInfo.SetCollisionLayer(RE::COL_LAYER::kLOS);
        {
            const RE::BSReadLockGuard guard{ world->worldLock };
            world->PickObject(pick);
        }
        const auto length = from.GetDistance(to);
        if (!pick.rayOutput.HasHit()) {
            return out;
        }
        out.fraction = pick.rayOutput.hitFraction;
        out.distance = out.fraction * length;
        out.point = from + (to - from) * out.fraction;
        if (pick.rayOutput.rootCollidable) {
            out.layer = static_cast<int>(pick.rayOutput.rootCollidable->GetCollisionLayer());
        }
        const bool nearEnd = length - out.distance < std::min(170.0f, 60.0f + length * 0.1f);
        out.blocked = !(IsActorLayer(static_cast<RE::COL_LAYER>(out.layer)) && nearEnd);
        return out;
    }

    Json RayJson(const Ray& ray)
    {
        Json out{ { "clear", !ray.blocked } };
        if (ray.fraction < 1.0f) {
            out["hitDistance"] = Round(ray.distance, 0);
            out["hitLayer"] = ray.layer;
            out["hitPoint"] = Vec3(ray.point);
        }
        return out;
    }

    RE::NiPoint3 Eye(RE::PlayerCharacter& player)
    {
        auto pos = player.GetPosition();
        pos.z += eyeHeight * player.GetScale();
        return pos;
    }

    const char* AmmoKind(RE::TESAmmo& ammo) { return ammo.IsBolt() ? "bolt" : "arrow"; }

    Json ProjectileJson(RE::BGSProjectile* projectile)
    {
        if (!projectile) {
            return nullptr;
        }
        return { { "formID", Hex(projectile->GetFormID()) }, { "speed", Round(projectile->data.speed, 1) },
            { "gravity", Round(projectile->data.gravity, 3) }, { "range", Round(projectile->data.range, 0) } };
    }

    Json EquippedJson(RE::TESForm* form)
    {
        if (!form) {
            return nullptr;
        }
        return { { "formID", Hex(form->GetFormID()) }, { "name", Name(form->GetName()) } };
    }

    RE::BGSProjectile* SpellProjectile(RE::SpellItem& spell)
    {
        for (const auto effect : spell.effects) {
            if (effect && effect->baseEffect && effect->baseEffect->data.projectileBase) {
                return effect->baseEffect->data.projectileBase;
            }
        }
        return nullptr;
    }

    const char* PartName(const std::string& part)
    {
        return part == "head" ? "head" : "torso";
    }
}

DEVTOOLS_MODULE(Ranged)
{
    // The bow and ammunition the player holds or carries. With equip (default) the best of each is equipped.
    // With spell it describes the projectile spell in the right hand instead.
    Register("Ranged.loadout", [](const Json& params) {
        AllowOnly(params, { "equip", "spell" });
        auto& player = RequirePlayer();
        if (Optional<bool>(params, "spell", false)) {
            const auto form = player.GetEquippedObject(false);
            const auto spell = form ? form->As<RE::SpellItem>() : nullptr;
            if (!spell) {
                return Json{ { "weapon", "spell" }, { "ready", false }, { "missing", Json::array({ "spell" }) } };
            }
            const auto projectile = SpellProjectile(*spell);
            Json out{ { "weapon", "spell" }, { "spell", EquippedJson(spell) }, { "projectile", ProjectileJson(projectile) },
                { "concentration", spell->GetCastingType() == RE::MagicSystem::CastingType::kConcentration },
                { "cost", Round(spell->CalculateMagickaCost(&player), 0) },
                { "magicka", Round(player.AsActorValueOwner()->GetActorValue(RE::ActorValue::kMagicka), 0) } };
            const bool usable = projectile && !out["concentration"].get<bool>();
            out["ready"] = usable;
            out["missing"] = usable ? Json::array() : Json::array({ "projectileSpell" });
            return out;
        }

        RE::TESObjectWEAP* bow = nullptr;
        RE::TESAmmo* ammo = nullptr;
        int ammoCount = 0;
        for (const auto& [object, data] : player.GetInventory()) {
            if (data.first <= 0) {
                continue;
            }
            if (const auto weapon = object->As<RE::TESObjectWEAP>(); weapon && (weapon->IsBow() || weapon->IsCrossbow())) {
                if (!bow || weapon->GetAttackDamage() > bow->GetAttackDamage()) {
                    bow = weapon;
                }
            } else if (const auto round = object->As<RE::TESAmmo>()) {
                const auto damage = round->GetRuntimeData().data.damage;
                if (!ammo || damage > ammo->GetRuntimeData().data.damage) {
                    ammo = round;
                    ammoCount = data.first;
                }
            }
        }
        // A bow already in hand wins over the best one in the bag, so a deliberate choice is kept.
        if (const auto held = player.GetEquippedObject(false); held && held->As<RE::TESObjectWEAP>() &&
                                                               (held->As<RE::TESObjectWEAP>()->IsBow() || held->As<RE::TESObjectWEAP>()->IsCrossbow())) {
            bow = held->As<RE::TESObjectWEAP>();
        }
        // Bolts for a crossbow, arrows for a bow.
        if (bow && ammo && ammo->IsBolt() != bow->IsCrossbow()) {
            ammo = nullptr;
            ammoCount = 0;
            for (const auto& [object, data] : player.GetInventory()) {
                const auto round = data.first > 0 ? object->As<RE::TESAmmo>() : nullptr;
                if (round && round->IsBolt() == bow->IsCrossbow() &&
                    (!ammo || round->GetRuntimeData().data.damage > ammo->GetRuntimeData().data.damage)) {
                    ammo = round;
                    ammoCount = data.first;
                }
            }
        }

        const auto manager = RE::ActorEquipManager::GetSingleton();
        const auto equipped = player.GetEquippedObject(false);
        const bool bowEquipped = bow && equipped == bow;
        const auto currentAmmo = player.GetCurrentAmmo();
        const bool ammoEquipped = ammo && currentAmmo == ammo;
        bool queued = false;
        if (Optional<bool>(params, "equip", true) && manager) {
            if (bow && !bowEquipped) {
                manager->EquipObject(&player, bow, nullptr, 1, nullptr, true, false, true, false);
                queued = true;
            }
            if (ammo && !ammoEquipped) {
                manager->EquipObject(&player, ammo, nullptr, 1, nullptr, true, false, true, false);
                queued = true;
            }
        }

        Json out{ { "weapon", "bow" }, { "equipQueued", queued }, { "bowEquipped", bowEquipped }, { "ammoEquipped", ammoEquipped } };
        auto missing = Json::array();
        if (bow) {
            out["bow"] = { { "formID", Hex(bow->GetFormID()) }, { "name", Name(bow->GetName()) }, { "speed", Round(bow->GetSpeed(), 2) },
                { "crossbow", bow->IsCrossbow() }, { "damage", bow->GetAttackDamage() }, { "reach", Round(bow->GetReach(), 1) } };
        } else {
            missing.push_back("bow");
        }
        if (ammo) {
            const auto& data = ammo->GetRuntimeData().data;
            out["ammo"] = { { "formID", Hex(ammo->GetFormID()) }, { "name", Name(ammo->GetName()) }, { "kind", AmmoKind(*ammo) },
                { "count", ammoCount }, { "damage", Round(data.damage, 1) }, { "projectile", ProjectileJson(data.projectile) } };
        } else {
            missing.push_back(bow && bow->IsCrossbow() ? "bolts" : "arrows");
        }
        out["missing"] = missing;
        out["ready"] = missing.empty();
        return out;
    }, { .thread = Thread::Main });

    // Whether the line from the player's eye (or from `from`, or from each of `points`, taken as feet positions) to the
    // target's torso or head is open, with where it first meets geometry. Also the target's aim points and health.
    Register("Ranged.los", [](const Json& params) {
        AllowOnly(params, { "refID", "part", "from", "points" });
        auto& player = RequirePlayer();
        const auto ref = RequireRef(params, "refID");
        const auto part = Lower(Optional<std::string>(params, "part", "torso"));
        if (part != "torso" && part != "head") {
            throw RpcError(code::invalidParams, "part is torso or head");
        }
        const auto aim = part == "head" ? Head(*ref) : Torso(*ref);
        const auto cell = player.GetParentCell();
        const auto from = params.contains("from") ? PointOf(params["from"], "from") : Eye(player);
        Json out = RayJson(CastLine(cell, from, aim));
        out["part"] = PartName(part);
        out["from"] = Vec3(from);
        out["torso"] = Vec3(Torso(*ref));
        out["head"] = Vec3(Head(*ref));
        out["target"] = Vec3(ref->GetPosition());
        out["distance"] = Round(from.GetDistance(aim), 0);
        out["sameCell"] = ref->GetParentCell() == cell;
        if (const auto actor = ref->As<RE::Actor>()) {
            out["health"] = actor->AsActorValueOwner()->GetActorValue(RE::ActorValue::kHealth);
            out["dead"] = actor->IsDead();
        }
        if (params.contains("points")) {
            auto list = Json::array();
            for (const auto& item : params["points"]) {
                auto feet = PointOf(item, "points entry");
                const auto start = RE::NiPoint3{ feet.x, feet.y, feet.z + eyeHeight };
                auto entry = RayJson(CastLine(cell, start, aim));
                entry["distance"] = Round(start.GetDistance(aim), 0);
                list.push_back(std::move(entry));
            }
            out["points"] = std::move(list);
        }
        return out;
    }, { .thread = Thread::Main });

    // Absolute aim: heading is degrees clockwise from north (+Y), pitch is degrees above the horizon.
    Register("Ranged.aim", [](const Json& params) {
        AllowOnly(params, { "headingDeg", "pitchDeg" });
        auto& player = RequirePlayer();
        const auto heading = Require<float>(params, "headingDeg");
        const auto pitch = std::clamp(Require<float>(params, "pitchDeg"), -80.0f, 80.0f);
        player.SetHeading(heading / radiansToDegrees);
        // The engine's look value is positive downwards.
        player.SetLooking(-pitch / radiansToDegrees);
        const auto state = player.AsActorState();
        return Json{ { "headingDeg", Round(std::fmod(heading + 360.0f, 360.0f), 2) }, { "pitchDeg", Round(pitch, 2) },
            { "attackState", static_cast<int>(state->GetAttackState()) }, { "weaponState", static_cast<int>(state->GetWeaponState()) },
            { "eye", Vec3(Eye(player)) } };
    }, { .thread = Thread::Main });

    // The player's own projectiles in flight or stuck: position, velocity (units/s) and distance flown.
    Register("Ranged.arrows", [](const Json&) {
        auto& player = RequirePlayer();
        auto list = Json::array();
        if (const auto manager = RE::Projectile::Manager::GetSingleton()) {
            const RE::BSSpinLockGuard guard{ manager->projectileLock };
            const auto add = [&](const RE::BSTArray<RE::ProjectileHandle>& handles) {
                for (const auto& handle : handles) {
                    const auto projectile = handle.get();
                    if (!projectile) {
                        continue;
                    }
                    const auto& data = projectile->GetProjectileRuntimeData();
                    if (data.shooter.get().get() != &player) {
                        continue;
                    }
                    list.push_back({ { "refID", Hex(projectile->GetFormID()) }, { "pos", Vec3(projectile->GetPosition()) },
                        { "velocity", Vec3(data.linearVelocity) }, { "distance", Round(data.distanceMoved, 0) },
                        { "power", Round(data.power, 2) }, { "destroyed", data.flags.any(RE::Projectile::Flags::kDestroyed) } });
                }
            };
            add(manager->limited);
            add(manager->unlimited);
        }
        return Json{ { "arrows", std::move(list) } };
    }, { .thread = Thread::Main });

    // Draw progress and ammunition left, cheap enough to poll while the string is held.
    Register("Ranged.state", [](const Json&) {
        auto& player = RequirePlayer();
        const auto state = player.AsActorState();
        Json out{ { "attackState", static_cast<int>(state->GetAttackState()) }, { "weaponState", static_cast<int>(state->GetWeaponState()) },
            { "drawn", state->IsWeaponDrawn() }, { "eye", Vec3(Eye(player)) }, { "position", Vec3(player.GetPosition()) },
            { "headingDeg", Round(std::fmod(player.GetAngleZ() * radiansToDegrees + 360.0f, 360.0f), 2) } };
        if (const auto ammo = player.GetCurrentAmmo()) {
            const auto counts = player.GetInventoryCounts([&](RE::TESBoundObject& o) { return &o == ammo; });
            const auto found = counts.find(ammo);
            out["ammoLeft"] = found == counts.end() ? 0 : found->second;
        }
        return out;
    }, { .thread = Thread::Main });
}
