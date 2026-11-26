// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Combat: what a player does in a fight (equip, draw, attack, block, cast, drink) and what a player sees
// (who is hostile, their health, own vitals). Actions go through the same controls a keyboard presses;
// only equipping and facing a target call into the engine directly.
#include "game.h"
#include "methods.h"
#include "state_common.h"

#include <algorithm>
#include <cmath>
#include <numbers>

// <windows.h> renames GetObject to GetObjectA, which hides BGSDefaultObjectManager::GetObject.
#undef GetObject

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace std::chrono_literals;

    constexpr float radiansToDegrees = 57.29577951308232f;
    constexpr float defaultRadius = 3000.0f;

    // The ControlMap events for the two hands; the same button swings a weapon, casts a spell or blocks.
    constexpr const char* rightControl = "Right Attack/Block";
    constexpr const char* leftControl = "Left Attack/Block";

    // The chosen enemy, remembered between calls so a fight is "target, then attack" like a lock-on.
    RE::ObjectRefHandle chosenTarget;

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return std::tolower(c); });
        return text;
    }

    float Wrap180(float degrees)
    {
        while (degrees > 180.0f) {
            degrees -= 360.0f;
        }
        while (degrees < -180.0f) {
            degrees += 360.0f;
        }
        return degrees;
    }

    // Bearing in the game's convention: degrees clockwise from +Y.
    float BearingTo(const RE::NiPoint3& from, const RE::NiPoint3& to)
    {
        return std::atan2(to.x - from.x, to.y - from.y) * radiansToDegrees;
    }

    const char* AttackStateName(RE::ATTACK_STATE_ENUM state)
    {
        switch (state) {
        case RE::ATTACK_STATE_ENUM::kNone: return "none";
        case RE::ATTACK_STATE_ENUM::kDraw: return "windup";
        case RE::ATTACK_STATE_ENUM::kSwing: return "swing";
        case RE::ATTACK_STATE_ENUM::kHit: return "hit";
        case RE::ATTACK_STATE_ENUM::kNextAttack: return "nextAttack";
        case RE::ATTACK_STATE_ENUM::kFollowThrough: return "followThrough";
        case RE::ATTACK_STATE_ENUM::kBash: return "bash";
        default: return "other";
        }
    }

    const char* WeaponStateName(RE::WEAPON_STATE state)
    {
        switch (state) {
        case RE::WEAPON_STATE::kSheathed: return "sheathed";
        case RE::WEAPON_STATE::kWantToDraw: return "wantToDraw";
        case RE::WEAPON_STATE::kDrawing: return "drawing";
        case RE::WEAPON_STATE::kDrawn: return "drawn";
        case RE::WEAPON_STATE::kWantToSheathe: return "wantToSheathe";
        case RE::WEAPON_STATE::kSheathing: return "sheathing";
        default: return "other";
        }
    }

    Json Vital(RE::Actor& actor, RE::ActorValue av)
    {
        const auto current = actor.AsActorValueOwner()->GetActorValue(av);
        const auto maximum = actor.GetActorValueMax(av);
        Json out{ { "cur", Round(current, 0) }, { "max", Round(maximum, 0) } };
        if (maximum > 0.0f) {
            out["pct"] = Round(std::clamp(current / maximum, 0.0f, 1.0f) * 100.0f, 0);
        }
        return out;
    }

    Json Equipped(RE::TESForm* form)
    {
        if (!form) {
            return nullptr;
        }
        return { { "formID", Hex(form->GetFormID()) }, { "name", Name(form->GetName()) }, { "type", FormTypeName(form) } };
    }

    bool IsHostile(RE::Actor& actor, RE::PlayerCharacter& player)
    {
        if (&actor == &player || actor.IsDead() || actor.IsDisabled() || actor.IsDeleted() || !actor.Is3DLoaded()) {
            return false;
        }
        if (actor.GetActorRuntimeData().currentCombatTarget.get().get() == &player) {
            return true;
        }
        return actor.IsHostileToActor(&player);
    }

    struct Hostile
    {
        RE::Actor* actor;
        float distance;
    };

    std::vector<Hostile> FindHostiles(RE::PlayerCharacter& player, float radius)
    {
        std::vector<Hostile> found;
        const auto tes = RE::TES::GetSingleton();
        if (!tes) {
            throw RpcError(code::notReady, "The world is not available yet");
        }
        const auto origin = player.GetPosition();
        tes->ForEachReferenceInRange(&player, radius, [&](RE::TESObjectREFR* ref) {
            if (const auto actor = ref ? ref->As<RE::Actor>() : nullptr; actor && IsHostile(*actor, player)) {
                found.push_back({ actor, origin.GetDistance(actor->GetPosition()) });
            }
            return RE::BSContainer::ForEachResult::kContinue;
        });
        std::ranges::sort(found, [](const auto& a, const auto& b) { return a.distance < b.distance; });
        return found;
    }

    Json DescribeHostile(const Hostile& hostile, RE::PlayerCharacter& player)
    {
        auto& actor = *hostile.actor;
        const auto bearing = BearingTo(player.GetPosition(), actor.GetPosition());
        const auto playerYaw = std::fmod(player.GetAngleZ() * radiansToDegrees + 360.0f, 360.0f);
        Json out{ { "refID", Hex(actor.GetFormID()) }, { "name", Name(actor.GetName()) },
            { "distance", Round(hostile.distance, 0) }, { "bearingDeg", Round(Wrap180(bearing - playerYaw), 0) },
            { "dz", Round(actor.GetPosition().z - player.GetPosition().z, 0) }, { "level", actor.GetLevel() },
            { "health", Vital(actor, RE::ActorValue::kHealth) }, { "inCombat", actor.IsInCombat() },
            { "targetsPlayer", actor.GetActorRuntimeData().currentCombatTarget.get().get() == &player },
            { "attack", AttackStateName(actor.AsActorState()->GetAttackState()) },
            { "blocking", actor.IsBlocking() } };
        if (const auto race = actor.GetRace()) {
            out["race"] = Name(race->GetName());
        }
        if (const auto chosen = chosenTarget.get(); chosen && chosen.get() == hostile.actor) {
            out["chosen"] = true;
        }
        return out;
    }

    // Turns the player to look at the middle of the body of `ref`.
    Json FaceRef(RE::PlayerCharacter& player, RE::TESObjectREFR& ref)
    {
        const auto from = player.GetPosition();
        const auto to = ref.GetPosition();
        const auto heading = BearingTo(from, to);
        const auto flat = std::hypot(to.x - from.x, to.y - from.y);
        // Eye height is about 120 units for a player; aim for the chest of the target.
        const auto dz = (to.z + ref.GetHeight() * 0.6f) - (from.z + 120.0f);
        const auto pitch = std::atan2(-dz, std::max(flat, 1.0f));
        player.SetHeading(heading / radiansToDegrees);
        player.SetLooking(std::clamp(pitch, -1.4f, 1.4f));
        return Json{ { "headingDeg", Round(std::fmod(heading + 360.0f, 360.0f)) }, { "pitchDeg", Round(pitch * radiansToDegrees) } };
    }

    // refID wins; otherwise the remembered target if it is still a living hostile; otherwise the nearest.
    RE::Actor* ResolveTarget(const Json& params, RE::PlayerCharacter& player, float radius)
    {
        if (params.contains("refID") && !params["refID"].is_null()) {
            const auto ref = RequireRef(params, "refID");
            const auto actor = ref->As<RE::Actor>();
            if (!actor || actor->IsDead()) {
                throw RpcError(code::notFound, "That reference is not a living actor");
            }
            return actor;
        }
        if (const auto chosen = chosenTarget.get()) {
            if (const auto actor = chosen->As<RE::Actor>(); actor && IsHostile(*actor, player)) {
                return actor;
            }
        }
        const auto hostiles = FindHostiles(player, radius);
        return hostiles.empty() ? nullptr : hostiles.front().actor;
    }

    RE::BGSEquipSlot* HandSlot(bool left)
    {
        const auto manager = RE::BGSDefaultObjectManager::GetSingleton();
        return manager ? manager->GetObject<RE::BGSEquipSlot>(left ? RE::DEFAULT_OBJECT::kLeftHandEquip : RE::DEFAULT_OBJECT::kRightHandEquip)
                       : nullptr;
    }

    bool IsLeft(const Json& params, const char* key, bool fallbackLeft = false)
    {
        const auto hand = Lower(Optional<std::string>(params, key, fallbackLeft ? "left" : "right"));
        if (hand != "left" && hand != "right") {
            throw RpcError(code::invalidParams, std::string(key) + " is left or right");
        }
        return hand == "left";
    }

    void RequireCombatReady(RE::PlayerCharacter& player)
    {
        if (player.IsDead()) {
            throw RpcError(code::notReady, "The player is dead");
        }
    }

    // Finds an inventory item by form, editor ID or a case-insensitive part of its display name.
    RE::TESBoundObject* FindInventoryItem(RE::PlayerCharacter& player, const Json& params)
    {
        if (params.contains("formID") || params.contains("editorID")) {
            return RequireForm(params)->As<RE::TESBoundObject>();
        }
        const auto wanted = Lower(Require<std::string>(params, "name"));
        RE::TESBoundObject* best = nullptr;
        for (const auto& [object, data] : player.GetInventory()) {
            if (data.first <= 0) {
                continue;
            }
            const char* display = data.second ? data.second->GetDisplayName() : object->GetName();
            const auto text = Lower(display && *display ? display : object->GetName());
            if (text == wanted) {
                return object;
            }
            if (!best && text.find(wanted) != std::string::npos) {
                best = object;
            }
        }
        return best;
    }

    RE::SpellItem* FindSpell(RE::PlayerCharacter& player, const Json& params)
    {
        std::vector<RE::SpellItem*> all;
        for (const auto spell : player.GetActorRuntimeData().addedSpells) {
            all.push_back(spell);
        }
        const auto addList = [&](RE::TESSpellList::SpellData* data) {
            for (std::uint32_t i = 0; data && i < data->numSpells; ++i) {
                all.push_back(data->spells[i]);
            }
        };
        if (const auto base = player.GetActorBase()) {
            addList(base->GetSpellList());
        }
        if (params.contains("formID") || params.contains("editorID")) {
            const auto form = RequireForm(params);
            return form->As<RE::SpellItem>();
        }
        const auto wanted = Lower(Require<std::string>(params, "name"));
        for (const auto spell : all) {
            if (spell && spell->GetName() && Lower(spell->GetName()) == wanted) {
                return spell;
            }
        }
        for (const auto spell : all) {
            if (spell && spell->GetName() && Lower(spell->GetName()).find(wanted) != std::string::npos) {
                return spell;
            }
        }
        return nullptr;
    }

    std::optional<RE::ActorValue> PotionKind(const std::string& kind)
    {
        if (kind == "health") {
            return RE::ActorValue::kHealth;
        }
        if (kind == "magicka") {
            return RE::ActorValue::kMagicka;
        }
        if (kind == "stamina") {
            return RE::ActorValue::kStamina;
        }
        return std::nullopt;
    }

    // The restoring magnitude of a potion for one vital, or 0 when it restores something else.
    float RestoreMagnitude(RE::AlchemyItem& potion, RE::ActorValue av)
    {
        if (potion.IsPoison() || potion.IsFood()) {
            return 0.0f;
        }
        float total = 0.0f;
        for (const auto effect : potion.effects) {
            if (!effect || !effect->baseEffect) {
                continue;
            }
            const auto& data = effect->baseEffect->data;
            if (data.primaryAV == av && data.archetype == RE::EffectSetting::Archetype::kValueModifier &&
                !data.flags.any(RE::EffectSetting::EffectSettingData::Flag::kDetrimental)) {
                total += effect->effectItem.magnitude;
            }
        }
        return total;
    }

    // What combat_unequip took off, per hand, so drawing again brings the weapon back instead of fists.
    RE::FormID unequippedRight = 0;
    RE::FormID unequippedLeft = 0;

    void RestoreUnequipped(RE::PlayerCharacter& player)
    {
        const auto manager = RE::ActorEquipManager::GetSingleton();
        if (!manager) {
            return;
        }
        const auto restore = [&](RE::FormID& id, bool left) {
            if (!id) {
                return;
            }
            const auto object = RE::TESForm::LookupByID<RE::TESBoundObject>(id);
            id = 0;
            if (object && !player.GetEquippedObject(left) && player.GetInventoryCounts().contains(object)) {
                manager->EquipObject(&player, object, nullptr, 1, HandSlot(left), true, false, true, false);
            }
        };
        restore(unequippedRight, false);
        restore(unequippedLeft, true);
    }

    Json PlayerVitals(RE::PlayerCharacter& player)
    {
        return Json{ { "health", Vital(player, RE::ActorValue::kHealth) }, { "magicka", Vital(player, RE::ActorValue::kMagicka) },
            { "stamina", Vital(player, RE::ActorValue::kStamina) } };
    }
}

