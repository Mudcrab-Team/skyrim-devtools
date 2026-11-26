// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Perception for agents: what a reference is and what state it is in (World.inspect), where it
// is on screen (Screen.project), engine settings (Setting.get/set), and the blocker report that
// navigation failures carry.
#include "inspect.h"
#include "methods.h"
#include "state_common.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <numbers>
#include <regex>
#include <set>

// windows.h maps GetObject to GetObjectA, which hides BSScript::Variable::GetObject.
#undef GetObject

namespace
{
    using namespace devtools;
    using namespace devtools::view;

    constexpr double degPerRad = 180.0 / std::numbers::pi;

    Json Angles(const RE::NiPoint3& radians)
    {
        return Json{ { "x", Round(radians.x * degPerRad) }, { "y", Round(radians.y * degPerRad) }, { "z", Round(radians.z * degPerRad) } };
    }

    Json MatrixAngles(const RE::NiMatrix3& matrix)
    {
        RE::NiPoint3 angles;
        matrix.ToEulerAnglesXYZ(angles);
        return Angles(angles);
    }

    // Small enough to show in a reply: up to `limit` named nodes, breadth first, with the local
    // rotation, which is where animated parts (a turning pillar, an opening gate) move.
    Json Nodes(RE::NiAVObject* root, std::size_t limit)
    {
        Json out = Json::array();
        std::vector<std::pair<RE::NiAVObject*, int>> queue{ { root, 0 } };
        for (std::size_t i = 0; i < queue.size() && out.size() < limit; ++i) {
            const auto [object, depth] = queue[i];
            if (!object) {
                continue;
            }
            if (const char* name = object->name.c_str(); name && *name) {
                out.push_back(Json{ { "name", name }, { "depth", depth }, { "localRotDeg", MatrixAngles(object->local.rotate) },
                    { "localPos", Vec3(object->local.translate) } });
            }
            if (depth >= 4) {
                continue;
            }
            if (const auto node = object->AsNode()) {
                for (const auto& child : node->GetChildren()) {
                    if (child) {
                        queue.emplace_back(child.get(), depth + 1);
                    }
                }
            }
        }
        return out;
    }

    // Behaviour graph variables read through the engine's own accessor. The raw word is shown
    // as an integer when it is a small one, as a float otherwise, since the type table is not
    // exposed.
    Json GraphVariables(RE::TESObjectREFR* ref, std::size_t limit)
    {
        RE::BSTSmartPointer<RE::BSAnimationGraphManager> manager;
        if (!ref->GetAnimationGraphManager(manager) || !manager) {
            return nullptr;
        }
        Json out{ { "graphs", manager->graphs.size() } };
        Json vars = Json::object();
        for (const auto& graph : manager->graphs) {
            if (!graph) {
                continue;
            }
            if (const char* project = graph->projectName.c_str(); project && *project) {
                out["project"] = project;
            }
            const auto behavior = graph->behaviorGraph;
            const auto data = behavior ? behavior->data.get() : nullptr;
            const auto strings = data ? data->stringData.get() : nullptr;
            if (!strings) {
                continue;
            }
            for (const auto& name : strings->variableNames) {
                if (vars.size() >= limit) {
                    out["truncated"] = true;
                    break;
                }
                const char* text = name.c_str();
                if (!text || !*text) {
                    continue;
                }
                std::int32_t asInt = 0;
                float asFloat = 0.0F;
                if (ref->GetGraphVariableInt(text, asInt)) {
                    if (asInt > -1000000 && asInt < 1000000) {
                        vars[text] = asInt;
                    } else if (ref->GetGraphVariableFloat(text, asFloat) && std::isfinite(asFloat)) {
                        vars[text] = Round(asFloat, 3);
                    }
                }
            }
            break;  // the first graph drives the reference; further ones are layered copies
        }
        out["variables"] = std::move(vars);
        return out;
    }

