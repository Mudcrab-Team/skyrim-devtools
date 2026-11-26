// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
#include "state_common.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <format>

namespace devtools::view
{
    double Round(double value, int decimals)
    {
        const auto scale = std::pow(10.0, decimals);
        return std::round(value * scale) / scale;
    }

    std::string Hex(RE::FormID id)
    {
        return std::format("{:08X}", id);
    }

    Json Name(const char* text)
    {
        return text && *text ? Json(text) : Json(nullptr);
    }

    Json Vec3(const RE::NiPoint3& point)
    {
        return Json::array({ Round(point.x), Round(point.y), Round(point.z) });
    }

    RE::FormID ParseFormId(const Json& value, const char* key)
    {
        if (value.is_number_unsigned()) {
            return value.get<RE::FormID>();
        }
        if (value.is_string()) {
            const auto text = value.get<std::string>();
            const auto digits = text.starts_with("0x") || text.starts_with("0X") ? text.substr(2) : text;
            if (!digits.empty() && digits.size() <= 8 && std::ranges::all_of(digits, [](unsigned char c) { return std::isxdigit(c); })) {
                return static_cast<RE::FormID>(std::strtoul(digits.c_str(), nullptr, 16));
            }
        }
        throw RpcError(code::invalidParams, std::string("'") + key + "' is a hex form ID such as \"0x14\"");
    }

    RE::TESForm* FindByEditorId(std::string_view editorId)
    {
        if (const auto found = RE::TESForm::LookupByEditorID(editorId)) {
            return found;
        }
        // The engine only keeps runtime editor IDs for some record types (quests, globals, cells, ...),
        // and the editor-ID map is not always populated, so fall back to a scan.
        const auto& [forms, lock] = RE::TESForm::GetAllForms();
        if (!forms) {
            return nullptr;
        }
        const RE::BSReadLockGuard guard{ lock };
        for (const auto& [id, form] : *forms) {
            if (form) {
                const auto candidate = form->GetFormEditorID();
                if (candidate && editorId == candidate) {
                    return form;
                }
            }
        }
        return nullptr;
    }

    RE::TESForm* RequireForm(const Json& params)
    {
        const bool hasId = params.contains("formID");
        const bool hasEditor = params.contains("editorID");
        if (hasId == hasEditor) {
            throw RpcError(code::invalidParams, "Pass exactly one of formID or editorID");
        }
        RE::TESForm* form = nullptr;
        if (hasId) {
            form = RE::TESForm::LookupByID(ParseFormId(params["formID"], "formID"));
        } else {
            const auto editorId = Require<std::string>(params, "editorID");
            form = FindByEditorId(editorId);
        }
        if (!form) {
            throw RpcError(code::notFound, "No such form");
        }
        return form;
    }

    RE::TESObjectREFR* RequireRef(const Json& params, const char* key)
    {
        const auto form = RE::TESForm::LookupByID(ParseFormId(Require<Json>(params, key), key));
        const auto ref = form ? form->AsReference() : nullptr;
        if (!ref) {
            throw RpcError(code::notFound, "No such reference");
        }
        return ref;
    }

    std::string FormTypeName(const RE::TESForm* form)
    {
        return form ? std::string(RE::FormTypeToString(form->GetFormType())) : std::string();
    }

    std::string RefCategory(RE::TESObjectREFR* ref)
    {
        if (ref->As<RE::Actor>()) {
            return "actor";
        }
        const auto base = ref->GetBaseObject();
        if (!base) {
            return "unknown";
        }
        switch (base->GetFormType()) {
        case RE::FormType::Container:
            return "container";
        case RE::FormType::Door:
            return "door";
        case RE::FormType::Activator:
        case RE::FormType::TalkingActivator:
            return "activator";
        case RE::FormType::Furniture:
            return "furniture";
        case RE::FormType::Flora:
        case RE::FormType::Tree:
            return "flora";
        case RE::FormType::Static:
        case RE::FormType::MovableStatic:
            return "static";
        case RE::FormType::Weapon:
        case RE::FormType::Armor:
        case RE::FormType::Ammo:
        case RE::FormType::Misc:
        case RE::FormType::Book:
        case RE::FormType::Ingredient:
        case RE::FormType::AlchemyItem:
        case RE::FormType::KeyMaster:
        case RE::FormType::Scroll:
        case RE::FormType::SoulGem:
        case RE::FormType::Note:
            return "item";
        default: {
            auto name = FormTypeName(base);
            std::ranges::transform(name, name.begin(), [](unsigned char c) { return std::tolower(c); });
            return name;
        }
        }
    }

