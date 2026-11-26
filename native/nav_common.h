// SPDX-License-Identifier: GPL-3.0-or-later
// Shared pieces of the movement, camera, navigation and recording modules. Everything here runs
// on the main thread: RPC handlers on Thread::Main and the per-poll driver both live there.
#pragma once

#include "game.h"
#include "input.h"
#include "stuck.h"

#include <cmath>
#include <format>
#include <functional>
#include <mutex>
#include <numbers>
#include <optional>
#include <string>

namespace devtools::nav
{
    constexpr double pi = std::numbers::pi;
    inline double Deg(double radians) { return radians * 180.0 / pi; }
    inline double Rad(double degrees) { return degrees * pi / 180.0; }

    // Wraps an angle in radians to (-pi, pi].
    inline double WrapPi(double angle)
    {
        angle = std::fmod(angle + pi, 2.0 * pi);
        if (angle < 0.0) {
            angle += 2.0 * pi;
        }
        return angle - pi;
    }

    // Skyrim headings start at +Y (north) and grow clockwise towards +X (east).
    inline double HeadingTo(const RE::NiPoint3& from, const RE::NiPoint3& to)
    {
        return std::atan2(static_cast<double>(to.x - from.x), static_cast<double>(to.y - from.y));
    }

    inline double Dist2D(const RE::NiPoint3& a, const RE::NiPoint3& b)
    {
        return std::hypot(static_cast<double>(a.x - b.x), static_cast<double>(a.y - b.y));
    }

    inline double Dist3D(const RE::NiPoint3& a, const RE::NiPoint3& b)
    {
        return std::sqrt(static_cast<double>((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y) +
                                             (a.z - b.z) * (a.z - b.z)));
    }

    inline double Round(double value, double scale = 10.0) { return std::round(value * scale) / scale; }

    inline Json Point(const RE::NiPoint3& p) { return Json::array({ Round(p.x), Round(p.y), Round(p.z) }); }

    inline std::string Hex(RE::FormID id) { return std::format("{:08X}", id); }

    // A form id as hex text ("0001A2B3", "0x1A2B3") or a number.
    inline RE::FormID ParseFormId(const Json& value, const char* what)
    {
        try {
            if (value.is_number_unsigned()) {
                return value.get<RE::FormID>();
            }
            if (value.is_string()) {
                auto text = value.get<std::string>();
                if (text.starts_with("0x") || text.starts_with("0X")) {
                    text = text.substr(2);
                }
                std::size_t used = 0;
                const auto id = std::stoul(text, &used, 16);
                if (used == text.size() && !text.empty()) {
                    return static_cast<RE::FormID>(id);
                }
            }
        } catch (const std::exception&) {
        }
        throw RpcError(code::invalidParams, std::string(what) + " is a form id such as \"0001A2B3\"");
    }

    inline RE::TESObjectREFR* RequireRef(const Json& value, const char* what = "refID")
    {
        const auto id = ParseFormId(value, what);
        const auto ref = RE::TESForm::LookupByID<RE::TESObjectREFR>(id);
        if (!ref) {
            throw RpcError(code::notFound, std::string("No reference ") + Hex(id));
        }
        return ref;
    }

    inline RE::PlayerCharacter& Player()
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            throw RpcError(code::notReady, "No player");
        }
        return *player;
    }

    inline Json DescribeRef(RE::TESObjectREFR* ref)
    {
        if (!ref) {
            return nullptr;
        }
        const auto name = ref->GetDisplayFullName();
        return { { "refID", Hex(ref->GetFormID()) }, { "name", name && *name ? Json(name) : Json(nullptr) },
            { "pos", Point(ref->GetPosition()) } };
    }

    inline Json DescribeCell(RE::TESObjectCELL* cell)
    {
        if (!cell) {
            return nullptr;
        }
        const auto editorId = cell->GetFormEditorID();
        const auto name = cell->GetName();
        Json out{ { "formId", Hex(cell->GetFormID()) }, { "editorId", editorId && *editorId ? Json(editorId) : Json(nullptr) },
            { "name", name && *name ? Json(name) : Json(nullptr) }, { "interior", cell->IsInteriorCell() } };
        return out;
    }

    inline Json DescribeWorld(RE::TESWorldSpace* world)
    {
        if (!world) {
            return nullptr;
        }
        const auto editorId = world->GetFormEditorID();
        return editorId && *editorId ? Json(editorId) : Json(Hex(world->GetFormID()));
    }

    // Drives the player once per input poll. Tickers run in registration order; each may add to
    // the drive and turn the player. One lock guards every module's state against RPC handlers.
    struct Tick
    {
        float dt;          // seconds since the previous poll
        double now;        // seconds on a steady clock
        input::Drive& drive;
    };
    using Ticker = std::function<void(Tick&)>;

    std::recursive_mutex& Lock();
    void AddTicker(Ticker ticker);
    double Now();

    // Heading/pitch writes that also keep the third-person camera behind the player, so that
    // "forward" moves where the player faces.
    void FaceHeading(RE::PlayerCharacter& player, double headingRad);
    void FacePitch(RE::PlayerCharacter& player, double pitchRad);

    // Movement intent shared by Move.walk and Nav.goto for recordings: the last poll's state.
    struct DriveState
    {
        std::string owner;  // "walk", "nav", "replay" or empty
        std::optional<double> targetHeading;
    };
    DriveState& CurrentDrive();

    // The active navigation process, summarised for recordings (null when idle).
    Json NavSummary();

    namespace stuck
    {
        // Options from a request's `stuck` member: false, or an object overriding the defaults.
        Config ParseConfig(const Json& params);
        DoorLog& Doors();
        // A loading screen, menu or dialogue is up, so standing still means nothing.
        bool Paused();
        // Position, facing, blocker guess and a screenshot hint for a stopped command; goal may be null.
        Json Report(const Verdict& verdict, RE::PlayerCharacter& player, const RE::NiPoint3* goal);
        void EmitStuck(const char* source, const Verdict& verdict, Json report);
        void EmitUnstuck(const char* source, const Verdict& verdict, const RE::NiPoint3& pos);
    }

    // Where a plugin run keeps its files (rpc.jsonl, screens/, recordings/).
    std::filesystem::path RunDir();
}