    Json ScriptValue(const RE::BSScript::Variable& value)
    {
        if (value.IsBool()) {
            return value.GetBool();
        }
        if (value.IsInt()) {
            return value.GetSInt();
        }
        if (value.IsFloat()) {
            return Round(value.GetFloat(), 3);
        }
        if (value.IsString()) {
            return std::string(value.GetString());
        }
        if (value.IsNoneObject()) {
            return nullptr;
        }
        if (value.IsObject()) {
            if (const auto object = value.GetObject()) {
                Json out{ { "handle", std::format("{:X}", object->GetHandle()) } };
                if (const auto type = object->GetTypeInfo()) {
                    out["class"] = Name(type->GetName());
                }
                return out;
            }
            return nullptr;
        }
        if (value.IsArray()) {
            return "array";
        }
        return "?";
    }

    // Attached Papyrus scripts with their state and variables. This is engine knowledge a player
    // does not have; it exists for debugging mods, and playtest reports say when they used it.
    Json Scripts(RE::TESObjectREFR* ref)
    {
        const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
        if (!vm) {
            return nullptr;
        }
        const auto policy = vm->GetObjectHandlePolicy();
        if (!policy) {
            return nullptr;
        }
        const auto handle = policy->GetHandleForObject(ref->GetFormType(), ref);
        if (handle == policy->EmptyHandle()) {
            return Json::array();
        }
        Json out = Json::array();
        RE::BSSpinLockGuard guard(vm->attachedScriptsLock);
        const auto found = vm->attachedScripts.find(handle);
        if (found == vm->attachedScripts.end()) {
            return out;
        }
        for (const auto& attached : found->second) {
            const auto object = attached.get();
            if (!object) {
                continue;
            }
            const auto type = object->GetTypeInfo();
            Json script{ { "class", type ? Name(type->GetName()) : Json(nullptr) }, { "state", Name(object->currentState.c_str()) } };
            // Variables are laid out base class first, so walk the chain from the root.
            std::vector<const RE::BSScript::ObjectTypeInfo*> chain;
            for (auto t = type; t; t = t->GetParent()) {
                chain.push_back(t);
            }
            std::ranges::reverse(chain);
            Json vars = Json::object();
            std::uint32_t index = 0;
            for (const auto t : chain) {
                const auto infos = t->GetVariableIter();
                for (std::uint32_t i = 0; i < t->GetNumVariables(); ++i, ++index) {
                    if (vars.size() < 40 && infos && infos[i].name.c_str()) {
                        vars[infos[i].name.c_str()] = ScriptValue(object->variables[index]);
                    }
                }
            }
            script["variables"] = std::move(vars);
            out.push_back(std::move(script));
        }
        return out;
    }

    Json Linked(RE::TESObjectREFR* ref)
    {
        Json out = Json::array();
        if (const auto extra = ref->extraList.GetByType<RE::ExtraLinkedRef>()) {
            for (const auto& link : extra->linkedRefs) {
                if (!link.refr) {
                    continue;
                }
                Json entry{ { "refID", Hex(link.refr->GetFormID()) }, { "type", RefCategory(link.refr) }, { "name", Name(RefName(link.refr)) } };
                if (link.keyword) {
                    entry["keyword"] = Name(link.keyword->GetFormEditorID());
                }
                out.push_back(std::move(entry));
            }
        }
        return out;
    }

    Json RefList(RE::BSSimpleList<RE::REF_ACTIVATE_DATA*>& list)  // the const iterator does not compile
    {
        Json out = Json::array();
        for (const auto data : list) {
            if (!data) {
                continue;
            }
            if (const auto target = data->activateRef.get()) {
                out.push_back(Json{ { "refID", Hex(target->GetFormID()) }, { "type", RefCategory(target.get()) },
                    { "name", Name(RefName(target.get())) }, { "delay", Round(data->activateDelay, 2) } });
            }
        }
        return out;
    }