    const char* RefName(RE::TESObjectREFR* ref)
    {
        auto name = ref->GetDisplayFullName();
        if (!name || !*name) {
            name = ref->GetName();
        }
        return name;
    }

    Json DescribeForm(RE::TESForm* form)
    {
        Json out{ { "formID", Hex(form->GetFormID()) }, { "type", FormTypeName(form) } };
        if (const auto editorId = form->GetFormEditorID(); editorId && *editorId) {
            out["editorID"] = editorId;
        }
        if (const auto name = form->GetName(); name && *name) {
            out["name"] = name;
        }
        if (const auto file = form->GetFile(0)) {
            out["file"] = std::string(file->GetFilename());
        }
        if (form->As<RE::TESBoundObject>()) {
            out["value"] = form->GetGoldValue();
            if (const auto weight = form->GetWeight(); weight > 0) {
                out["weight"] = Round(weight, 2);
            }
        }
        if (const auto ref = form->AsReference()) {
            if (const auto base = ref->GetBaseObject()) {
                out["base"] = Hex(base->GetFormID());
            }
        }
        if (const auto cell = form->As<RE::TESObjectCELL>()) {
            out["interior"] = cell->IsInteriorCell();
        }
        return out;
    }

    Json DescribeRef(RE::TESObjectREFR* ref, RE::TESObjectREFR* from)
    {
        Json out{ { "refID", Hex(ref->GetFormID()) }, { "type", RefCategory(ref) } };
        if (const auto base = ref->GetBaseObject()) {
            out["baseID"] = Hex(base->GetFormID());
            out["form"] = FormTypeName(base);
        }
        out["name"] = Name(RefName(ref));
        if (from) {
            out["distance"] = Round(from->GetPosition().GetDistance(ref->GetPosition()));
        }
        out["pos"] = Vec3(ref->GetPosition());
        if (ref->IsDisabled()) {
            out["disabled"] = true;
        }
        if (const auto actor = ref->As<RE::Actor>()) {
            out["level"] = actor->GetLevel();
            const auto owner = actor->AsActorValueOwner();
            const auto maxHealth = actor->GetActorValueMax(RE::ActorValue::kHealth);
            out["healthPct"] = maxHealth > 0 ? Round(owner->GetActorValue(RE::ActorValue::kHealth) * 100.0 / maxHealth, 0) : 0;
            out["dead"] = actor->IsDead();
            out["inCombat"] = actor->IsInCombat();
            if (const auto player = RE::PlayerCharacter::GetSingleton(); player && actor != player) {
                out["hostile"] = actor->IsHostileToActor(player);
            }
            if (const auto race = actor->GetRace()) {
                out["race"] = Name(race->GetName());
            }
        } else if (const auto category = out["type"].get<std::string>(); category == "door") {
            AddDoorDetails(ref, out);
        } else if (category == "container") {
            if (ref->IsLocked()) {
                out["locked"] = true;
            }
        }
        return out;
    }

    Json DescribeCell(RE::TESObjectCELL* cell)
    {
        if (!cell) {
            return nullptr;
        }
        Json out{ { "formID", Hex(cell->GetFormID()) }, { "name", Name(cell->GetName()) },
            { "editorID", Name(cell->GetFormEditorID()) }, { "interior", cell->IsInteriorCell() } };
        if (cell->IsExteriorCell()) {
            if (const auto coords = cell->GetCoordinates()) {
                out["grid"] = Json::array({ coords->cellX, coords->cellY });
            }
        }
        return out;
    }

    RE::PlayerCharacter& RequirePlayer()
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player || !player->Is3DLoaded()) {
            throw RpcError(code::notReady, "No playable world is loaded");
        }
        return *player;
    }
}
