// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Helpers the state-query modules share: form lookup, compact JSON views of forms and references.
#pragma once

#include "game.h"

#include <string>
#include <string_view>

namespace devtools::view
{
    // One decimal is enough for a playtester and keeps replies small.
    [[nodiscard]] double Round(double value, int decimals = 1);
    [[nodiscard]] std::string Hex(RE::FormID id);
    [[nodiscard]] Json Name(const char* text);  // null for a missing or empty string
    [[nodiscard]] Json Vec3(const RE::NiPoint3& point);

    // Accepts a hex string ("0x14", "00000014") or an integer.
    [[nodiscard]] RE::FormID ParseFormId(const Json& value, const char* key);
    // Resolves params.formID or params.editorID; throws not-found / invalid-params.
    [[nodiscard]] RE::TESForm* RequireForm(const Json& params);
    [[nodiscard]] RE::TESForm* FindByEditorId(std::string_view editorId);
    [[nodiscard]] RE::TESObjectREFR* RequireRef(const Json& params, const char* key);

    [[nodiscard]] std::string FormTypeName(const RE::TESForm* form);
    // actor, container, door, item, activator, furniture, flora, static, or the form type in lower case.
    [[nodiscard]] std::string RefCategory(RE::TESObjectREFR* ref);
    [[nodiscard]] const char* RefName(RE::TESObjectREFR* ref);

    [[nodiscard]] Json DescribeForm(RE::TESForm* form);
    // Compact reference view; actor extras are included for actors.
    [[nodiscard]] Json DescribeRef(RE::TESObjectREFR* ref, RE::TESObjectREFR* from);
    [[nodiscard]] Json DescribeCell(RE::TESObjectCELL* cell);
    // Adds locked/lockLevel and, for teleport doors, the destination (doors.cpp).
    void AddDoorDetails(RE::TESObjectREFR* ref, Json& out);

    [[nodiscard]] RE::PlayerCharacter& RequirePlayer();

    // The engine keeps the player's combat flag set for a while after the fight is over (a teleport or a load
    // leaves it behind), so "in combat" needs a living, loaded actor that is fighting the player right now.
    [[nodiscard]] int ActiveCombatants(RE::PlayerCharacter& player);
    // Ends combat and alarm state on the player and on everyone fighting them; returns how many actors were stopped.
    int ClearPlayerCombat(RE::PlayerCharacter& player);
}