DEVTOOLS_MODULE(Combat)
{
    Register("Combat.state", [](const Json& params) {
        AllowOnly(params, { "radius", "limit" });
        auto& player = RequirePlayer();
        const auto radius = std::clamp(Optional<float>(params, "radius", defaultRadius), 100.0f, 20000.0f);
        const auto limit = std::clamp(Optional<int>(params, "limit", 10), 1, 50);
        const auto state = player.AsActorState();

        Json self = PlayerVitals(player);
        self["dead"] = player.IsDead();
        self["level"] = player.GetLevel();
        self["inCombat"] = player.IsInCombat();
        self["weapon"] = WeaponStateName(state->GetWeaponState());
        self["drawn"] = state->IsWeaponDrawn();
        self["blocking"] = player.IsBlocking();
        self["sneaking"] = player.IsSneaking();
        self["attack"] = AttackStateName(state->GetAttackState());
        self["rightHand"] = Equipped(player.GetEquippedObject(false));
        self["leftHand"] = Equipped(player.GetEquippedObject(true));

        const auto hostiles = FindHostiles(player, radius);
        auto list = Json::array();
        for (const auto& hostile : hostiles) {
            if (static_cast<int>(list.size()) >= limit) {
                break;
            }
            list.push_back(DescribeHostile(hostile, player));
        }
        Json out{ { "player", std::move(self) }, { "hostileTotal", hostiles.size() }, { "hostiles", std::move(list) } };
        if (const auto chosen = chosenTarget.get()) {
            out["target"] = Hex(chosen->GetFormID());
        }
        return out;
    }, { .thread = Thread::Main });

    Register("Combat.target", [](const Json& params) {
        AllowOnly(params, { "refID", "radius", "face", "clear" });
        auto& player = RequirePlayer();
        if (Optional<bool>(params, "clear", false)) {
            chosenTarget = {};
            return Json{ { "target", nullptr } };
        }
        const auto radius = std::clamp(Optional<float>(params, "radius", defaultRadius), 100.0f, 20000.0f);
        const bool explicitRef = params.contains("refID") && !params["refID"].is_null();
        RE::Actor* actor = nullptr;
        if (explicitRef) {
            actor = ResolveTarget(params, player, radius);
        } else {
            // "Nearest" ignores the remembered target on purpose, so the call re-picks as enemies move.
            const auto hostiles = FindHostiles(player, radius);
            actor = hostiles.empty() ? nullptr : hostiles.front().actor;
        }
        if (!actor) {
            chosenTarget = {};
            return Json{ { "target", nullptr }, { "reason", "no hostile within range" } };
        }
        chosenTarget = actor->GetHandle();
        Json out{ { "target", Hex(actor->GetFormID()) }, { "name", Name(actor->GetName()) },
            { "distance", Round(player.GetPosition().GetDistance(actor->GetPosition()), 0) },
            { "health", Vital(*actor, RE::ActorValue::kHealth) } };
        if (Optional<bool>(params, "face", true)) {
            out["facing"] = FaceRef(player, *actor);
        }
        return out;
    }, { .thread = Thread::Main });

    Register("Combat.face", [](const Json& params) {
        AllowOnly(params, { "refID", "radius" });
        auto& player = RequirePlayer();
        const auto actor = ResolveTarget(params, player, std::clamp(Optional<float>(params, "radius", defaultRadius), 100.0f, 20000.0f));
        if (!actor) {
            throw RpcError(code::notFound, "No hostile to face");
        }
        return Json{ { "target", Hex(actor->GetFormID()) }, { "facing", FaceRef(player, *actor) },
            { "distance", Round(player.GetPosition().GetDistance(actor->GetPosition()), 0) } };
    }, { .thread = Thread::Main });

    Register("Combat.draw", [](const Json& params) {
        AllowOnly(params, { "on" });
        auto& player = RequirePlayer();
        RequireCombatReady(player);
        const bool on = Optional<bool>(params, "on", true);
        if (on) {
            RestoreUnequipped(player);
        }
        player.DrawWeaponMagicHands(on);
        return Json{ { "requested", on ? "draw" : "sheathe" }, { "weapon", WeaponStateName(player.AsActorState()->GetWeaponState()) } };
    }, { .thread = Thread::Main });

    Register("Combat.equip", [](const Json& params) {
        AllowOnly(params, { "name", "formID", "editorID", "hand" });
        auto& player = RequirePlayer();
        RequireCombatReady(player);
        const auto manager = RE::ActorEquipManager::GetSingleton();
        if (!manager) {
            throw RpcError(code::notReady, "The equip manager is not available yet");
        }
        const auto hand = Lower(Optional<std::string>(params, "hand", "right"));
        if (hand != "left" && hand != "right" && hand != "both") {
            throw RpcError(code::invalidParams, "hand is left, right or both");
        }
        const auto slot = hand == "both" ? nullptr : HandSlot(hand == "left");

        // Spells first when the caller did not name an inventory item.
        if (!params.contains("formID") && !params.contains("editorID")) {
            if (const auto spell = FindSpell(player, params); spell && !FindInventoryItem(player, params)) {
                manager->EquipSpell(&player, spell, slot);
                return Json{ { "equipped", Equipped(spell) }, { "hand", hand }, { "kind", "spell" }, { "queued", true } };
            }
        } else if (const auto form = RequireForm(params); form->Is(RE::FormType::Spell)) {
            manager->EquipSpell(&player, form->As<RE::SpellItem>(), slot);
            return Json{ { "equipped", Equipped(form) }, { "hand", hand }, { "kind", "spell" }, { "queued", true } };
        }

        const auto object = FindInventoryItem(player, params);
        if (!object) {
            throw RpcError(code::notFound, "Nothing in the inventory or spell list matches");
        }
        const bool handItem = object->Is(RE::FormType::Weapon);
        manager->EquipObject(&player, object, nullptr, 1, handItem ? slot : nullptr, true, false, true, false);
        return Json{ { "equipped", Equipped(object) }, { "hand", handItem ? hand : "worn" }, { "kind", FormTypeName(object) }, { "queued", true } };
    }, { .thread = Thread::Main });

    Register("Combat.unequip", [](const Json& params) {
        AllowOnly(params, { "hand" });
        auto& player = RequirePlayer();
        const auto manager = RE::ActorEquipManager::GetSingleton();
        if (!manager) {
            throw RpcError(code::notReady, "The equip manager is not available yet");
        }
        const bool left = IsLeft(params, "hand");
        const auto current = player.GetEquippedObject(left);
        if (!current) {
            return Json{ { "unequipped", nullptr } };
        }
        if (const auto spell = current->As<RE::SpellItem>()) {
            // Spells leave a hand by being replaced, not removed; the engine has no direct unequip for them.
            throw RpcError(code::invalidParams, "A spell cannot be unequipped; equip a weapon or fists over it");
        }
        (left ? unequippedLeft : unequippedRight) = current->GetFormID();
        manager->UnequipObject(&player, current->As<RE::TESBoundObject>(), nullptr, 1, HandSlot(left));
        return Json{ { "unequipped", Equipped(current) }, { "queued", true } };
    }, { .thread = Thread::Main });

    // kind light taps the attack button; power holds it past the engine's power-attack threshold.
    RegisterDeferred("Combat.attack", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "kind", "hand", "face", "refID", "holdMs" });
        auto& player = RequirePlayer();
        RequireCombatReady(player);
        const auto kind = Lower(Optional<std::string>(params, "kind", "light"));
        if (kind != "light" && kind != "power") {
            throw RpcError(code::invalidParams, "kind is light or power");
        }
        const bool left = IsLeft(params, "hand");
        const auto hold = std::chrono::milliseconds(
            std::clamp<std::int64_t>(Optional<std::int64_t>(params, "holdMs", kind == "power" ? 900 : 120), 20, 5000));
        Json out{ { "kind", kind }, { "hand", left ? "left" : "right" }, { "holdMs", hold.count() },
            { "wasDrawn", player.AsActorState()->IsWeaponDrawn() } };
        if (Optional<bool>(params, "face", true)) {
            if (const auto actor = ResolveTarget(params, player, defaultRadius)) {
                out["facing"] = FaceRef(player, *actor);
                out["target"] = Hex(actor->GetFormID());
            }
        }
        HoldControl(left ? leftControl : rightControl, hold, std::make_shared<Reply>([reply, out](Json result) mutable {
            if (result.contains("result")) {
                out["input"] = result["result"];
                reply->Ok(std::move(out));
            } else {
                reply->Fail(result["error"].value("code", code::notFound), result["error"].value("message", "input failed"));
            }
        }));
    }, { .thread = Thread::Main, .timeout = 15s });

    RegisterDeferred("Combat.block", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "holdMs", "face", "refID" });
        auto& player = RequirePlayer();
        RequireCombatReady(player);
        const auto hold = std::chrono::milliseconds(std::clamp<std::int64_t>(Optional<std::int64_t>(params, "holdMs", 1500), 50, 8000));
        Json out{ { "holdMs", hold.count() }, { "leftHand", Equipped(player.GetEquippedObject(true)) } };
        if (Optional<bool>(params, "face", true)) {
            if (const auto actor = ResolveTarget(params, player, defaultRadius)) {
                FaceRef(player, *actor);
            }
        }
        HoldControl(leftControl, hold, std::make_shared<Reply>([reply, out](Json result) mutable {
            if (result.contains("result")) {
                reply->Ok(std::move(out));
            } else {
                reply->Fail(result["error"].value("code", code::notFound), result["error"].value("message", "input failed"));
            }
        }));
    }, { .thread = Thread::Main, .timeout = 15s });

    // Casts whatever spell is in the chosen hand: fire-and-forget spells need a short press, concentration
    // spells (flames, sparks) last as long as the button is held.
    RegisterDeferred("Combat.cast", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "hand", "holdMs", "face", "refID" });
        auto& player = RequirePlayer();
        RequireCombatReady(player);
        const bool left = IsLeft(params, "hand");
        const auto spell = player.GetEquippedObject(left) ? player.GetEquippedObject(left)->As<RE::SpellItem>() : nullptr;
        if (!spell) {
            throw RpcError(code::invalidParams, "No spell is equipped in that hand (Combat.equip a spell first)");
        }
        const auto cost = spell->CalculateMagickaCost(&player);
        const auto magicka = player.AsActorValueOwner()->GetActorValue(RE::ActorValue::kMagicka);
        if (magicka < cost && spell->GetCastingType() != RE::MagicSystem::CastingType::kConcentration) {
            throw RpcError(code::notReady, "Not enough magicka for that spell (" + std::to_string(static_cast<int>(magicka)) + " < " +
                                               std::to_string(static_cast<int>(cost)) + ")");
        }
        const bool concentration = spell->GetCastingType() == RE::MagicSystem::CastingType::kConcentration;
        const auto hold = std::chrono::milliseconds(
            std::clamp<std::int64_t>(Optional<std::int64_t>(params, "holdMs", concentration ? 2000 : 250), 50, 8000));
        Json out{ { "spell", Equipped(spell) }, { "concentration", concentration }, { "holdMs", hold.count() },
            { "magickaBefore", Round(magicka, 0) } };
        if (Optional<bool>(params, "face", true)) {
            if (const auto actor = ResolveTarget(params, player, defaultRadius)) {
                FaceRef(player, *actor);
                out["target"] = Hex(actor->GetFormID());
            }
        }
        HoldControl(left ? leftControl : rightControl, hold, std::make_shared<Reply>([reply, out](Json result) mutable {
            if (result.contains("result")) {
                reply->Ok(std::move(out));
            } else {
                reply->Fail(result["error"].value("code", code::notFound), result["error"].value("message", "input failed"));
            }
        }));
    }, { .thread = Thread::Main, .timeout = 15s });

    // Drinks the weakest potion that covers the missing amount of the vital, or the strongest one when none does.
    Register("Combat.drink", [](const Json& params) {
        AllowOnly(params, { "kind", "name", "formID", "editorID" });
        auto& player = RequirePlayer();
        RequireCombatReady(player);
        const auto manager = RE::ActorEquipManager::GetSingleton();
        if (!manager) {
            throw RpcError(code::notReady, "The equip manager is not available yet");
        }
        RE::AlchemyItem* chosen = nullptr;
        std::string kind = Lower(Optional<std::string>(params, "kind", ""));
        if (params.contains("name") || params.contains("formID") || params.contains("editorID")) {
            const auto item = FindInventoryItem(player, params);
            chosen = item ? item->As<RE::AlchemyItem>() : nullptr;
            if (!chosen) {
                throw RpcError(code::notFound, "No such potion in the inventory");
            }
        } else {
            const auto av = PotionKind(kind);
            if (!av) {
                throw RpcError(code::invalidParams, "Pass kind health, magicka or stamina, or a potion by name or form");
            }
            const auto values = player.AsActorValueOwner();
            const auto missing = player.GetActorValueMax(*av) - values->GetActorValue(*av);
            float chosenMagnitude = 0.0f;
            for (const auto& [object, data] : player.GetInventory([](RE::TESBoundObject& o) { return o.Is(RE::FormType::AlchemyItem); })) {
                const auto potion = data.first > 0 ? object->As<RE::AlchemyItem>() : nullptr;
                const auto magnitude = potion ? RestoreMagnitude(*potion, *av) : 0.0f;
                if (magnitude <= 0.0f) {
                    continue;
                }
                const bool covers = magnitude >= missing;
                const bool chosenCovers = chosenMagnitude >= missing;
                const bool better = !chosen || (covers && !chosenCovers) || (covers && chosenCovers && magnitude < chosenMagnitude) ||
                                    (!covers && !chosenCovers && magnitude > chosenMagnitude);
                if (better) {
                    chosen = potion;
                    chosenMagnitude = magnitude;
                }
            }
            if (!chosen) {
                throw RpcError(code::notFound, "No " + kind + " potion in the inventory");
            }
        }
        const auto countOf = [&] {
            const auto counts = player.GetInventoryCounts([&](RE::TESBoundObject& other) { return &other == chosen; });
            const auto found = counts.find(chosen);
            return found == counts.end() ? 0 : found->second;
        };
        const auto before = countOf();
        // applyNow makes the engine consume the potion in this call instead of queueing the equip.
        manager->EquipObject(&player, chosen, nullptr, 1, nullptr, true, false, true, true);
        Json out{ { "potion", Equipped(chosen) }, { "countBefore", before }, { "countAfter", countOf() } };
        out["player"] = PlayerVitals(player);
        return out;
    }, { .thread = Thread::Main });
}