    Json Inspect(RE::TESObjectREFR* ref, bool withNodes, bool withGraph, bool withScripts)
    {
        auto& player = RequirePlayer();
        auto out = DescribeRef(ref, &player);
        const auto base = ref->GetBaseObject();
        if (base) {
            Json form{ { "formID", Hex(base->GetFormID()) }, { "type", FormTypeName(base) }, { "name", Name(base->GetName()) } };
            if (const auto editorId = base->GetFormEditorID(); editorId && *editorId) {
                form["editorID"] = editorId;
            }
            out["base"] = std::move(form);
        }
        if (const auto model = inspect::ModelPath(ref); !model.empty()) {
            out["model"] = inspect::Basename(model);
            out["modelPath"] = model;
        }
        out["angleDeg"] = Angles(ref->GetAngle());
        out["scale"] = Round(ref->GetScale(), 3);
        out["cell"] = DescribeCell(ref->GetParentCell());
        out["loaded"] = ref->Is3DLoaded();
        out["openState"] = inspect::OpenState(ref);
        out["locked"] = ref->IsLocked();
        out["destroyed"] = ref->IsDestroyed();
        out["activationBlocked"] = ref->IsActivationBlocked();
        if (const auto root = ref->Get3D()) {
            out["worldRotDeg"] = MatrixAngles(root->world.rotate);
            out["bound"] = Json{ { "center", Vec3(root->worldBound.center) }, { "radius", Round(root->worldBound.radius) } };
            if (withNodes) {
                out["nodes"] = Nodes(root, 24);
            }
        }
        bool sight = false;
        out["lineOfSight"] = player.HasLineOfSight(ref, sight);
        out["linkedRefs"] = Linked(ref);
        if (const auto parent = ref->extraList.GetByType<RE::ExtraEnableStateParent>()) {
            if (const auto p = parent->parent.get()) {
                out["enableParent"] = Json{ { "refID", Hex(p->GetFormID()) }, { "name", Name(RefName(p.get())) },
                    { "disabled", p->IsDisabled() }, { "oppositeState", (parent->flags & 1) != 0 } };
            }
        }
        if (const auto parents = ref->extraList.GetByType<RE::ExtraActivateRef>()) {
            out["activatedBy"] = RefList(parents->parents);
        }
        if (const auto children = ref->extraList.GetByType<RE::ExtraActivateRefChildren>()) {
            out["activates"] = RefList(children->children);
        }
        if (withGraph) {
            out["graph"] = GraphVariables(ref, 64);
        }
        if (withScripts) {
            out["scripts"] = Scripts(ref);
        }
        return out;
    }

    // Screen position of a world point: x right, y down, both 0..1 across the game viewport;
    // `inFront` is false behind the camera.
    struct Projected
    {
        double x = 0.0;
        double y = 0.0;
        bool inFront = false;
    };

    Projected Project(RE::NiCamera* camera, const RE::NiPoint3& point)
    {
        float x = 0.0F, y = 0.0F, z = 0.0F;
        // The world camera's port is the unit rectangle, so the result is already 0..1, y up.
        RE::NiCamera::WorldPtToScreenPt3(camera->GetRuntimeData().worldToCam, camera->GetRuntimeData2().port, point, x, y, z, 1e-5F);
        Projected p;
        p.x = x;
        p.y = 1.0 - y;
        p.inFront = z > 0.0F;
        return p;
    }

    bool Matches(const std::string& text, const std::regex& pattern) { return std::regex_search(text, pattern); }

    std::string NameOf(RE::TESObjectREFR* ref)
    {
        const auto name = RefName(ref);
        return name ? name : "";
    }

    RE::Setting* FindSetting(const std::string& name)
    {
        if (const auto ini = RE::INISettingCollection::GetSingleton()) {
            if (const auto s = ini->GetSetting(name)) {
                return s;
            }
        }
        if (const auto prefs = RE::INIPrefSettingCollection::GetSingleton()) {
            if (const auto s = prefs->GetSetting(name)) {
                return s;
            }
        }
        if (const auto game = RE::GameSettingCollection::GetSingleton()) {
            if (const auto s = game->GetSetting(name.c_str())) {
                return s;
            }
        }
        return nullptr;
    }

    Json SettingValue(RE::Setting* setting)
    {
        switch (setting->GetType()) {
        case RE::Setting::Type::kBool: return setting->GetBool();
        case RE::Setting::Type::kFloat: return Round(setting->GetFloat(), 4);
        case RE::Setting::Type::kInteger: return setting->GetInteger();
        case RE::Setting::Type::kCharacter: return setting->GetCharacter();
        case RE::Setting::Type::kUnsignedCharacter: return setting->GetUnsignedCharacter();
        case RE::Setting::Type::kUnsignedInteger: return setting->GetUnsignedInteger();
        case RE::Setting::Type::kColorRGB:
        case RE::Setting::Type::kColorRGBA: return setting->GetColor();
        case RE::Setting::Type::kString: return Name(setting->GetString());
        default: return nullptr;
        }
    }
}

