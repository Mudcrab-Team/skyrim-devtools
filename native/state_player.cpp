// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Player state, inventory, equipment, spells and simple player mutations.
#include "methods.h"
#include "state_common.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace std::chrono_literals;

    constexpr float radiansToDegrees = 57.29577951308232f;

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return std::tolower(c); });
        return text;
    }

    // The engine's by-name lookup is not safe for unknown names, so match against the enum's own names.
    RE::ActorValue ParseActorValue(const std::string& name)
    {
        const auto wanted = Lower(name);
        for (int i = 0; i < static_cast<int>(RE::ActorValue::kTotal); ++i) {
            const auto info = RE::ActorValueList::GetActorValueInfo(static_cast<RE::ActorValue>(i));
            if (info && info->enumName && Lower(info->enumName) == wanted) {
                return static_cast<RE::ActorValue>(i);
            }
        }
        throw RpcError(code::invalidParams, "Unknown actor value '" + name + "'");
    }

    const char* SpellKind(RE::MagicItem* item)
    {
        if (!item) {
            return "unknown";
        }
        switch (item->GetSpellType()) {
        case RE::MagicSystem::SpellType::kSpell: return "spell";
        case RE::MagicSystem::SpellType::kPower: return "power";
        case RE::MagicSystem::SpellType::kLesserPower: return "lesserPower";
        case RE::MagicSystem::SpellType::kAbility: return "ability";
        case RE::MagicSystem::SpellType::kDisease: return "disease";
        case RE::MagicSystem::SpellType::kVoicePower: return "voice";
        case RE::MagicSystem::SpellType::kScroll: return "scroll";
        default: return "other";
        }
    }

    Json DescribeSpell(RE::SpellItem* spell, RE::Actor* caster)
    {
        Json out{ { "formID", Hex(spell->GetFormID()) }, { "name", Name(spell->GetName()) }, { "kind", SpellKind(spell) } };
        if (const auto skill = spell->GetAssociatedSkill(); skill != RE::ActorValue::kNone) {
            if (const auto info = RE::ActorValueList::GetActorValueInfo(skill); info && info->enumName) {
                out["skill"] = info->enumName;
            }
        }
        if (caster && spell->GetSpellType() == RE::MagicSystem::SpellType::kSpell) {
            out["cost"] = Round(spell->CalculateMagickaCost(caster), 0);
        }
        return out;
    }

    Json DescribeEquipped(RE::TESForm* form)
    {
        if (!form) {
            return nullptr;
        }
        return { { "formID", Hex(form->GetFormID()) }, { "name", Name(form->GetName()) }, { "type", FormTypeName(form) } };
    }

    // Skyrim's filter vocabulary for inventory queries; empty matches everything.
    bool MatchesFilter(const std::string& filter, RE::TESBoundObject& object)
    {
        if (filter.empty() || filter == "all") {
            return true;
        }
        const auto type = Lower(FormTypeName(&object));
        static const std::map<std::string, std::string> aliases{ { "weapon", "weap" }, { "armor", "armo" }, { "ammo", "ammo" },
            { "potion", "alch" }, { "ingredient", "ingr" }, { "book", "book" }, { "misc", "misc" }, { "key", "keym" },
            { "scroll", "scrl" }, { "soulgem", "slgm" }, { "note", "note" } };
        if (const auto alias = aliases.find(filter); alias != aliases.end()) {
            return type == alias->second;
        }
        return type == filter;
    }

    Json DescribeEntry(RE::TESBoundObject* object, int count, RE::InventoryEntryData* entry)
    {
        Json item{ { "formID", Hex(object->GetFormID()) }, { "type", FormTypeName(object) }, { "count", count } };
        const char* name = entry ? entry->GetDisplayName() : object->GetName();
        item["name"] = Name(name && *name ? name : object->GetName());
        if (entry) {
            item["value"] = entry->GetValue();
            if (const auto weight = entry->GetWeight(); weight > 0) {
                item["weight"] = Round(weight, 2);
            }
            if (entry->IsWorn()) {
                item["equipped"] = true;
            }
        }
        return item;
    }

    Json Slots(RE::TESObjectARMO* armor)
    {
        auto slots = Json::array();
        const auto mask = static_cast<std::uint32_t>(armor->GetSlotMask().get());
        for (int bit = 0; bit < 32; ++bit) {
            if (mask & (1u << bit)) {
                slots.push_back(bit + 30);
            }
        }
        return slots;
    }
}