namespace devtools::inspect
{
    std::string ModelPath(RE::TESObjectREFR* ref)
    {
        const auto base = ref ? ref->GetBaseObject() : nullptr;
        const auto model = base ? base->As<RE::TESModel>() : nullptr;
        const char* path = model ? model->GetModel() : nullptr;
        return path ? std::string(path) : std::string();
    }

    std::string Basename(const std::string& path)
    {
        const auto slash = path.find_last_of("\\/");
        return slash == std::string::npos ? path : path.substr(slash + 1);
    }

    Json OpenState(RE::TESObjectREFR* ref)
    {
        const auto base = ref ? ref->GetBaseObject() : nullptr;
        if (!base || !(base->Is(RE::FormType::Door) || base->Is(RE::FormType::Activator))) {
            return nullptr;
        }
        switch (RE::BGSOpenCloseForm::GetOpenState(ref)) {
        case RE::BGSOpenCloseForm::OPEN_STATE::kOpen: return "open";
        case RE::BGSOpenCloseForm::OPEN_STATE::kOpening: return "opening";
        case RE::BGSOpenCloseForm::OPEN_STATE::kClosed: return "closed";
        case RE::BGSOpenCloseForm::OPEN_STATE::kClosing: return "closing";
        default: return nullptr;
        }
    }

    Json Blockers(const RE::NiPoint3& end, const RE::NiPoint3& goal)
    {
        static const std::regex barrier("gate|portcullis|door|web|bars|grate|lever|pillar|wall|cage|rubble|collapse",
            std::regex::icase);
        static const std::regex loose("torch|candle|lantern|lamp|brazier|sconce|chandelier|clutter|light", std::regex::icase);
        const auto tes = RE::TES::GetSingleton();
        const auto player = RE::PlayerCharacter::GetSingleton();
        const double dx = goal.x - end.x, dy = goal.y - end.y;
        const double horizontal = std::hypot(dx, dy);
        Json out{ { "stop", Vec3(end) }, { "gap", Json{ { "horizontal", Round(horizontal) }, { "vertical", Round(goal.z - end.z) } } } };
        if (!tes || !player) {
            out["refs"] = Json::array();
            return out;
        }
        // Distance from a point to the first 2000 units of the straight line from the stop towards the goal.
        const double reach = std::min(horizontal, 2000.0);
        const auto corridor = [&](const RE::NiPoint3& p) -> double {
            if (horizontal < 1.0) {
                return std::hypot(p.x - end.x, p.y - end.y);
            }
            const double ux = dx / horizontal, uy = dy / horizontal;
            const double t = std::clamp((p.x - end.x) * ux + (p.y - end.y) * uy, 0.0, reach);
            return std::hypot(p.x - (end.x + ux * t), p.y - (end.y + uy * t));
        };
        struct Found
        {
            double score;
            Json view;
        };
        std::vector<Found> found;
        tes->ForEachReferenceInRange(player, static_cast<float>(player->GetPosition().GetDistance(end) + reach + 800.0), [&](RE::TESObjectREFR* ref) {
            if (!ref || ref == player || ref->IsDisabled() || ref->IsDeleted() || !ref->GetBaseObject() || ref->As<RE::Actor>()) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            const auto pos = ref->GetPosition();
            const double fromStop = end.GetDistance(pos);
            const double side = corridor(pos);
            if (fromStop > 700.0 && side > 300.0) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            const auto category = RefCategory(ref);
            const auto model = Basename(ModelPath(ref));
            // Torches and similar fixtures are activators (they can be taken) but the player walks past
            // them; naming one as the cause of a dead end sends the caller after the wrong thing.
            const bool fixture = category != "door" && (Matches(model, loose) || Matches(NameOf(ref), loose));
            const bool interesting = !fixture && (category == "door" || category == "activator" || Matches(model, barrier));
            if (!interesting) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            Json view{ { "refID", Hex(ref->GetFormID()) }, { "type", category }, { "name", Name(RefName(ref)) },
                { "model", model.empty() ? Json(nullptr) : Json(model) }, { "pos", Vec3(pos) }, { "fromStop", Round(fromStop) },
                { "offLine", Round(side) } };
            if (const auto state = OpenState(ref); !state.is_null()) {
                view["openState"] = state;
            }
            if (ref->IsLocked()) {
                view["locked"] = true;
            }
            // Closed doors and gates first, then anything else by distance to the stop.
            const bool closed = view.contains("openState") && view["openState"] != "open";
            found.push_back({ std::min(fromStop, side + 100.0) - (closed ? 400.0 : 0.0), std::move(view) });
            return RE::BSContainer::ForEachResult::kContinue;
        });
        std::ranges::sort(found, [](const Found& a, const Found& b) { return a.score < b.score; });
        Json refs = Json::array();
        for (auto& f : found) {
            if (refs.size() >= 8) {
                break;
            }
            refs.push_back(std::move(f.view));
        }
        std::string likely;
        for (const auto& r : refs) {
            if (r.contains("openState") && r["openState"] != "open" && r["fromStop"].get<double>() < 600.0) {
                likely = std::format("closed {} {} ({})", r["type"].get<std::string>(), r["refID"].get<std::string>(),
                    r["name"].is_string() ? r["name"].get<std::string>() : (r["model"].is_string() ? r["model"].get<std::string>() : "unnamed"));
                break;
            }
        }
        if (likely.empty() && std::abs(goal.z - end.z) > 200.0 && horizontal < 600.0) {
            likely = std::format("height gap of {:.0f} units between the last reachable point and the goal", goal.z - end.z);
        }
        if (likely.empty() && !refs.empty()) {
            const auto& r = refs.front();
            likely = std::format("navmesh ends near {} {} ({})", r["type"].get<std::string>(), r["refID"].get<std::string>(),
                r["name"].is_string() ? r["name"].get<std::string>() : (r["model"].is_string() ? r["model"].get<std::string>() : "unnamed"));
        }
        out["likely"] = likely.empty() ? Json("the navmesh ends with no door, gate or activator nearby") : Json(likely);
        out["refs"] = std::move(refs);
        return out;
    }

    std::vector<RE::FormID> Pullables(const RE::NiPoint3& end, double radius)
    {
        static const std::regex lever("lever|chain|pull|switch|button|handle|winch|crank", std::regex::icase);
        static const std::regex barrier("gate|portcullis|bars|grate|bridge|cage|rubble|collapse|door", std::regex::icase);
        static const std::regex loose("torch|candle|lantern|lamp|brazier|sconce|chandelier|clutter|light", std::regex::icase);
        const auto tes = RE::TES::GetSingleton();
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!tes || !player) {
            return {};
        }
        struct Candidate
        {
            double score;
            RE::FormID id;
        };
        std::vector<Candidate> candidates;
        std::set<RE::FormID> barriers;
        std::vector<RE::TESObjectREFR*> all;
        tes->ForEachReferenceInRange(player, static_cast<float>(player->GetPosition().GetDistance(end) + radius), [&](RE::TESObjectREFR* ref) {
            if (!ref || ref == player || ref->IsDeleted() || !ref->GetBaseObject() || ref->As<RE::Actor>()) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            all.push_back(ref);
            // A gate or drawbridge is a candidate barrier whether or not it is currently loaded visibly:
            // what matters is that something else activates it.
            const auto base = ref->GetBaseObject();
            const bool loadDoor = base->Is(RE::FormType::Door) && ref->extraList.HasType(RE::ExtraDataType::kTeleport);
            if (!loadDoor && Matches(Basename(ModelPath(ref)), barrier)) {
                barriers.insert(ref->GetFormID());
                if (const auto parents = ref->extraList.GetByType<RE::ExtraActivateRef>()) {
                    for (const auto data : parents->parents) {
                        if (const auto parent = data ? data->activateRef.get() : nullptr; parent && !parent->IsDeleted()) {
                            candidates.push_back({ end.GetDistance(parent->GetPosition()) - 2000.0, parent->GetFormID() });
                        }
                    }
                }
            }
            return RE::BSContainer::ForEachResult::kContinue;
        });
        for (const auto ref : all) {
            if (ref->IsDisabled() || ref->IsLocked() || ref->IsActivationBlocked() || RefCategory(ref) != "activator") {
                continue;
            }
            const auto model = Basename(ModelPath(ref));
            const bool named = Matches(model, lever) || Matches(NameOf(ref), lever);
            bool linked = false;
            if (const auto extra = ref->extraList.GetByType<RE::ExtraLinkedRef>()) {
                for (const auto& link : extra->linkedRefs) {
                    linked = linked || (link.refr && barriers.contains(link.refr->GetFormID()));
                }
            }
            if (!linked && (!named || Matches(model, loose))) {
                continue;
            }
            candidates.push_back({ end.GetDistance(ref->GetPosition()) - (linked ? 2000.0 : 0.0), ref->GetFormID() });
        }
        std::ranges::sort(candidates, [](const Candidate& a, const Candidate& b) { return a.score < b.score; });
        std::vector<RE::FormID> out;
        for (const auto& c : candidates) {
            if (std::ranges::find(out, c.id) == out.end()) {
                out.push_back(c.id);
            }
        }
        return out;
    }

    std::string BlockerSummary(const Json& blockers)
    {
        if (!blockers.is_object() || !blockers.contains("likely")) {
            return {};
        }
        const auto& stop = blockers["stop"];
        return std::format("stopped at ({:.0f}, {:.0f}, {:.0f}); likely cause: {}", stop[0].get<double>(), stop[1].get<double>(),
            stop[2].get<double>(), blockers["likely"].get<std::string>());
    }
}