DEVTOOLS_MODULE(StatePlayer)
{
    Register("Player.state", [](const Json& params) {
        AllowOnly(params, { "avs" });
        auto& player = RequirePlayer();
        const auto values = player.AsActorValueOwner();
        const auto stat = [&](RE::ActorValue av) {
            return Json{ { "cur", Round(values->GetActorValue(av), 0) }, { "max", Round(player.GetActorValueMax(av), 0) } };
        };

        Json out;
        out["name"] = Name(player.GetName());
        if (const auto race = player.GetRace()) {
            out["race"] = Name(race->GetName());
        }
        out["level"] = player.GetLevel();
        out["pos"] = Vec3(player.GetPosition());
        out["rotDeg"] = Json{ { "yaw", Round(std::fmod(player.GetAngleZ() * radiansToDegrees + 360.0f, 360.0f)) },
            { "pitch", Round(player.GetAngleX() * radiansToDegrees) } };
        out["cell"] = DescribeCell(player.GetParentCell());
        if (const auto world = player.GetWorldspace()) {
            out["worldspace"] = Json{ { "formID", Hex(world->GetFormID()) }, { "name", Name(world->GetName()) },
                { "editorID", Name(world->GetFormEditorID()) } };
        } else {
            out["worldspace"] = nullptr;
        }
        if (const auto location = player.GetCurrentLocation()) {
            out["location"] = Name(location->GetName());
        }
        out["health"] = stat(RE::ActorValue::kHealth);
        out["magicka"] = stat(RE::ActorValue::kMagicka);
        out["stamina"] = stat(RE::ActorValue::kStamina);
        out["gold"] = player.GetGoldAmount();
        out["carry"] = Json{ { "cur", Round(values->GetActorValue(RE::ActorValue::kInventoryWeight)) },
            { "max", Round(values->GetActorValue(RE::ActorValue::kCarryWeight), 0) } };
        const auto state = player.AsActorState();
        out["sneaking"] = player.IsSneaking();
        out["sprinting"] = state->IsSprinting();
        out["swimming"] = state->IsSwimming();
        out["weaponDrawn"] = state->IsWeaponDrawn();
        const auto combatants = view::ActiveCombatants(player);
        out["inCombat"] = combatants > 0;
        out["combatFlag"] = player.IsInCombat();
        out["combatants"] = combatants;
        out["dead"] = player.IsDead();

        auto effects = Json::array();
        if (const auto list = player.AsMagicTarget()->GetActiveEffectList()) {
            for (const auto effect : *list) {
                if (!effect || effect->flags.any(RE::ActiveEffect::Flag::kInactive, RE::ActiveEffect::Flag::kDispelled)) {
                    continue;
                }
                const auto base = effect->effect ? effect->effect->baseEffect : nullptr;
                Json entry{ { "name", Name(base ? base->GetName() : nullptr) },
                    { "magnitude", Round(effect->magnitude) } };
                if (effect->spell && effect->spell->GetName() && base && std::string_view(effect->spell->GetName()) != base->GetName()) {
                    entry["source"] = Name(effect->spell->GetName());
                }
                entry["remaining"] = effect->duration > 0 ? Json(Round(effect->duration - effect->elapsedSeconds)) : Json(nullptr);
                effects.push_back(std::move(entry));
                if (effects.size() >= 40) {
                    break;
                }
            }
        }
        out["effects"] = std::move(effects);

        if (params.contains("avs")) {
            Json avs = Json::object();
            for (const auto& name : Require<std::vector<std::string>>(params, "avs")) {
                const auto av = ParseActorValue(name);
                avs[name] = Json{ { "cur", Round(values->GetActorValue(av)) }, { "base", Round(values->GetBaseActorValue(av)) } };
            }
            out["avs"] = std::move(avs);
        }
        return out;
    }, { .thread = Thread::Main });

    Register("Player.inventory", [](const Json& params) {
        AllowOnly(params, { "filter", "limit" });
        auto& player = RequirePlayer();
        const auto filter = Lower(Optional<std::string>(params, "filter", ""));
        const auto limit = std::clamp(Optional<int>(params, "limit", 100), 1, 500);
        const auto items = player.GetInventory([&](RE::TESBoundObject& object) { return MatchesFilter(filter, object); });
        auto list = Json::array();
        std::vector<const decltype(items)::value_type*> sorted;
        for (const auto& entry : items) {
            sorted.push_back(&entry);
        }
        std::ranges::sort(sorted, [](const auto* a, const auto* b) {
            const auto nameA = std::string(a->first->GetName());
            const auto nameB = std::string(b->first->GetName());
            return nameA < nameB;
        });
        for (const auto* entry : sorted) {
            if (static_cast<int>(list.size()) >= limit) {
                break;
            }
            if (entry->second.first <= 0) {
                continue;
            }
            list.push_back(DescribeEntry(entry->first, entry->second.first, entry->second.second.get()));
        }
        return Json{ { "total", sorted.size() }, { "items", std::move(list) } };
    }, { .thread = Thread::Main });

    Register("Player.equipped", [](const Json& params) {
        AllowOnly(params, {});
        auto& player = RequirePlayer();
        auto worn = Json::array();
        for (const auto& [object, entry] : player.GetInventory([](RE::TESBoundObject& object) {
                 return object.Is(RE::FormType::Armor) || object.Is(RE::FormType::Ammo);
             })) {
            if (entry.second && entry.second->IsWorn()) {
                auto item = DescribeEntry(object, entry.first, entry.second.get());
                if (const auto armor = object->As<RE::TESObjectARMO>()) {
                    item["slots"] = Slots(armor);
                    item["armorRating"] = Round(armor->armorRating / 100.0, 0);
                }
                worn.push_back(std::move(item));
            }
        }
        Json out{ { "rightHand", DescribeEquipped(player.GetEquippedObject(false)) },
            { "leftHand", DescribeEquipped(player.GetEquippedObject(true)) }, { "worn", std::move(worn) } };
        out["voice"] = DescribeEquipped(player.GetActorRuntimeData().selectedSpells[RE::Actor::SlotTypes::kPowerOrShout]);
        return out;
    }, { .thread = Thread::Main });

    Register("Player.spells", [](const Json& params) {
        AllowOnly(params, { "kind", "limit" });
        auto& player = RequirePlayer();
        const auto kind = Optional<std::string>(params, "kind", "");
        const auto limit = std::clamp(Optional<int>(params, "limit", 100), 1, 500);
        std::set<RE::FormID> seen;
        auto spells = Json::array();
        auto shouts = Json::array();
        const auto addSpell = [&](RE::SpellItem* spell) {
            if (spell && seen.insert(spell->GetFormID()).second) {
                auto entry = DescribeSpell(spell, &player);
                if (kind.empty() || entry["kind"] == kind) {
                    spells.push_back(std::move(entry));
                }
            }
        };
        for (const auto spell : player.GetActorRuntimeData().addedSpells) {
            addSpell(spell);
        }
        const auto addList = [&](RE::TESSpellList::SpellData* data) {
            if (!data) {
                return;
            }
            for (std::uint32_t i = 0; i < data->numSpells; ++i) {
                addSpell(data->spells[i]);
            }
            for (std::uint32_t i = 0; i < data->numShouts; ++i) {
                if (const auto shout = data->shouts[i]; shout && seen.insert(shout->GetFormID()).second) {
                    shouts.push_back({ { "formID", Hex(shout->GetFormID()) }, { "name", Name(shout->GetName()) } });
                }
            }
        };
        if (const auto base = player.GetActorBase()) {
            addList(base->GetSpellList());
        }
        if (const auto race = player.GetRace()) {
            addList(race->actorEffects);
        }
        const auto total = spells.size();
        if (static_cast<int>(total) > limit) {
            spells.erase(spells.begin() + limit, spells.end());
        }
        return Json{ { "total", total }, { "spells", std::move(spells) }, { "shouts", std::move(shouts) } };
    }, { .thread = Thread::Main });

    Register("Player.teleport", [](const Json& params) {
        AllowOnly(params, { "x", "y", "z", "refID", "yawDeg", "loadEpoch" });
        RequirePlayable(params);
        auto& player = RequirePlayer();
        if (params.contains("refID")) {
            if (params.contains("x") || params.contains("y") || params.contains("z")) {
                throw RpcError(code::invalidParams, "Pass either refID or x,y,z");
            }
            const auto target = RequireRef(params, "refID");
            // MoveTo is queued by the engine; the reply reports the destination, not the old position.
            player.MoveTo(target);
            return Json{ { "queued", true }, { "target", Hex(target->GetFormID()) }, { "targetPos", Vec3(target->GetPosition()) } };
        } else {
            const auto position = RE::NiPoint3(Require<float>(params, "x"), Require<float>(params, "y"), Require<float>(params, "z"));
            player.SetPosition(position, true);
        }
        if (params.contains("yawDeg")) {
            auto angle = player.GetAngle();
            angle.z = Require<float>(params, "yawDeg") / radiansToDegrees;
            player.SetAngle(angle);
        }
        return Json{ { "pos", Vec3(player.GetPosition()) }, { "cell", DescribeCell(player.GetParentCell()) } };
    }, { .thread = Thread::Main });

    Register("Player.setAV", [](const Json& params) {
        AllowOnly(params, { "av", "value", "loadEpoch" });
        RequirePlayable(params);
        auto& player = RequirePlayer();
        const auto name = Require<std::string>(params, "av");
        const auto av = ParseActorValue(name);
        const auto values = player.AsActorValueOwner();
        const auto before = values->GetActorValue(av);
        values->SetActorValue(av, Require<float>(params, "value"));
        return Json{ { "av", name }, { "before", Round(before) }, { "after", Round(values->GetActorValue(av)) } };
    }, { .thread = Thread::Main });

    const auto changeItem = [](const Json& params, bool adding) {
        AllowOnly(params, { "formID", "editorID", "count", "loadEpoch" });
        RequirePlayable(params);
        auto& player = RequirePlayer();
        const auto object = RequireForm(params)->As<RE::TESBoundObject>();
        if (!object) {
            throw RpcError(code::invalidParams, "That form is not an inventory item");
        }
        const auto count = Optional<int>(params, "count", 1);
        if (count < 1 || count > 1000000) {
            throw RpcError(code::invalidParams, "count is between 1 and 1000000");
        }
        const auto countOf = [&] {
            const auto counts = player.GetInventoryCounts([&](RE::TESBoundObject& other) { return &other == object; });
            const auto found = counts.find(object);
            return found == counts.end() ? 0 : found->second;
        };
        const auto before = countOf();
        if (adding) {
            player.AddObjectToContainer(object, nullptr, count, nullptr);
        } else {
            if (before < count) {
                throw RpcError(code::invalidParams, "The player has only " + std::to_string(before));
            }
            player.RemoveItem(object, count, RE::ITEM_REMOVE_REASON::kRemove, nullptr, nullptr);
        }
        return Json{ { "formID", Hex(object->GetFormID()) }, { "name", Name(object->GetName()) }, { "before", before }, { "after", countOf() } };
    };
    Register("Player.addItem", [changeItem](const Json& params) { return changeItem(params, true); }, { .thread = Thread::Main });
    Register("Player.removeItem", [changeItem](const Json& params) { return changeItem(params, false); }, { .thread = Thread::Main });
}