DEVTOOLS_MODULE(Inspect)
{
    Register("World.inspect", [](const Json& params) {
        AllowOnly(params, { "refID", "nodes", "graph", "scripts" });
        const auto ref = RequireRef(params, "refID");
        return Inspect(ref, Optional<bool>(params, "nodes", true), Optional<bool>(params, "graph", true), Optional<bool>(params, "scripts", false));
    }, { .thread = Thread::Main });

    Register("World.models", [](const Json& params) {
        AllowOnly(params, { "radius", "contains", "limit" });
        auto& player = RequirePlayer();
        const auto radius = std::clamp(Optional<float>(params, "radius", 1500.0F), 1.0F, 20000.0F);
        const auto limit = std::clamp(Optional<int>(params, "limit", 40), 1, 300);
        auto needle = Optional<std::string>(params, "contains", "");
        std::ranges::transform(needle, needle.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        const auto tes = RE::TES::GetSingleton();
        if (!tes) {
            throw RpcError(code::notReady, "The world is not available yet");
        }
        const auto origin = player.GetPosition();
        std::vector<std::pair<float, RE::TESObjectREFR*>> found;
        tes->ForEachReferenceInRange(&player, radius, [&](RE::TESObjectREFR* ref) {
            if (!ref || ref == &player || ref->IsDisabled() || ref->IsDeleted() || !ref->GetBaseObject()) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            auto model = inspect::Basename(inspect::ModelPath(ref));
            if (model.empty()) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            std::ranges::transform(model, model.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            if (needle.empty() || model.find(needle) != std::string::npos) {
                found.emplace_back(origin.GetDistance(ref->GetPosition()), ref);
            }
            return RE::BSContainer::ForEachResult::kContinue;
        });
        std::ranges::sort(found, [](const auto& a, const auto& b) { return a.first < b.first; });
        Json refs = Json::array();
        for (const auto& [distance, ref] : found) {
            if (static_cast<int>(refs.size()) >= limit) {
                break;
            }
            Json view{ { "refID", Hex(ref->GetFormID()) }, { "type", RefCategory(ref) }, { "name", Name(RefName(ref)) },
                { "model", inspect::Basename(inspect::ModelPath(ref)) }, { "distance", Round(distance) }, { "pos", Vec3(ref->GetPosition()) },
                { "angleDeg", Angles(ref->GetAngle()) } };
            refs.push_back(std::move(view));
        }
        return Json{ { "total", found.size() }, { "refs", std::move(refs) } };
    }, { .thread = Thread::Main });

    Register("Screen.project", [](const Json& params) {
        AllowOnly(params, { "refID", "x", "y", "z" });
        const auto camera = RE::Main::WorldRootCamera();
        if (!camera) {
            throw RpcError(code::notReady, "The world camera is not available");
        }
        RE::NiPoint3 center;
        float radius = 0.0F;
        Json describe = nullptr;
        if (params.contains("refID")) {
            const auto ref = RequireRef(params, "refID");
            describe = Json{ { "refID", Hex(ref->GetFormID()) }, { "name", Name(RefName(ref)) } };
            if (const auto root = ref->Get3D(); root && root->worldBound.radius > 0.0F) {
                center = root->worldBound.center;
                radius = root->worldBound.radius;
            } else {
                center = ref->GetPosition();
                radius = 50.0F;
            }
        } else {
            center = { Require<float>(params, "x"), Require<float>(params, "y"), Require<float>(params, "z") };
        }
        const auto mid = Project(camera, center);
        double left = mid.x, right = mid.x, top = mid.y, bottom = mid.y;
        bool allInFront = mid.inFront;
        if (radius > 0.0F) {
            for (int corner = 0; corner < 8; ++corner) {
                const RE::NiPoint3 p{ center.x + ((corner & 1) ? radius : -radius), center.y + ((corner & 2) ? radius : -radius),
                    center.z + ((corner & 4) ? radius : -radius) };
                const auto q = Project(camera, p);
                allInFront = allInFront && q.inFront;
                left = std::min(left, q.x);
                right = std::max(right, q.x);
                top = std::min(top, q.y);
                bottom = std::max(bottom, q.y);
            }
        }
        const auto clamp01 = [](double v) { return std::clamp(v, 0.0, 1.0); };
        const bool onScreen = mid.inFront && mid.x >= 0.0 && mid.x <= 1.0 && mid.y >= 0.0 && mid.y <= 1.0;
        Json out{ { "center", Json::array({ Round(mid.x, 4), Round(mid.y, 4) }) }, { "onScreen", onScreen }, { "inFront", mid.inFront },
            { "rect", Json{ { "x", Round(clamp01(left), 4) }, { "y", Round(clamp01(top), 4) }, { "w", Round(clamp01(right) - clamp01(left), 4) },
                          { "h", Round(clamp01(bottom) - clamp01(top), 4) } } },
            { "worldCenter", Vec3(center) }, { "radius", Round(radius) } };
        if (!allInFront) {
            out["note"] = "part of the bound is behind the camera; the rect is approximate";
        }
        if (!describe.is_null()) {
            out["ref"] = std::move(describe);
        }
        return out;
    }, { .thread = Thread::Main });

    Register("Setting.get", [](const Json& params) {
        AllowOnly(params, { "name" });
        const auto name = Require<std::string>(params, "name");
        const auto setting = FindSetting(name);
        if (!setting) {
            throw RpcError(code::notFound, "No INI or game setting by that name (INI names look like fAutoVanityModeDelay:Camera)");
        }
        return Json{ { "name", name }, { "value", SettingValue(setting) } };
    }, { .thread = Thread::Main });

    Register("Setting.set", [](const Json& params) {
        AllowOnly(params, { "name", "value" });
        const auto name = Require<std::string>(params, "name");
        const auto setting = FindSetting(name);
        if (!setting) {
            throw RpcError(code::notFound, "No INI or game setting by that name (INI names look like fAutoVanityModeDelay:Camera)");
        }
        const auto& value = params.at("value");
        Json previous = SettingValue(setting);
        switch (setting->GetType()) {
        case RE::Setting::Type::kBool:
            setting->SetBool(value.is_boolean() ? value.get<bool>() : value.get<double>() != 0.0);
            break;
        case RE::Setting::Type::kFloat:
            setting->SetFloat(value.get<float>());
            break;
        case RE::Setting::Type::kInteger:
            setting->SetInteger(value.get<std::int32_t>());
            break;
        case RE::Setting::Type::kUnsignedInteger:
            setting->SetUnsignedInteger(value.get<std::uint32_t>());
            break;
        default:
            throw RpcError(code::invalidParams, "Only bool, float and integer settings can be set");
        }
        return Json{ { "name", name }, { "previous", previous }, { "value", SettingValue(setting) } };
    }, { .thread = Thread::Main });
}
