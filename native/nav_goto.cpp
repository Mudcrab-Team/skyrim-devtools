// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Nav.goto: one navigation process at a time, advanced once per input poll.
//
// ai mode hands the player to the engine: the player becomes AI-driven and the Papyrus latent
// Actor.PathToReference walks it to a reference, so the engine's own long-range pathfinder does
// the routing. input mode drives the player like a person would: it plans over the loaded
// navmeshes, holds Forward towards the next bend, and when the goal is beyond the loaded cells
// walks to the nearest reachable point and plans again as cells stream in.
#include "console.h"
#include "inspect.h"
#include "events.h"
#include "menu_util.h"
#include "methods.h"
#include "nav_graph.h"
#include "nav_path.h"
#include "quest_targets.h"

#include <deque>
#include <set>
#include <fstream>

namespace
{
    using namespace devtools;
    using namespace devtools::nav;

    struct Goal
    {
        std::string kind;                 // ref, point, cell, place, near
        RE::FormID ref = 0;               // followed every poll while it is loaded
        RE::NiPoint3 point;               // last known goal position
        RE::TESObjectCELL* cell = nullptr;      // goal interior or exterior cell, when known
        RE::TESWorldSpace* world = nullptr;     // goal worldspace for exterior goals
        double radius = 150.0;
        Json describe;
        std::string questText;
        bool questFinal = true;
    };

    struct History
    {
        double t;
        RE::NiPoint3 pos;
    };

    class PathCallback : public RE::BSScript::IStackCallbackFunctor
    {
    public:
        explicit PathCallback(std::shared_ptr<std::atomic_int> out) : out_(std::move(out)) {}
        // Runs on a Papyrus thread, so it only records the outcome; the poll picks it up.
        void operator()(RE::BSScript::Variable result) override { out_->store(result.IsBool() && result.GetBool() ? 1 : 2); }
        void SetObject(const RE::BSTSmartPointer<RE::BSScript::Object>&) override {}

    private:
        std::shared_ptr<std::atomic_int> out_;
    };

    struct Process
    {
        int id = 0;
        std::string mode;
        Goal goal;
        std::string state = "planning";  // planning, moving, recovering, paused, door, arrived, failed, cancelled
        std::string onCombat = "flee";
        std::string reason;
        Json blockers;  // set when the walk fails at a dead end
        double started = 0.0;
        double timeout = 300.0;
        bool run = true;
        bool sprint = false;
        float aiSpeed = 1.0F;
        std::optional<bool> restoreRunning;
        std::vector<RE::NiPoint3> via;
        std::size_t viaIndex = 0;

        // A long-range route over the worldspace graph, walked a gate at a time: the gates are
        // the via points and the local planner gets the player from one to the next.
        bool global = false;
        double globalPlanned = -1e9;
        double globalLength = 0.0;
        int globalRoutes = 0;
        int globalSkips = 0;
        int globalTeleports = 0;

        Plan plan;
        std::size_t waypoint = 0;
        std::uint64_t generation = 0;
        double lastPlan = -1e9;
        bool replanWanted = false;
        int deadEnds = 0;

        std::deque<History> history;
        int stuckCount = 0;
        int replans = 0;

        RE::FormID door = 0;        // load door being used to change cells
        double doorActivated = 0.0;
        std::string questText;      // questTarget goals: which quest, re-resolved as the compass moves on
        bool questFinal = true;     // the goal reference is the quest target itself, not a door on the way
        bool questStale = false;    // a cell change happened and the target has not been re-resolved yet
        double questStaleSince = 0.0;
        double questRefreshed = 0.0;
        bool activate = false;      // activate the goal reference on arrival
        bool activated = false;
        double loadingTotal = 0.0;  // seconds spent in loading screens; they do not count against timeoutS
        double loadingSince = -1.0;
        std::uint64_t openSignature = 0;  // open/locked/disabled state of what stands nearby
        double openChecked = -1e9;
        double openReset = -1e9;
        int staleResets = 0;

        RE::NiPointer<RE::TESObjectREFR> marker;
        std::shared_ptr<std::atomic_int> aiResult;
        double aiDispatched = 0.0;
        double aiTravelledAtDispatch = 0.0;
        int aiFailures = 0;  // consecutive legs that ended without real progress
        double aiEnded = -1e9;
        double aiBridgeUntil = -1e9;  // walking by input after engine legs that got nowhere
        double aiBridgeTravelled = 0.0;
        RE::FormID aiStone = 0;              // a map marker the engine is routed through instead
        std::set<RE::FormID> aiStonesTried;
        int aiStones = 0;
        bool combatSteer = false;

        // Interruption policies and the interruption in progress.
        std::string onDialogue = "close";
        std::string onMessageBox = "firstButton";
        std::string onMenu = "close";
        bool invulnerable = false;
        bool enabledGodMode = false;
        std::string interruption;  // empty while nothing interrupts the trip
        Json interruptionDetails;
        double interruptionBegan = 0.0;
        double interruptionActed = -1e9;
        int interruptionAttempts = 0;
        std::string interruptionAction;
        std::vector<Json> interruptions;
        int interruptionCount = 0;
        double fightSwing = -1e9;
        double fightDraw = -1e9;
        bool sheathe = false;

        // Stall and off-path episodes, for diagnosing steering.
        std::optional<double> stallBegan;
        RE::NiPoint3 stallPos;
        std::string stallCause;
        double stallOffPath = 0.0;
        std::optional<double> offPathBegan;
        RE::NiPoint3 offPathPos;
        double offPathMax = 0.0;
        std::vector<Json> episodes;
        int stallCount = 0;
        double stallTotal = 0.0;
        int offPathCount = 0;
        double offPathTotal = 0.0;
        double offPathNow = 0.0;

        // Door approach: the door being walked to (a load door or an activate goal) is entered from a
        // staging point in front of it, not from whichever side the path hugs.
        RE::FormID stageFor = 0;
        bool hasStage = false;
        bool staged = false;
        RE::NiPoint3 stage;
        bool slowed = false;  // walking the last stretch to a door
        // Stuck detection shared with Move.walk (stuck.h): no displacement, no progress to the goal,
        // door loops.
        stuck::Detector det;
        bool recoveringByAi = false;  // an engine-driven trip is being walked clear by input
        Json stuckInfo;               // the report of the stop, when the trip ended stuck
        // Spots the player walked through, a few hundred units apart, to step back to when caught
        // in collision geometry that no key press gets out of.
        std::deque<RE::NiPoint3> trail;
        int unsticks = 0;
        std::vector<RE::NiPoint3> traps;

        RE::NiPoint3 start;
        RE::NiPoint3 last;
        double travelled = 0.0;
        double lastEmit = 0.0;
        double ended = 0.0;
        bool combat = false;
        int teleports = 0;
    };

    std::optional<Process> active;

    // Hits on the player, written by the event sink and read by the poll.
    std::atomic<double> lastHitAt = -1e9;
    std::atomic<RE::FormID> lastHitBy = 0;
    std::atomic<int> hitCount = 0;

    class HitSink : public RE::BSTEventSink<RE::TESHitEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::TESHitEvent* event, RE::BSTEventSource<RE::TESHitEvent>*) override
        {
            const auto player = RE::PlayerCharacter::GetSingleton();
            if (event && player && event->target.get() == player) {
                lastHitAt = Now();
                lastHitBy = event->cause ? event->cause->GetFormID() : 0;
                ++hitCount;
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };
    std::optional<Json> lastSummary;
    int nextId = 1;

    void TargetRef(Goal& goal, RE::TESObjectREFR* ref);

    RE::TESObjectREFR* LookupRef(RE::FormID id) { return id ? RE::TESForm::LookupByID<RE::TESObjectREFR>(id) : nullptr; }

    bool* Running()
    {
        const auto controls = RE::PlayerControls::GetSingleton();
        return controls ? &controls->data.running : nullptr;
    }

    RE::NiPoint3 GoalPosition(Process& p)
    {
        if (const auto ref = LookupRef(p.goal.ref)) {
            p.goal.point = ref->GetPosition();
        }
        return p.goal.point;
    }

    // The door this trip is walking up to, once the via points are used up: a load door chosen to
    // change cells, or a door goal that is to be activated.
    RE::TESObjectREFR* ApproachDoor(Process& p)
    {
        if (p.viaIndex < p.via.size()) {
            return nullptr;
        }
        if (const auto door = LookupRef(p.door)) {
            return door;
        }
        if (p.activate && p.goal.ref) {
            const auto ref = LookupRef(p.goal.ref);
            const auto base = ref ? ref->GetBaseObject() : nullptr;
            if (base && base->Is(RE::FormType::Door)) {
                return ref;
            }
        }
        return nullptr;
    }

    constexpr double doorStageDistance = 130.0;
    constexpr double doorActivateReach = 160.0;

    // A point in front of the door on the side the player comes from, on the navmesh. Door models are
    // thin along one of their two horizontal axes and which one depends on the model, so the four
    // candidates along both are tried and only one on the navmesh and nearest the player is taken.
    bool StagePoint(RE::TESObjectREFR& door, const RE::NiPoint3& from, RE::NiPoint3& out)
    {
        const auto at = door.GetPosition();
        const double angle = door.GetAngleZ();
        const double axes[2][2] = { { std::sin(angle), std::cos(angle) }, { std::cos(angle), -std::sin(angle) } };
        double best = std::numeric_limits<double>::max();
        bool found = false;
        for (const auto& axis : axes) {
            for (const double sign : { 1.0, -1.0 }) {
                const RE::NiPoint3 candidate{ at.x + static_cast<float>(sign * axis[0] * doorStageDistance),
                    at.y + static_cast<float>(sign * axis[1] * doorStageDistance), at.z };
                if (!OnNavmesh(candidate)) {
                    continue;
                }
                const auto d = Dist2D(candidate, from);
                if (d < best) {
                    best = d;
                    out = candidate;
                    found = true;
                }
            }
        }
        return found;
    }

    // Where the player is heading right now: the next via point, the staging point or the face of a
    // door, or the goal.
    RE::NiPoint3 CurrentTarget(Process& p, double& radius)
    {
        if (p.viaIndex < p.via.size()) {
            radius = p.global ? 400.0 : 250.0;
            return p.via[p.viaIndex];
        }
        if (const auto door = ApproachDoor(p)) {
            if (p.stageFor != door->GetFormID()) {
                p.stageFor = door->GetFormID();
                p.staged = false;
                p.hasStage = false;
                if (const auto player = RE::PlayerCharacter::GetSingleton()) {
                    p.hasStage = StagePoint(*door, player->GetPosition(), p.stage);
                }
            }
            if (p.hasStage && !p.staged) {
                radius = 60.0;
                return p.stage;
            }
            // From the staging point the door is in reach: activate it rather than walk into it.
            radius = p.staged ? doorActivateReach : (p.door ? 110.0 : p.goal.radius);
            return door->GetPosition();
        }
        radius = p.goal.radius;
        return GoalPosition(p);
    }

    bool SameSpace(RE::PlayerCharacter& player, const Goal& goal)
    {
        const auto cell = player.GetParentCell();
        if (goal.cell && goal.cell->IsInteriorCell()) {
            return cell == goal.cell;
        }
        if (goal.world) {
            return player.GetWorldspace() == goal.world;
        }
        return true;
    }

    // The load door the player last arrived through, so a trip does not walk straight back through it.
    struct DoorTransit
    {
        RE::FormID arrival = 0;
        double at = -1e9;
        bool intoExterior = false;
    };
    DoorTransit recentDoor;
    constexpr double doorMemoryS = 120.0;
    RE::TESObjectCELL* lastCell = nullptr;
    RE::NiPoint3 lastCellPos;

    // Notes a cell change by door: the door standing next to the arrival point is the one just used.
    void TickDoors(Tick&)
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        const auto tes = RE::TES::GetSingleton();
        if (!player || !tes) {
            return;
        }
        const auto cell = player->GetParentCell();
        if (!cell) {
            return;
        }
        const auto pos = player->GetPosition();
        if (lastCell && cell != lastCell && !(!cell->IsInteriorCell() && !lastCell->IsInteriorCell())) {
            RE::TESObjectREFR* best = nullptr;
            double bestDistance = 450.0;
            tes->ForEachReferenceInRange(player, 450.0F, [&](RE::TESObjectREFR* ref) {
                const auto base = ref ? ref->GetBaseObject() : nullptr;
                if (base && base->Is(RE::FormType::Door) && ref->extraList.GetTeleportLinkedDoor().get()) {
                    const auto d = Dist3D(ref->GetPosition(), pos);
                    if (d < bestDistance) {
                        bestDistance = d;
                        best = ref;
                    }
                }
                return RE::BSContainer::ForEachResult::kContinue;
            });
            if (best) {
                recentDoor = { best->GetFormID(), Now(), !cell->IsInteriorCell() };
                stuck::Doors().Note(Now(), best->GetFormID());
            }
        }
        lastCell = cell;
        lastCellPos = pos;
    }

    // Open, locked and disabled state of the doors, gates and other activators within reach of the
    // route: a change means a way that was shut may be open (or the reverse), so cached dead ends are stale.
    std::uint64_t OpenSignature(RE::PlayerCharacter& player)
    {
        std::uint64_t hash = 1469598103934665603ULL;
        if (const auto tes = RE::TES::GetSingleton()) {
            tes->ForEachReferenceInRange(&player, 2500.0F, [&](RE::TESObjectREFR* ref) {
                const auto base = ref ? ref->GetBaseObject() : nullptr;
                if (!base || !(base->Is(RE::FormType::Door) || base->Is(RE::FormType::Activator))) {
                    return RE::BSContainer::ForEachResult::kContinue;
                }
                const std::uint64_t state = static_cast<std::uint64_t>(RE::BGSOpenCloseForm::GetOpenState(ref)) | (ref->IsLocked() ? 8ULL : 0ULL) |
                                            (ref->IsDisabled() ? 16ULL : 0ULL) | (ref->IsDeleted() ? 32ULL : 0ULL);
                hash = (hash ^ ref->GetFormID()) * 1099511628211ULL;
                hash = (hash ^ state) * 1099511628211ULL;
                return RE::BSContainer::ForEachResult::kContinue;
            });
        }
        return hash;
    }

    // A loaded load door whose other side is in the goal's cell or worldspace (one hop only).
    // Load doors a trip stopped using after being caught in a loop through them.
    std::set<RE::FormID>& BlockedDoors()
    {
        static std::set<RE::FormID> doors;
        return doors;
    }

    RE::TESObjectREFR* FindLoadDoor(RE::PlayerCharacter& player, const Goal& goal)
    {
        const auto tes = RE::TES::GetSingleton();
        if (!tes) {
            return nullptr;
        }
        const auto now = Now();
        const auto here = player.GetParentCell();
        const bool inInterior = here && here->IsInteriorCell();
        // First choice: a door whose other side holds the goal. Second: from an interior with no such
        // door, the way out to the exterior, which is where doors to the goal's cell are found. The
        // door just arrived through is not taken again unless the goal is behind it.
        RE::TESObjectREFR* best = nullptr;
        RE::TESObjectREFR* exit = nullptr;
        double bestDistance = std::numeric_limits<double>::max();
        double exitDistance = std::numeric_limits<double>::max();
        tes->ForEachReferenceInRange(&player, 30000.0F, [&](RE::TESObjectREFR* ref) {
            const auto base = ref ? ref->GetBaseObject() : nullptr;
            if (!base || !base->Is(RE::FormType::Door) || ref->IsDisabled() || BlockedDoors().contains(ref->GetFormID())) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            const auto linked = ref->extraList.GetTeleportLinkedDoor().get();
            if (!linked) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            const auto cell = linked->GetParentCell();
            const bool leads = goal.cell && goal.cell->IsInteriorCell() ? cell == goal.cell
                                                                        : (goal.world && linked->GetWorldspace() == goal.world);
            const auto d = Dist3D(ref->GetPosition(), player.GetPosition());
            if (leads) {
                if (d < bestDistance) {
                    bestDistance = d;
                    best = ref;
                }
            } else if (inInterior && cell && !cell->IsInteriorCell() && d < exitDistance &&
                       !(ref->GetFormID() == recentDoor.arrival && now - recentDoor.at < doorMemoryS)) {
                exitDistance = d;
                exit = ref;
            }
            return RE::BSContainer::ForEachResult::kContinue;
        });
        return best ? best : exit;
    }

    // An ordinary (non-load) door just ahead of the player, which may be what blocks it.
    RE::TESObjectREFR* DoorAhead(RE::PlayerCharacter& player)
    {
        RE::TESObjectREFR* best = nullptr;
        double bestDistance = 220.0;
        const auto pos = player.GetPosition();
        const double heading = player.GetAngleZ();
        if (const auto tes = RE::TES::GetSingleton()) {
            tes->ForEachReferenceInRange(&player, 260.0F, [&](RE::TESObjectREFR* ref) {
                const auto base = ref ? ref->GetBaseObject() : nullptr;
                if (!base || !base->Is(RE::FormType::Door) || ref->IsDisabled() || ref->extraList.GetTeleportLinkedDoor().get()) {
                    return RE::BSContainer::ForEachResult::kContinue;
                }
                const auto d = Dist2D(ref->GetPosition(), pos);
                if (d < bestDistance && std::abs(WrapPi(HeadingTo(pos, ref->GetPosition()) - heading)) < Rad(70.0)) {
                    bestDistance = d;
                    best = ref;
                }
                return RE::BSContainer::ForEachResult::kContinue;
            });
        }
        return best;
    }

    double SpeedOf(const Process& p)
    {
        if (p.history.size() < 2) {
            return 0.0;
        }
        const auto& a = p.history.front();
        const auto& b = p.history.back();
        return b.t - a.t > 0.2 ? Dist2D(a.pos, b.pos) / (b.t - a.t) : 0.0;
    }

    double PathRemaining(const Process& p, const RE::NiPoint3& pos, const RE::NiPoint3& target)
    {
        if (p.mode != "input" || p.plan.points.empty() || p.waypoint >= p.plan.points.size()) {
            return Dist3D(pos, target);
        }
        double total = Dist3D(pos, p.plan.points[p.waypoint]);
        for (auto i = p.waypoint + 1; i < p.plan.points.size(); ++i) {
            total += Dist3D(p.plan.points[i - 1], p.plan.points[i]);
        }
        if (!p.plan.complete) {
            total += Dist3D(p.plan.points.back(), target);
        }
        return total;
    }

    graph::V3 V(const RE::NiPoint3& p) { return { p.x, p.y, p.z }; }

    std::vector<graph::V3> GlobalPoints(const Process& p)
    {
        std::vector<graph::V3> out;
        out.reserve(p.via.size());
        for (const auto& v : p.via) {
            out.push_back(V(v));
        }
        return out;
    }

    Json Status(Process& p)
    {
        Json out{ { "id", p.id }, { "state", p.state }, { "mode", p.mode }, { "goal", p.goal.describe },
            { "elapsedS", Round((p.ended > 0 ? p.ended : Now()) - p.started, 10.0) }, { "stuckCount", p.stuckCount },
            { "replans", p.replans }, { "travelled", Round(p.travelled) }, { "combat", p.combat },
            { "reason", p.reason.empty() ? Json(nullptr) : Json(p.reason) }, { "blockers", p.blockers.is_null() ? Json(nullptr) : p.blockers }, { "teleports", p.teleports }, { "unsticks", p.unsticks } };
        if (const auto player = RE::PlayerCharacter::GetSingleton()) {
            const auto pos = player->GetPosition();
            const auto goalPos = GoalPosition(p);
            out["pos"] = Point(pos);
            out["goalPos"] = Point(goalPos);
            out["straightDistance"] = Round(Dist3D(pos, goalPos));
            double radius = 0.0;
            const auto target = CurrentTarget(p, radius);
            auto remaining = p.viaIndex < p.via.size() || p.door ? Dist3D(pos, goalPos) : PathRemaining(p, pos, target);
            if (p.global && p.viaIndex < p.via.size()) {
                remaining = global::Remaining(GlobalPoints(p), p.viaIndex, V(pos)) + Dist3D(p.via.back(), goalPos);
            }
            out["distanceRemaining"] = Round(remaining);
            const auto speed = SpeedOf(p);
            out["speed"] = Round(speed);
            out["etaS"] = speed > 20.0 ? Json(Round(remaining / speed)) : Json(nullptr);
        }
        out["stuck"] = p.stuckInfo.is_null() ? Json(nullptr) : p.stuckInfo;
        out["loading"] = p.interruption == "loading";
        out["loadingS"] = Round(p.loadingTotal);
        if (p.activate) {
            out["activated"] = p.activated;
        }
        if (p.staleResets) {
            out["openStateResets"] = p.staleResets;
        }
        if (recentDoor.arrival && Now() - recentDoor.at < doorMemoryS) {
            out["lastDoor"] = { { "refID", Hex(recentDoor.arrival) }, { "agoS", Round(Now() - recentDoor.at) } };
        }
        if (p.mode == "input") {
            out["waypoint"] = p.waypoint;
            out["waypoints"] = p.plan.points.size();
            out["planComplete"] = p.plan.complete;
            out["meshes"] = p.plan.meshes;
        }
        if (!p.via.empty()) {
            out["via"] = { { "index", p.viaIndex }, { "count", p.via.size() } };
        }
        if (p.global) {
            out["global"] = { { "length", Round(p.globalLength) }, { "routes", p.globalRoutes }, { "skips", p.globalSkips } };
        }
        if (const auto door = LookupRef(p.door)) {
            out["door"] = DescribeRef(door);
        }
        if (!p.interruption.empty()) {
            out["interruption"] = { { "reason", p.interruption }, { "details", p.interruptionDetails },
                { "sinceS", Round(Now() - p.interruptionBegan) }, { "attempts", p.interruptionAttempts },
                { "action", p.interruptionAction } };
        } else {
            out["interruption"] = nullptr;
        }
        const auto tail = [](const std::vector<Json>& all, std::size_t n) {
            return Json(std::vector<Json>(all.end() - static_cast<std::ptrdiff_t>(std::min(n, all.size())), all.end()));
        };
        out["interruptions"] = { { "count", p.interruptionCount }, { "recent", tail(p.interruptions, 20) } };
        out["stalls"] = { { "count", p.stallCount }, { "totalS", Round(p.stallTotal) }, { "offPathCount", p.offPathCount },
            { "offPathTotalS", Round(p.offPathTotal) }, { "offPathNow", Round(p.offPathNow) }, { "recent", tail(p.episodes, 20) } };
        return out;
    }

    void Emit(const char* type, Json data)
    {
        if (events::Subscribed(type)) {
            events::Emit(type, std::move(data));
        }
    }

    void Finish(Process& p, const std::string& state, const std::string& reason)
    {
        p.state = state;
        p.reason = reason;
        p.ended = Now();
        if (p.restoreRunning) {
            if (const auto running = Running()) {
                *running = *p.restoreRunning;
            }
        }
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (p.mode == "ai" && player) {
            player->SetAIDriven(false);
        }
        if (p.enabledGodMode && RE::PlayerCharacter::IsGodMode()) {
            console::Run("tgm", nullptr);
        }
        if (p.marker) {
            p.marker->Disable();
            p.marker->SetDelete(true);
            p.marker.reset();
        }
        auto summary = Status(p);
        if (state == "arrived") {
            Emit("navArrived", summary);
        } else if (state == "failed") {
            Emit("navFailed", summary);
        }
        lastSummary = summary;
        if (CurrentDrive().owner == "nav") {
            CurrentDrive() = {};
        }
        active.reset();
    }

    // Plans the long-range route from where the player is now and makes its gates the via
    // points; false (route unchanged) when the graph has none.
    bool RouteGlobal(Process& p, RE::PlayerCharacter& player, double now, const char* why, std::string* note = nullptr)
    {
        p.globalPlanned = now;
        const auto plan = PlanGlobal(player.GetWorldspace(), player.GetPosition(), GoalPosition(p));
        Json event{ { "id", p.id }, { "why", why }, { "global", GlobalJson(plan, true) } };
        Emit("navRoute", std::move(event));
        if (!plan.route.found) {
            if (note) {
                *note = plan.note;
            }
            return false;
        }
        p.via.clear();
        // Neither the start nor the goal: the goal is reached the ordinary way after the last gate.
        for (std::size_t i = 1; i + 1 < plan.route.points.size(); ++i) {
            const auto& v = plan.route.points[i];
            p.via.push_back({ static_cast<float>(v.x), static_cast<float>(v.y), static_cast<float>(v.z) });
        }
        p.viaIndex = 0;
        p.global = true;
        p.globalLength = plan.route.length;
        ++p.globalRoutes;
        p.plan = {};
        p.deadEnds = 0;
        return true;
    }

    bool DispatchPath(RE::PlayerCharacter& player, RE::TESObjectREFR* target, float speed, std::shared_ptr<std::atomic_int> out)
    {
        const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
        if (!vm || !target) {
            return false;
        }
        const auto handle = vm->GetObjectHandlePolicy()->GetHandleForObject(RE::FormType::ActorCharacter, &player);
        RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback{ new PathCallback(std::move(out)) };
        const auto args = RE::MakeFunctionArguments(std::move(target), std::move(speed));
        return vm->DispatchMethodCall(handle, "Actor", "PathToReference", args, callback);
    }

    // The engine reports no path for some long trips from some spots, although a nearer goal
    // on the way is reachable; a location's map marker part way there makes a good stepping stone.
    RE::TESObjectREFR* PickStone(Process& p, RE::PlayerCharacter& player)
    {
        const auto data = RE::TESDataHandler::GetSingleton();
        if (!data) {
            return nullptr;
        }
        const auto pos = player.GetPosition();
        const auto goal = GoalPosition(p);
        const auto direct = Dist2D(pos, goal);
        RE::TESObjectREFR* best = nullptr;
        double bestCost = 1e18;
        for (const auto location : data->GetFormArray<RE::BGSLocation>()) {
            const auto marker = location ? location->worldLocMarker.get() : nullptr;
            if (!marker || marker->GetWorldspace() != player.GetWorldspace() || p.aiStonesTried.contains(marker->GetFormID())) {
                continue;
            }
            const auto there = marker->GetPosition();
            const auto hop = Dist2D(pos, there);
            const auto rest = Dist2D(there, goal);
            if (hop < 2500.0 || hop > 25000.0 || rest > direct - 2000.0) {
                continue;
            }
            const auto cost = hop + rest;
            if (cost < bestCost) {
                bestCost = cost;
                best = marker.get();
            }
        }
        return best;
    }

    RE::TESObjectREFR* AiTarget(Process& p, RE::PlayerCharacter& player)
    {
        if (const auto stone = LookupRef(p.aiStone)) {
            return stone;
        }
        if (const auto ref = LookupRef(p.goal.ref)) {
            return ref;
        }
        if (!p.marker) {
            // A persistent marker stands in for a bare position, since the Papyrus call wants a
            // reference; persistence keeps it valid when its cell is not loaded.
            const auto base = RE::TESForm::LookupByID<RE::TESBoundObject>(0x3B);
            if (!base) {
                return nullptr;
            }
            p.marker = player.PlaceObjectAtMe(base, true);
            if (!p.marker) {
                return nullptr;
            }
            p.marker->SetPosition(p.goal.point);
        }
        return p.marker.get();
    }

    void StartAiLeg(Process& p, RE::PlayerCharacter& player, double now)
    {
        const auto target = AiTarget(p, player);
        p.aiResult = std::make_shared<std::atomic_int>(0);
        p.aiDispatched = now;
        p.aiTravelledAtDispatch = p.travelled;
        player.SetAIDriven(true);
        if (!target || !DispatchPath(player, target, p.aiSpeed, p.aiResult)) {
            Finish(p, "failed", "Could not start Actor.PathToReference on the player");
            return;
        }
        p.state = "moving";
    }

    void Plan(Process& p, RE::PlayerCharacter& player, const RE::NiPoint3& target, double now, bool count)
    {
        p.plan = PlanPath(player.GetPosition(), target);
        p.generation = GraphGeneration();
        p.lastPlan = now;
        p.waypoint = 1;
        p.replanWanted = false;
        if (count) {
            ++p.replans;
        }
        if (p.plan.found && !p.plan.complete && p.plan.points.size() >= 2 &&
            Dist2D(p.plan.points.back(), player.GetPosition()) < 120.0) {
            ++p.deadEnds;
        } else if (p.plan.found) {
            p.deadEnds = 0;
        }
    }

    // Pure pursuit: aim at the point `distance` ahead along the path rather than at the next
    // corner, which turns early and smoothly instead of overshooting each waypoint.
    // How far the route turns at waypoint i, in degrees (0 at its ends).
    double TurnAt(const struct Plan& plan, std::size_t i)
    {
        if (i == 0 || i + 1 >= plan.points.size()) {
            return 0.0;
        }
        const auto& a = plan.points[i - 1];
        const auto& b = plan.points[i];
        const auto& c = plan.points[i + 1];
        const auto before = HeadingTo(a, b);
        const auto after = HeadingTo(b, c);
        return std::abs(Deg(WrapPi(after - before)));
    }

    constexpr double sharpTurnDeg = 25.0;
    // Being under a bridge deck or on the floor below a ledge is not being at a point above: arrival
    // needs the same level, within a step or two.
    constexpr double levelTolerance = 120.0;
    constexpr double goalLevelTolerance = 150.0;

    bool AtLevel(const RE::NiPoint3& pos, const RE::NiPoint3& point, double xyRadius, double zTolerance = levelTolerance)
    {
        return Dist2D(pos, point) < xyRadius && std::abs(pos.z - point.z) < zTolerance;
    }

    // Distance along the route to the next sharp corner within `within`, or a negative number.
    double SharpCornerAhead(const Process& p, const RE::NiPoint3& pos, double within, double minTurn)
    {
        double travelled = 0.0;
        RE::NiPoint3 from = pos;
        for (auto i = p.waypoint; i < p.plan.points.size() && travelled <= within; ++i) {
            travelled += Dist2D(from, p.plan.points[i]);
            from = p.plan.points[i];
            if (TurnAt(p.plan, i) >= minTurn && travelled <= within) {
                return travelled;
            }
        }
        return -1.0;
    }

    // Pure pursuit that stops at sharp corners instead of aiming past them, which would cut inside the
    // corner and drag the player along its wall.
    RE::NiPoint3 LookAhead(const Process& p, const RE::NiPoint3& pos, double distance)
    {
        RE::NiPoint3 from = pos;
        double left = distance;
        for (auto i = p.waypoint; i < p.plan.points.size(); ++i) {
            const auto& to = p.plan.points[i];
            const auto d = Dist2D(from, to);
            if (d >= left && d > 1e-3) {
                const auto f = static_cast<float>(left / d);
                return { from.x + (to.x - from.x) * f, from.y + (to.y - from.y) * f, from.z + (to.z - from.z) * f };
            }
            if (TurnAt(p.plan, i) >= sharpTurnDeg) {
                return to;
            }
            left -= d;
            from = to;
        }
        return p.plan.points.back();
    }

    // Keeps a long-range route current: moves past gates already passed (a fight or a detour can
    // carry the player beyond one), and plans again after a teleport or once well off the route.
    void FollowGlobal(Process& p, RE::PlayerCharacter& player, double now)
    {
        const auto pos = player.GetPosition();
        if (p.teleports != p.globalTeleports) {
            p.globalTeleports = p.teleports;
            if (RouteGlobal(p, player, now, "teleport")) {
                return;
            }
        }
        if (p.viaIndex >= p.via.size()) {
            return;
        }
        const auto points = GlobalPoints(p);
        const auto next = global::Advance(points, p.viaIndex, V(pos));
        if (next != p.viaIndex) {
            p.viaIndex = next;
            p.plan = {};
        }
        if (global::OffRoute(points, p.viaIndex, V(pos)) > 3000.0 && now - p.globalPlanned > 15.0) {
            RouteGlobal(p, player, now, "offRoute");
        }
    }

    bool IsLoadDoor(RE::TESObjectREFR* ref)
    {
        const auto base = ref ? ref->GetBaseObject() : nullptr;
        return base && base->Is(RE::FormType::Door) && ref->extraList.HasType(RE::ExtraDataType::kTeleport);
    }

    // Follows the compass: the quest's tracking reference changes as the player crosses cells (the door
    // out of a building, then the door into the dungeon, then the objective itself).
    bool RefreshQuestGoal(Process& p, RE::PlayerCharacter& player, double now)
    {
        p.questRefreshed = now;
        std::vector<quest::Target> targets;
        try {
            targets = quest::CurrentTargets(p.questText, true);
        } catch (const RpcError&) {
            return false;
        }
        if (targets.empty()) {
            return false;
        }
        const auto& chosen = targets.front();
        const auto ref = chosen.tracking ? chosen.tracking : chosen.target;
        p.questFinal = !chosen.tracking || chosen.tracking == chosen.target;
        if (ref->GetFormID() != p.goal.ref) {
            TargetRef(p.goal, ref);
            p.goal.describe["ref"] = DescribeRef(ref);
            p.plan = {};
            p.replanWanted = true;
            p.door = 0;
            p.via.clear();
            p.viaIndex = 0;
            p.global = false;
            const auto world = player.GetWorldspace();
            if (SameSpace(player, p.goal) && world && Dist2D(player.GetPosition(), p.goal.point) > 8000.0 && HasGlobalGraph(world)) {
                RouteGlobal(p, player, now, "questTarget");
            }
        }
        return true;
    }

    // Ends a trip at its goal; with activate set, first faces the goal reference and activates it.
    void Arrive(Process& p, RE::PlayerCharacter& player)
    {
        if (p.goal.kind == "questTarget" && !p.questFinal) {
            // A door on the way to the objective: go through it; the next hop is resolved after the load.
            if (const auto ref = LookupRef(p.goal.ref); IsLoadDoor(ref)) {
                const auto now = Now();
                if (now - p.doorActivated > 5.0) {
                    p.state = "door";
                    p.doorActivated = now;
                    ref->ActivateRef(&player, 0, nullptr, 1, false);
                }
                return;
            }
        }
        if (p.activate && !p.activated) {
            if (const auto ref = LookupRef(p.goal.ref)) {
                p.activated = true;
                FaceHeading(player, HeadingTo(player.GetPosition(), ref->GetPosition()));
                ref->ActivateRef(&player, 0, nullptr, 1, false);
            }
        }
        Finish(p, "arrived", "");
    }

    void TickInput(Process& p, RE::PlayerCharacter& player, Tick& tick)
    {
        const auto now = tick.now;
        const auto pos = player.GetPosition();
        if (now - p.openChecked > 1.0) {
            p.openChecked = now;
            const auto signature = OpenSignature(player);
            if (p.openSignature != 0 && signature != p.openSignature && now - p.openReset > 8.0) {
                p.openReset = now;
                // A gate, web or door nearby changed state: dead ends and traps recorded so far no longer hold.
                p.traps.clear();
                p.deadEnds = 0;
                p.plan = {};
                p.replanWanted = true;
                ++p.staleResets;
                Emit("navReplan", { { "id", p.id }, { "why", "openStateChanged" }, { "pos", Point(pos) } });
            }
            p.openSignature = signature;
        }
        if (p.global) {
            FollowGlobal(p, player, now);
        }
        double radius = 0.0;
        auto target = CurrentTarget(p, radius);

        if (!p.door && p.viaIndex >= p.via.size() && !SameSpace(player, p.goal)) {
            if (const auto door = FindLoadDoor(player, p.goal)) {
                p.door = door->GetFormID();
                target = CurrentTarget(p, radius);
                Plan(p, player, target, now, false);
            } else {
                Finish(p, "failed", "The goal is in another cell or worldspace and no loaded load door leads there");
                return;
            }
        }

        if (p.hasStage && !p.staged && p.viaIndex >= p.via.size() && Dist2D(pos, target) < radius) {
            // At the staging point: face the door, which is then within reach.
            p.staged = true;
            if (const auto door = ApproachDoor(p)) {
                FaceHeading(player, HeadingTo(pos, door->GetPosition()));
            }
            p.plan = {};
            target = CurrentTarget(p, radius);
        }
        {
            // The last stretch to a door is walked, not run.
            const auto approach = ApproachDoor(p);
            const bool slow = approach && Dist2D(pos, approach->GetPosition()) < 200.0;
            if (slow != p.slowed) {
                p.slowed = slow;
                if (const auto running = Running(); running && p.run) {
                    *running = !slow;
                }
            }
        }
        if (Dist2D(pos, target) < radius && std::abs(pos.z - target.z) < goalLevelTolerance) {
            if (p.viaIndex < p.via.size()) {
                ++p.viaIndex;
                p.plan = {};
                return;
            }
            if (const auto door = LookupRef(p.door)) {
                if (now - p.doorActivated > 5.0) {
                    p.state = "door";
                    p.doorActivated = now;
                    door->ActivateRef(&player, 0, nullptr, 1, false);
                }
                return;
            }
            Arrive(p, player);
            return;
        }

        const bool stale = p.plan.points.empty() || p.waypoint >= p.plan.points.size() || p.replanWanted ||
                           (!p.plan.complete && GraphGeneration() != p.generation);
        bool deviated = false;
        p.offPathNow = 0.0;
        if (!stale && p.waypoint > 0 && p.waypoint < p.plan.points.size()) {
            p.offPathNow = graph::SegmentDistance2D({ pos.x, pos.y, pos.z },
                { p.plan.points[p.waypoint - 1].x, p.plan.points[p.waypoint - 1].y, 0.0 },
                { p.plan.points[p.waypoint].x, p.plan.points[p.waypoint].y, 0.0 });
            deviated = p.offPathNow > 200.0;
            // On another level than the segment it is following (under the deck it should be crossing):
            // the plan no longer describes where the player is.
            const auto& from = p.plan.points[p.waypoint - 1];
            const auto& to = p.plan.points[p.waypoint];
            const double sx = to.x - from.x, sy = to.y - from.y;
            const double len2 = sx * sx + sy * sy;
            const double t = len2 > 1.0 ? std::clamp(((pos.x - from.x) * sx + (pos.y - from.y) * sy) / len2, 0.0, 1.0) : 0.0;
            if (std::abs(pos.z - (from.z + (to.z - from.z) * t)) > 150.0) {
                p.offPathNow = std::max(p.offPathNow, 201.0);
                deviated = true;
            }
        }
        if ((stale || deviated) && now - p.lastPlan > 0.5) {
            Plan(p, player, target, now, p.lastPlan > -1e8);
            if (p.deadEnds > 5 && p.global && p.viaIndex < p.via.size() && p.globalSkips < 30) {
                // A gate the local planner cannot get closer to (a seam the graph joins but the
                // loaded navmeshes do not): aim for the one after it instead.
                ++p.viaIndex;
                ++p.globalSkips;
                p.deadEnds = 0;
                p.plan = {};
                return;
            }
            if (p.deadEnds > 5) {
                const auto stop = p.plan.points.empty() ? pos : p.plan.points.back();
                p.blockers = inspect::Blockers(stop, target);
                Finish(p, "failed", "Dead end: the loaded navmeshes have no way closer to the goal; " + inspect::BlockerSummary(p.blockers));
                return;
            }
        }

        RE::NiPoint3 aim = target;
        if (p.plan.found) {
            // A sharp corner is reached properly (its point already keeps clear of the walls); a gentle
            // bend may be rounded.
            while (p.waypoint + 1 < p.plan.points.size() &&
                   AtLevel(pos, p.plan.points[p.waypoint], TurnAt(p.plan, p.waypoint) >= sharpTurnDeg ? 35.0 : 90.0)) {
                ++p.waypoint;
            }
            if (p.waypoint < p.plan.points.size()) {
                aim = LookAhead(p, pos, 150.0);
            }
            if (p.waypoint + 1 == p.plan.points.size() && !p.plan.complete && AtLevel(pos, p.plan.points[p.waypoint], 90.0)) {
                // Plan the next stretch before reaching the end of this one, so there is no pause.
                p.replanWanted = true;
            }
        }
        p.state = "moving";
        const auto want = HeadingTo(pos, aim);
        const auto current = static_cast<double>(player.GetAngleZ());
        const auto error = WrapPi(want - current);
        const auto maxTurn = Rad(540.0) * std::max(0.005, static_cast<double>(tick.dt));
        FaceHeading(player, std::abs(error) <= maxTurn ? want : current + std::copysign(maxTurn, error));
        CurrentDrive().targetHeading = want;
        if (std::abs(error) < Rad(60.0)) {
            tick.drive.hold.push_back("Forward");
            // Sprinting through a sharp bend overshoots it into the wall, so the sprint stops short of one.
            const bool cornerSoon = p.plan.found && SharpCornerAhead(p, pos, 220.0, 45.0) >= 0.0;
            if ((p.sprint || p.combatSteer) && std::abs(error) < Rad(35.0) && !cornerSoon) {
                tick.drive.hold.push_back("Sprint");
            }
        }
    }

    void TickAi(Process& p, RE::PlayerCharacter& player, Tick& tick)
    {
        const auto now = tick.now;
        const auto pos = player.GetPosition();
        if (const auto stone = LookupRef(p.aiStone); stone && Dist2D(pos, stone->GetPosition()) < 1500.0) {
            // Past the stepping stone: head for the goal again on the next leg.
            p.aiStone = 0;
            p.aiStonesTried.clear();
            if (p.aiResult) {
                p.aiResult.reset();
                p.aiEnded = -1e9;
            }
        }
        const auto goal = GoalPosition(p);
        if (Dist2D(pos, goal) < p.goal.radius && std::abs(pos.z - goal.z) < goalLevelTolerance) {
            Arrive(p, player);
            return;
        }
        if (p.aiResult) {
            const auto result = p.aiResult->load();
            if (result != 0) {
                // PathToReference returned without arriving (interrupted, or no path): try again.
                if (Dist2D(pos, goal) < std::max(p.goal.radius * 3.0, 400.0)) {
                    Arrive(p, player);
                    return;
                }
                // Long trips end a leg now and then (combat, a cell boundary); only legs that got
                // nowhere count towards giving up.
                if (p.travelled - p.aiTravelledAtDispatch > 1500.0) {
                    p.aiFailures = 0;
                } else if (++p.aiFailures >= 40) {
                    Finish(p, "failed", result == 1 ? "PathToReference ended away from the goal" : "PathToReference reported no path");
                    return;
                }
                p.aiResult.reset();
                p.aiEnded = now;
                p.state = "waiting";
                if (const auto stone = LookupRef(p.aiStone); stone && p.travelled - p.aiTravelledAtDispatch <= 1500.0) {
                    // The stepping stone is no better; go back to the goal and try another one.
                    p.aiStone = 0;
                } else if (p.aiFailures >= 2 && p.aiStones < 12) {
                    if (const auto next = PickStone(p, player)) {
                        p.aiStone = next->GetFormID();
                        p.aiStonesTried.insert(p.aiStone);
                        ++p.aiStones;
                        p.aiFailures = 1;
                        Emit("navStone", { { "id", p.id }, { "marker", DescribeRef(next) }, { "pos", Point(player.GetPosition()) } });
                    }
                }
                if (p.aiFailures >= 2) {
                    // The engine finds no path from some spots (a navmesh seam, a ledge, just
                    // after a fight); walking the local navmesh for a few seconds gets it unstuck
                    // faster than retrying in place.
                    player.SetAIDriven(false);
                    p.aiBridgeUntil = now + std::min(4.0 + 2.0 * (p.aiFailures - 2), 12.0);
                    p.aiBridgeTravelled = p.travelled;
                    p.plan = {};
                    p.replanWanted = true;
                }
            }
        }
        if (now < p.aiBridgeUntil) {
            CurrentDrive().owner = "nav";
            TickInput(p, player, tick);
            if (p.travelled - p.aiBridgeTravelled > 1500.0) {
                // The walk got somewhere, so the next engine failure starts a short bridge again.
                p.aiFailures = 2;
            }
            return;
        }
        // A leg that made progress is followed at once, so the player never stands still between
        // legs; legs that got nowhere back off a little each time.
        const double backoff = p.aiFailures >= 2 ? 0.0 : 0.75 * p.aiFailures;
        if (!p.aiResult && now - p.aiEnded >= backoff) {
            if (CurrentDrive().owner == "nav") {
                CurrentDrive() = {};
            }
            ++p.replans;
            StartAiLeg(p, player, now);
            return;
        }
    }

    // ---- Interruptions: whatever stops the trip that is not the route itself. ----

    bool AnyMenuOpen(RE::UI& ui, std::initializer_list<std::string_view> names, std::string& which)
    {
        for (const auto name : names) {
            if (ui.IsMenuOpen(name)) {
                which = name;
                return true;
            }
        }
        return false;
    }

    // Menus that stop the player walking and that a playtester would just leave.
    const std::initializer_list<std::string_view>& BlockingMenus()
    {
        static const std::initializer_list<std::string_view> names{ RE::LockpickingMenu::MENU_NAME, RE::BookMenu::MENU_NAME,
            RE::SleepWaitMenu::MENU_NAME, RE::ContainerMenu::MENU_NAME, RE::BarterMenu::MENU_NAME, RE::GiftMenu::MENU_NAME,
            RE::TrainingMenu::MENU_NAME, RE::CraftingMenu::MENU_NAME, RE::LevelUpMenu::MENU_NAME, RE::TweenMenu::MENU_NAME,
            RE::InventoryMenu::MENU_NAME, RE::MagicMenu::MENU_NAME, RE::MapMenu::MENU_NAME, RE::JournalMenu::MENU_NAME,
            RE::FavoritesMenu::MENU_NAME, RE::StatsMenu::MENU_NAME, RE::TutorialMenu::MENU_NAME };
        return names;
    }

    Json DescribeActor(RE::TESObjectREFR* ref)
    {
        return ref ? DescribeRef(ref) : Json(nullptr);
    }

    RE::Actor* NearestHostile(RE::PlayerCharacter& player, double range)
    {
        const auto lists = RE::ProcessLists::GetSingleton();
        if (!lists) {
            return nullptr;
        }
        RE::Actor* best = nullptr;
        double bestDistance = range;
        for (const auto& handle : lists->highActorHandles) {
            const auto actor = handle.get();
            if (!actor || actor.get() == &player || actor->IsDead() || actor->IsDisabled()) {
                continue;
            }
            const auto targeting = actor->GetActorRuntimeData().currentCombatTarget.get();
            if (targeting.get() != &player && !actor->IsHostileToActor(&player)) {
                continue;
            }
            const auto d = Dist3D(actor->GetPosition(), player.GetPosition());
            if (d < bestDistance) {
                bestDistance = d;
                best = actor.get();
            }
        }
        return best;
    }

    // What blocks the trip right now, most severe first; empty when nothing does.
    std::string DetectInterruption(Process& p, RE::PlayerCharacter& player, double now, Json& details)
    {
        details = Json::object();
        if (player.IsDead()) {
            return "dead";
        }
        if (const auto ui = RE::UI::GetSingleton()) {
            std::string menu;
            if (AnyMenuOpen(*ui, { RE::LoadingMenu::MENU_NAME, RE::FaderMenu::MENU_NAME }, menu)) {
                details["menu"] = menu;
                return "loading";
            }
            if (ui->IsMenuOpen(RE::DialogueMenu::MENU_NAME)) {
                details["menu"] = RE::DialogueMenu::MENU_NAME;
                if (const auto topics = RE::MenuTopicManager::GetSingleton()) {
                    details["speaker"] = DescribeActor(topics->speaker.get().get());
                }
                return "dialogue";
            }
            if (ui->IsMenuOpen(RE::MessageBoxMenu::MENU_NAME)) {
                details["menu"] = RE::MessageBoxMenu::MENU_NAME;
                return "messageBox";
            }
            if (AnyMenuOpen(*ui, BlockingMenus(), menu)) {
                details["menu"] = menu;
                return "menu";
            }
            if (ui->GameIsPaused()) {
                return "paused";
            }
        }
        if (player.IsInRagdollState() || player.AsActorState()->GetKnockState() != RE::KNOCK_STATE_ENUM::kNormal) {
            return "knockdown";
        }
        if (player.AsActorValueOwner()->GetActorValue(RE::ActorValue::kParalysis) > 0.0F) {
            return "paralysis";
        }
        const bool hit = now - lastHitAt.load() < 4.0;
        if (p.onCombat != "ignore" && (player.IsInCombat() || hit)) {
            if (const auto enemy = NearestHostile(player, 4000.0)) {
                details["enemy"] = DescribeActor(enemy);
            }
            if (hit) {
                details["hitBy"] = DescribeActor(LookupRef(lastHitBy.load()));
            }
            details["hits"] = hitCount.load();
            return "combat";
        }
        return "";
    }

    std::string ActionFor(const Process& p, const std::string& reason)
    {
        if (reason == "dialogue") {
            return p.onDialogue;
        }
        if (reason == "messageBox") {
            return p.onMessageBox;
        }
        if (reason == "menu") {
            return p.onMenu;
        }
        if (reason == "combat") {
            return p.onCombat == "pause" ? "wait" : p.onCombat;
        }
        return reason == "dead" ? "stop" : "wait";
    }

    void BeginInterruption(Process& p, RE::PlayerCharacter& player, const std::string& reason, Json details, double now)
    {
        p.interruption = reason;
        p.interruptionDetails = std::move(details);
        p.interruptionBegan = now;
        p.interruptionActed = -1e9;
        p.interruptionAttempts = 0;
        p.interruptionAction = ActionFor(p, reason);
        ++p.interruptionCount;
        p.stallBegan.reset();
        p.offPathBegan.reset();
        if (p.mode == "ai") {
            // Whatever happens next (closing a menu, fleeing, waiting), the engine leg is over.
            p.aiResult.reset();
            player.SetAIDriven(false);
        }
        Emit("navInterrupted", { { "id", p.id }, { "reason", reason }, { "details", p.interruptionDetails },
            { "action", p.interruptionAction }, { "pos", Point(player.GetPosition()) } });
    }

    void EndInterruption(Process& p, RE::PlayerCharacter& player, double now, bool resume)
    {
        Json entry{ { "reason", p.interruption }, { "details", p.interruptionDetails }, { "action", p.interruptionAction },
            { "attempts", p.interruptionAttempts }, { "atS", Round(p.interruptionBegan - p.started) },
            { "durationS", Round(now - p.interruptionBegan) }, { "pos", Point(player.GetPosition()) } };
        if (p.interruptions.size() < 200) {
            p.interruptions.push_back(entry);
        }
        p.interruption.clear();
        p.interruptionDetails = nullptr;
        if (!resume) {
            return;
        }
        // Pick the route up again from wherever the interruption left the player.
        p.history.clear();
        p.plan = {};
        p.replanWanted = true;
        p.state = "moving";
        p.aiEnded = -1e9;
        p.aiFailures = 0;
        p.det.Reset(now);
        if (p.combatSteer) {
            p.combatSteer = false;
            p.sheathe = player.AsActorState()->IsWeaponDrawn();
            if (p.mode == "ai" && CurrentDrive().owner == "nav") {
                CurrentDrive() = {};
            }
        }
        Emit("navResumed", { { "id", p.id }, { "after", entry } });
    }

    void HideMenu(std::string_view name)
    {
        SKSE::GetTaskInterface()->AddUITask([menu = std::string(name)] {
            if (const auto queue = RE::UIMessageQueue::GetSingleton()) {
                queue->AddMessage(menu, RE::UI_MESSAGE_TYPE::kHide, nullptr);
            }
        });
    }

    // Leaves a conversation the way a player would: Cancel first, then closing the menu, and,
    // for a forced greeting that will not let go, picking the last topic (usually the refusal).
    void CloseDialogue(int attempt, Tick& tick)
    {
        if (attempt <= 2) {
            tick.drive.tap.push_back("Cancel");
        } else if (attempt <= 4) {
            HideMenu(RE::DialogueMenu::MENU_NAME);
        } else if (attempt % 2 == 1) {
            SKSE::GetTaskInterface()->AddUITask([] {
                const auto ui = RE::UI::GetSingleton();
                const auto manager = RE::MenuTopicManager::GetSingleton();
                if (!ui || !manager || !manager->dialogueList || !ui->IsMenuOpen(RE::DialogueMenu::MENU_NAME)) {
                    return;
                }
                int count = 0;
                for ([[maybe_unused]] const auto* topic : *manager->dialogueList) {
                    ++count;
                }
                if (const auto view = ui->GetMovieView(RE::DialogueMenu::MENU_NAME); view && count > 0) {
                    menu::CallDelegate(*view, "TopicClicked", { RE::GFxValue(static_cast<double>(count - 1)) });
                }
            });
        } else {
            tick.drive.tap.push_back("Cancel");
        }
    }

    void AnswerMessageBox(int attempt, Tick& tick)
    {
        if (attempt <= 2) {
            SKSE::GetTaskInterface()->AddUITask([] {
                if (const auto ui = RE::UI::GetSingleton(); ui && ui->IsMenuOpen(RE::MessageBoxMenu::MENU_NAME)) {
                    RE::MessageBoxMenu::SelectOption(0);
                }
            });
        } else if (attempt <= 4) {
            tick.drive.tap.push_back("Accept");
        } else {
            HideMenu(RE::MessageBoxMenu::MENU_NAME);
        }
    }

    // Stands and fights the nearest enemy; false when there is nobody to fight.
    bool Fight(Process& p, RE::PlayerCharacter& player, Tick& tick)
    {
        const auto enemy = NearestHostile(player, 2500.0);
        if (!enemy) {
            return false;
        }
        const auto now = tick.now;
        const auto pos = player.GetPosition();
        const auto want = HeadingTo(pos, enemy->GetPosition());
        FaceHeading(player, want);
        CurrentDrive().targetHeading = want;
        p.state = "fighting";
        if (!player.AsActorState()->IsWeaponDrawn()) {
            if (now - p.fightDraw > 2.0) {
                p.fightDraw = now;
                tick.drive.tap.push_back("Ready Weapon");
            }
            return true;
        }
        if (Dist2D(pos, enemy->GetPosition()) > 170.0) {
            tick.drive.hold.push_back("Forward");
        } else if (now - p.fightSwing > 0.7) {
            p.fightSwing = now;
            tick.drive.tap.push_back("Right Attack/Block");
        }
        return true;
    }

    void HandleInterruption(Process& p, RE::PlayerCharacter& player, Tick& tick)
    {
        const auto now = tick.now;
        const auto& action = p.interruptionAction;
        if (action != "flee" && action != "fight") {
            // Flee and fight keep the state and history that stuck recovery runs on.
            p.state = "interrupted";
            p.history.clear();
        }
        if (action == "close" || action == "firstButton") {
            if (now - p.interruptionActed < 1.0) {
                return;
            }
            p.interruptionActed = now;
            const int attempt = ++p.interruptionAttempts;
            if (p.interruption == "dialogue") {
                CloseDialogue(attempt, tick);
            } else if (p.interruption == "messageBox") {
                AnswerMessageBox(attempt, tick);
            } else if (attempt % 2 == 1) {
                tick.drive.tap.push_back("Cancel");
            } else {
                HideMenu(p.interruptionDetails.value("menu", ""));
            }
            return;
        }
        if (action == "flee" || action == "fight") {
            if (!p.combatSteer) {
                p.combatSteer = true;
                p.plan = {};
            }
            CurrentDrive().owner = "nav";
            if (action == "fight" && Fight(p, player, tick)) {
                return;
            }
            // Flee: keep to the route at a sprint; the history is needed for stuck recovery.
            TickInput(p, player, tick);
        }
    }

    // ---- Stall and off-path episodes. ----

    double RecentSpeed(const Process& p, double now, double window)
    {
        if (p.history.size() < 2) {
            return 0.0;
        }
        for (const auto& h : p.history) {
            if (now - h.t <= window) {
                const auto dt = p.history.back().t - h.t;
                return dt > 0.1 ? Dist2D(h.pos, p.history.back().pos) / dt : 1e9;
            }
        }
        return 1e9;
    }

    std::string GuessStallCause(Process& p, RE::PlayerCharacter& player)
    {
        if (p.mode == "ai" && Now() < p.aiBridgeUntil) {
            return "engine found no path (walking by input)";
        }
        if (p.mode == "ai" && !p.aiResult) {
            return "ai leg ended (replan latency)";
        }
        if (p.state == "recovering") {
            return "obstacle (recovering)";
        }
        if (p.mode == "input" && (p.replanWanted || p.plan.points.empty())) {
            return "replan latency";
        }
        const auto pos = player.GetPosition();
        const double heading = player.GetAngleZ();
        if (const auto lists = RE::ProcessLists::GetSingleton()) {
            for (const auto& handle : lists->highActorHandles) {
                const auto actor = handle.get();
                if (actor && actor.get() != &player && Dist2D(actor->GetPosition(), pos) < 160.0 &&
                    std::abs(WrapPi(HeadingTo(pos, actor->GetPosition()) - heading)) < Rad(60.0)) {
                    const auto name = actor->GetDisplayFullName();
                    return std::string("actor in the way: ") + (name ? name : "?");
                }
            }
        }
        if (DoorAhead(player)) {
            return "door";
        }
        if (const auto target = CurrentDrive().targetHeading; target && std::abs(WrapPi(*target - heading)) > Rad(60.0)) {
            return "turning (waypoint overshoot)";
        }
        if (p.mode == "input" && p.waypoint < p.plan.points.size()) {
            const auto& next = p.plan.points[p.waypoint];
            if (next.z - pos.z > 0.8 * std::max(1.0, Dist2D(pos, next))) {
                return "steep slope";
            }
        }
        return "obstacle (rock, tree or terrain)";
    }

    void PushEpisode(Process& p, Json entry)
    {
        if (p.episodes.size() >= 400) {
            p.episodes.erase(p.episodes.begin());
        }
        p.episodes.push_back(entry);
        Emit("navStall", std::move(entry));
    }

    void TrackEpisodes(Process& p, RE::PlayerCharacter& player, double now)
    {
        const auto pos = player.GetPosition();
        const auto speed = RecentSpeed(p, now, 0.5);
        const bool running = now - p.started > 2.0 && p.state != "door" && p.state != "arrived";
        if (p.stallBegan) {
            if (speed > 80.0 || !running) {
                const auto duration = now - *p.stallBegan;
                if (duration >= 0.5) {
                    ++p.stallCount;
                    p.stallTotal += duration;
                    PushEpisode(p, { { "id", p.id }, { "kind", "stall" }, { "atS", Round(*p.stallBegan - p.started) },
                        { "durationS", Round(duration) }, { "pos", Point(p.stallPos) }, { "cause", p.stallCause },
                        { "offPath", Round(p.stallOffPath) } });
                }
                p.stallBegan.reset();
            } else if (p.stallCause.starts_with("obstacle (rock")) {
                p.stallCause = GuessStallCause(p, player);
            }
        } else if (running && speed < 40.0) {
            p.stallBegan = now;
            p.stallPos = pos;
            p.stallCause = GuessStallCause(p, player);
            p.stallOffPath = p.offPathNow;
        }
        if (p.mode != "input" && !p.combatSteer) {
            p.offPathNow = 0.0;
        }
        if (p.offPathBegan) {
            p.offPathMax = std::max(p.offPathMax, p.offPathNow);
            if (p.offPathNow < 60.0) {
                const auto duration = now - *p.offPathBegan;
                ++p.offPathCount;
                p.offPathTotal += duration;
                PushEpisode(p, { { "id", p.id }, { "kind", "offPath" }, { "atS", Round(*p.offPathBegan - p.started) },
                    { "durationS", Round(duration) }, { "pos", Point(p.offPathPos) }, { "maxDistance", Round(p.offPathMax) } });
                p.offPathBegan.reset();
            }
        } else if (p.offPathNow > 120.0) {
            p.offPathBegan = now;
            p.offPathPos = pos;
            p.offPathMax = p.offPathNow;
        }
    }

    // Last resort for a player wedged in collision (seen at high sgtm against rocks): jumping,
    // strafing and backing up all leave it within a few units, so put it back on its own trail.
    bool Unstick(Process& p, RE::PlayerCharacter& player, double now)
    {
        const auto pos = player.GetPosition();
        for (auto it = p.trail.rbegin(); it != p.trail.rend(); ++it) {
            if (Dist2D(*it, pos) < 300.0) {
                continue;
            }
            const RE::NiPoint3 to{ it->x, it->y, it->z + 30.0F };
            // Caught at the same spot again: the corridor to this gate runs through it, so aim
            // for the next gate instead.
            const bool again = std::any_of(p.traps.begin(), p.traps.end(), [&](const auto& t) { return Dist2D(t, pos) < 300.0; });
            if (again && p.global && p.viaIndex < p.via.size() && p.globalSkips < 30) {
                ++p.viaIndex;
                ++p.globalSkips;
            }
            p.traps.push_back(pos);
            player.SetPosition(to, true);
            ++p.unsticks;
            p.last = to;
            p.history.clear();
            p.plan = {};
            p.replanWanted = true;
            p.det.Reset(now);
            Json data = Status(p);
            data["action"] = "unstick";
            data["from"] = Point(pos);
            data["to"] = Point(to);
            Emit("navStuck", std::move(data));
            return true;
        }
        return false;
    }

    // Distance still to go along the route, for the no-progress check.
    double RemainingDistance(Process& p, const RE::NiPoint3& pos)
    {
        const auto goalPos = GoalPosition(p);
        double radius = 0.0;
        const auto target = CurrentTarget(p, radius);
        if (p.global && p.viaIndex < p.via.size()) {
            return global::Remaining(GlobalPoints(p), p.viaIndex, V(pos)) + Dist3D(p.via.back(), goalPos);
        }
        return p.viaIndex < p.via.size() || p.door ? Dist3D(pos, goalPos) : PathRemaining(p, pos, target);
    }

    void BlockDoor(RE::FormID id)
    {
        BlockedDoors().insert(id);
        if (const auto ref = LookupRef(id)) {
            if (const auto linked = ref->extraList.GetTeleportLinkedDoor().get()) {
                BlockedDoors().insert(linked->GetFormID());
            }
        }
    }

    // One poll of the shared detector for a running trip: starts recoveries, replans around the
    // trouble spot, or ends the trip as stuck.
    void DetectStuck(Process& p, RE::PlayerCharacter& player, Tick& tick)
    {
        const auto now = tick.now;
        const auto pos = player.GetPosition();
        if (stuck::Paused() || p.state == "door" || p.state == "waiting" || p.state == "planning") {
            p.det.Reset(now);
            return;
        }
        if (p.det.Recovering(now)) {
            return;
        }
        const auto verdict = p.det.Update(now, pos.x, pos.y, RemainingDistance(p, pos), &stuck::Doors());
        if (verdict.unstuck) {
            stuck::EmitUnstuck("nav", verdict, pos);
        }
        if (verdict.step == stuck::Step::none) {
            return;
        }
        const auto goalPos = GoalPosition(p);
        auto report = stuck::Report(verdict, player, &goalPos);
        ++p.stuckCount;
        stuck::EmitStuck("nav", verdict, report);
        Json data = Status(p);
        data["attempt"] = verdict.failures;
        data["reason"] = stuck::Name(verdict.reason);
        Emit("navStuck", std::move(data));
        if (verdict.step == stuck::Step::abort) {
            if (p.global && p.unsticks < 10 && verdict.reason == stuck::Reason::stuck && Unstick(p, player, now)) {
                return;
            }
            p.blockers = report["blockers"];
            p.stuckInfo = report;
            Finish(p, "failed", std::string("stuck (") + stuck::Name(verdict.reason) + "): " + inspect::BlockerSummary(p.blockers));
            return;
        }
        if (p.mode == "ai") {
            player.SetAIDriven(false);
            if (verdict.step == stuck::Step::replan) {
                p.recoveringByAi = false;
                p.aiResult.reset();
                p.aiEnded = -1e9;
                ++p.replans;
            } else {
                p.recoveringByAi = true;
            }
        }
        if (verdict.step == stuck::Step::replan) {
            // Aim around the trouble: remember the spot, skip the gate or waypoint whose corridor runs
            // through it, and refuse a door that keeps being re-entered.
            p.traps.push_back(pos);
            if (verdict.door) {
                BlockDoor(verdict.door);
                p.door = 0;
            } else if (p.door) {
                BlockDoor(p.door);
                p.door = 0;
            }
            if (p.global && p.viaIndex < p.via.size() && p.globalSkips < 30 && verdict.reason != stuck::Reason::doorLoop) {
                ++p.viaIndex;
                ++p.globalSkips;
            } else if (p.waypoint + 1 < p.plan.points.size()) {
                ++p.waypoint;
            }
            p.plan = {};
            p.deadEnds = 0;
            p.replanWanted = true;
            p.det.Rebase(now);
            Emit("navReplan", { { "id", p.id }, { "why", stuck::Name(verdict.reason) }, { "pos", Point(pos) } });
        }
    }

    void TickNav(Tick& tick)
    {
        if (!active) {
            return;
        }
        auto& p = *active;
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!player) {
            return;
        }
        const auto now = tick.now;
        const auto pos = player->GetPosition();
        const auto step = Dist3D(pos, p.last);
        if (step > 1500.0) {
            ++p.teleports;
            p.history.clear();
            p.trail.clear();
            p.plan = {};
            p.door = 0;
            if (p.goal.kind == "questTarget") {
                p.questStale = true;
                p.questStaleSince = now;
            }
        } else {
            p.travelled += step;
        }
        p.last = pos;
        if (p.trail.empty() || Dist2D(pos, p.trail.back()) > 250.0) {
            p.trail.push_back(pos);
            if (p.trail.size() > 12) {
                p.trail.pop_front();
            }
        }
        p.history.push_back({ now, pos });
        while (!p.history.empty() && now - p.history.front().t > 8.5) {
            p.history.pop_front();
        }
        if (p.interruption == "loading") {
            p.loadingTotal += tick.dt;
        }
        if (now - p.started - p.loadingTotal > p.timeout) {
            Finish(p, "failed", "Timed out");
            return;
        }
        p.combat = player->IsInCombat();

        Json details;
        const auto reason = DetectInterruption(p, *player, now, details);
        if (reason != p.interruption) {
            if (!p.interruption.empty()) {
                EndInterruption(p, *player, now, reason.empty());
            }
            if (!reason.empty()) {
                BeginInterruption(p, *player, reason, std::move(details), now);
                if (p.interruptionAction == "stop") {
                    EndInterruption(p, *player, now, false);
                    Finish(p, "failed", reason == "dead" ? "The player died" : "Interrupted by " + reason);
                    return;
                }
            }
        }
        if (!p.interruption.empty()) {
            HandleInterruption(p, *player, tick);
            p.det.Reset(now);
        } else {
            if (p.sheathe) {
                p.sheathe = false;
                tick.drive.tap.push_back("Ready Weapon");
            }
            if (p.goal.kind == "questTarget" && (p.questStale || now - p.questRefreshed > 3.0)) {
                const bool ok = RefreshQuestGoal(p, *player, now);
                if (p.questStale && ok) {
                    p.questStale = false;
                } else if (p.questStale) {
                    // The compass has not picked the next hop yet; walking back through the door just used would undo the trip.
                    if (now - p.questStaleSince > 15.0) {
                        Finish(p, "failed", "The quest target was lost after changing cells");
                    }
                    return;
                }
            }
            if (p.det.Recovering(now)) {
                // A step-back or jump recovery replaces normal driving for its short duration.
                CurrentDrive().owner = "nav";
                p.state = "recovering";
                const auto controls = p.det.RecoveryControls(now);
                tick.drive.hold.insert(tick.drive.hold.end(), controls.hold.begin(), controls.hold.end());
                if (controls.tapJump) {
                    tick.drive.tap.push_back("Jump");
                }
                return;
            }
            if (p.recoveringByAi) {
                // The engine pathfinder takes over again from wherever the recovery left the player.
                p.recoveringByAi = false;
                p.aiResult.reset();
                p.aiEnded = -1e9;
                p.state = "moving";
            }
            if (p.mode == "ai") {
                TickAi(p, *player, tick);
            } else {
                CurrentDrive().owner = "nav";
                TickInput(p, *player, tick);
            }
            if (active) {
                TrackEpisodes(p, *player, now);
                DetectStuck(p, *player, tick);
            }
        }
        if (active && now - p.lastEmit >= 1.0) {
            p.lastEmit = now;
            Emit("navProgress", Status(p));
        }
    }

    RE::TESObjectCELL* CellByEditorId(const std::string& id)
    {
        return RE::TESForm::LookupByEditorID<RE::TESObjectCELL>(id);
    }

    RE::BGSLocation* LocationByName(const std::string& name)
    {
        const auto data = RE::TESDataHandler::GetSingleton();
        if (!data) {
            return nullptr;
        }
        auto lower = [](std::string s) {
            std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            return s;
        };
        // Localized games have translated names, so a location's form id works as well.
        if (name.size() >= 6 && name.size() <= 10 &&
            std::all_of(name.begin() + (name.starts_with("0x") ? 2 : 0), name.end(), [](unsigned char c) { return std::isxdigit(c); })) {
            const auto byId = RE::TESForm::LookupByID<RE::BGSLocation>(ParseFormId(Json(name), "place"));
            if (byId && byId->worldLocMarker.get()) {
                return byId;
            }
        }
        const auto wanted = lower(name);
        RE::BGSLocation* fallback = nullptr;
        for (const auto location : data->GetFormArray<RE::BGSLocation>()) {
            if (!location || !location->worldLocMarker.get()) {
                continue;
            }
            const auto full = location->GetFullName();
            if (full && lower(full) == wanted) {
                return location;
            }
            const auto editor = location->GetFormEditorID();
            if (!fallback && editor && lower(editor) == wanted) {
                fallback = location;
            }
        }
        return fallback;
    }

    void TargetRef(Goal& goal, RE::TESObjectREFR* ref)
    {
        goal.ref = ref->GetFormID();
        goal.point = ref->GetPosition();
        goal.cell = ref->GetParentCell();
        goal.world = ref->GetWorldspace();
    }

    Goal ResolveGoal(const Json& spec, RE::PlayerCharacter& player)
    {
        if (!spec.is_object()) {
            throw RpcError(code::invalidParams, "goal is an object: {refID} | {x,y,z} | {cell} | {place} | {questTarget} | {near, radius}");
        }
        Goal goal;
        if (spec.contains("refID")) {
            AllowOnly(spec, { "refID", "radius" });
            goal.kind = "ref";
            const auto ref = RequireRef(spec["refID"]);
            TargetRef(goal, ref);
            goal.describe = { { "refID", Hex(ref->GetFormID()) }, { "ref", DescribeRef(ref) } };
        } else if (spec.contains("questTarget")) {
            AllowOnly(spec, { "questTarget", "radius" });
            goal.kind = "questTarget";
            const auto& wanted = spec["questTarget"];
            const auto name = wanted.is_string() ? wanted.get<std::string>() : std::string();
            goal.questText = name;
            const auto targets = quest::CurrentTargets(name, true);
            if (targets.empty()) {
                throw RpcError(code::notFound, "No quest target to walk to" + (name.empty() ? std::string(" (no running quest has one)") : " for " + name));
            }
            // The compass follows the tracking ref; it is the way to the target when that is in another cell.
            const auto& chosen = targets.front();
            const auto ref = chosen.tracking ? chosen.tracking : chosen.target;
            goal.questFinal = !chosen.tracking || chosen.tracking == chosen.target;
            TargetRef(goal, ref);
            goal.describe = { { "questTarget", chosen.quest->GetFormEditorID() ? chosen.quest->GetFormEditorID() : "" },
                { "objective", chosen.objectiveIndex }, { "text", chosen.objectiveText }, { "ref", DescribeRef(ref) } };
        } else if (spec.contains("near")) {
            AllowOnly(spec, { "near", "radius" });
            goal.kind = "near";
            const auto ref = RequireRef(spec["near"], "near");
            TargetRef(goal, ref);
            goal.radius = 300.0;
            goal.describe = { { "near", DescribeRef(ref) } };
        } else if (spec.contains("place")) {
            AllowOnly(spec, { "place", "radius" });
            goal.kind = "place";
            const auto name = Require<std::string>(spec, "place");
            const auto location = LocationByName(name);
            if (!location) {
                throw RpcError(code::notFound, "No location named " + name + " with a world marker");
            }
            const auto marker = location->worldLocMarker.get();
            TargetRef(goal, marker.get());
            goal.radius = 600.0;
            goal.describe = { { "place", location->GetFullName() }, { "location", Hex(location->GetFormID()) }, { "marker", DescribeRef(marker.get()) } };
        } else if (spec.contains("cell")) {
            AllowOnly(spec, { "cell", "radius" });
            goal.kind = "cell";
            const auto id = Require<std::string>(spec, "cell");
            const auto cell = CellByEditorId(id);
            if (!cell) {
                throw RpcError(code::notFound, "No cell with editor id " + id);
            }
            goal.cell = cell;
            if (cell->IsInteriorCell()) {
                goal.point = player.GetPosition();
                goal.radius = 1e9;  // arriving means being inside the cell
            } else {
                const auto coords = cell->GetCoordinates();
                if (!coords) {
                    throw RpcError(code::notFound, "Cell " + id + " has no coordinates");
                }
                goal.world = cell->GetRuntimeData().worldSpace;
                goal.point = { coords->cellX * 4096.0F + 2048.0F, coords->cellY * 4096.0F + 2048.0F, player.GetPosition().z };
                if (const auto tes = RE::TES::GetSingleton()) {
                    float height = 0.0F;
                    if (tes->GetLandHeight(goal.point, height)) {
                        goal.point.z = height;
                    }
                }
                goal.radius = 1500.0;
            }
            goal.describe = { { "cell", DescribeCell(cell) } };
        } else if (spec.contains("x") && spec.contains("y")) {
            AllowOnly(spec, { "x", "y", "z", "worldspace", "radius" });
            goal.kind = "point";
            goal.point = { Require<float>(spec, "x"), Require<float>(spec, "y"), Optional<float>(spec, "z", player.GetPosition().z) };
            goal.world = player.GetWorldspace();
            if (spec.contains("worldspace")) {
                const auto name = Require<std::string>(spec, "worldspace");
                goal.world = RE::TESForm::LookupByEditorID<RE::TESWorldSpace>(name);
                if (!goal.world) {
                    throw RpcError(code::notFound, "No worldspace " + name);
                }
            }
            goal.describe = { { "point", Point(goal.point) }, { "worldspace", DescribeWorld(goal.world) } };
        } else {
            throw RpcError(code::invalidParams, "goal needs refID, near, place, questTarget, cell or x and y");
        }
        if (spec.contains("radius")) {
            goal.radius = Require<double>(spec, "radius");
        }
        return goal;
    }

    std::vector<RE::NiPoint3> ReadRoute(const std::string& name, double spacing)
    {
        if (name.find("..") != std::string::npos || name.find_first_of("/\\:") != std::string::npos) {
            throw RpcError(code::invalidParams, "route is a recording name from Record.list");
        }
        const auto path = RunDir() / "recordings" / (name.ends_with(".jsonl") ? name : name + ".jsonl");
        std::ifstream in(path);
        if (!in) {
            throw RpcError(code::notFound, "No recording " + name);
        }
        std::vector<RE::NiPoint3> points;
        std::string line;
        while (std::getline(in, line)) {
            const auto sample = Json::parse(line, nullptr, false);
            if (!sample.is_object() || !sample.contains("pos") || !sample["pos"].is_array() || sample["pos"].size() != 3) {
                continue;
            }
            const RE::NiPoint3 p{ sample["pos"][0].get<float>(), sample["pos"][1].get<float>(), sample["pos"][2].get<float>() };
            if (points.empty() || Dist3D(points.back(), p) >= spacing) {
                points.push_back(p);
            }
        }
        return points;
    }
}

namespace devtools::nav
{
    Json NavSummary()
    {
        if (!active) {
            return nullptr;
        }
        auto& p = *active;
        Json out{ { "id", p.id }, { "state", p.state }, { "mode", p.mode }, { "stuckCount", p.stuckCount }, { "replans", p.replans },
            { "interruption", p.interruption.empty() ? Json(nullptr) : Json(p.interruption) }, { "stalled", p.stallBegan.has_value() } };
        if (p.mode == "input" && p.waypoint < p.plan.points.size()) {
            out["waypoint"] = Point(p.plan.points[p.waypoint]);
        }
        return out;
    }
}

DEVTOOLS_MODULE(NavGoto)
{
    events::Declare("navProgress", "Nav.goto progress, about once a second: the Nav.status fields");
    events::Declare("navArrived", "Nav.goto reached its goal: the final Nav.status fields");
    events::Declare("navStuck", "Nav.goto made no progress and is recovering: Nav.status fields plus attempt");
    events::Declare("navFailed", "Nav.goto gave up (timeout, stuck, dead end, combat, no door): Nav.status fields with reason");
    events::Declare("navInterrupted", "Something stopped Nav.goto (dialogue, message box, menu, loading, combat, knockdown, death): {id, reason, details, action, pos}");
    events::Declare("navResumed", "An interruption cleared and Nav.goto picked the route up again: {id, after}");
    events::Declare("navStone", "Nav.goto ai mode routes the engine through a map marker after the goal reported no path: {id, marker, pos}");
    events::Declare("navRoute", "Nav.goto planned a long-range route over the worldspace graph: {id, why, global: Nav.plan's global fields}");
    events::Declare("navReplan", "Nav.goto dropped its dead-end memory and planned again because a nearby door, gate or web changed state: {id, why, pos}");
    events::Declare("navStall", "A stall (speed under 40 units/s) or off-path episode ended: {id, kind, atS, durationS, pos, cause?, offPath?, maxDistance?}");
    AddTicker(TickNav);
    AddTicker(TickDoors);
    if (const auto source = RE::ScriptEventSourceHolder::GetSingleton()) {
        static HitSink hits;
        source->AddEventSink<RE::TESHitEvent>(&hits);
    }

    Register("Nav.goto", [](const Json& params) {
        AllowOnly(params, { "goal", "mode", "timeoutS", "run", "sprint", "onCombat", "onDialogue", "onMessageBox", "onMenu", "invulnerable", "via",
            "route", "routeSpacing", "activate", "stuck", "loadEpoch" });
        RequirePlayable(params);
        std::lock_guard lock(Lock());
        if (active) {
            throw RpcError(code::busy, "A navigation is running; Nav.cancel it first");
        }
        if (CurrentDrive().owner == "walk" || CurrentDrive().owner == "replay") {
            throw RpcError(code::busy, "The player is being driven by " + CurrentDrive().owner);
        }
        auto& player = Player();
        Process p;
        p.goal = ResolveGoal(params.value("goal", Json::object()), player);
        p.questText = p.goal.questText;
        p.questFinal = p.goal.questFinal;
        p.questRefreshed = Now();
        p.activate = Optional<bool>(params, "activate", false);
        if (p.activate && !p.goal.ref) {
            throw RpcError(code::invalidParams, "activate needs a goal with a reference (refID, near, place or questTarget)");
        }
        if (p.activate && !params["goal"].contains("radius")) {
            p.goal.radius = std::min(p.goal.radius, 90.0);  // inside the activation reach, not merely near
        }
        p.mode = Optional<std::string>(params, "mode", "input");
        if (p.mode != "input" && p.mode != "ai") {
            throw RpcError(code::invalidParams, "mode is input or ai");
        }
        const auto choose = [&](const char* name, const char* fallback, std::initializer_list<std::string_view> allowed) {
            const auto value = Optional<std::string>(params, name, fallback);
            if (std::find(allowed.begin(), allowed.end(), value) == allowed.end()) {
                std::string list;
                for (const auto a : allowed) {
                    list += (list.empty() ? "" : ", ") + std::string(a);
                }
                throw RpcError(code::invalidParams, std::string(name) + " is one of " + list);
            }
            return value;
        };
        p.onCombat = choose("onCombat", "flee", { "flee", "fight", "stop", "ignore", "pause", "continue", "fail" });
        // Older spellings: continue kept walking, fail gave up.
        if (p.onCombat == "continue") {
            p.onCombat = "flee";
        } else if (p.onCombat == "fail") {
            p.onCombat = "stop";
        }
        p.onDialogue = choose("onDialogue", "close", { "close", "stop", "wait" });
        p.onMessageBox = choose("onMessageBox", "firstButton", { "firstButton", "stop", "wait" });
        p.onMenu = choose("onMenu", "close", { "close", "stop", "wait" });
        p.invulnerable = Optional<bool>(params, "invulnerable", false);
        p.timeout = Optional<double>(params, "timeoutS", 300.0);
        p.run = Optional<bool>(params, "run", true);
        p.sprint = Optional<bool>(params, "sprint", false);
        p.aiSpeed = p.run ? 1.0F : 0.0F;
        if (params.contains("via")) {
            for (const auto& v : params["via"]) {
                if (!v.is_array() || v.size() < 2) {
                    throw RpcError(code::invalidParams, "via is a list of [x, y, z?]");
                }
                p.via.push_back({ v[0].get<float>(), v[1].get<float>(), v.size() > 2 ? v[2].get<float>() : player.GetPosition().z });
            }
        }
        const auto routeName = Optional<std::string>(params, "route", "");
        const bool wantGlobal = routeName == "global";
        const bool wantLocal = routeName == "local";
        if (params.contains("route") && !wantGlobal && !wantLocal) {
            auto route = ReadRoute(Require<std::string>(params, "route"), Optional<double>(params, "routeSpacing", 600.0));
            // Start from the route point nearest the player, so a route can be joined mid-way.
            std::size_t nearest = 0;
            for (std::size_t i = 1; i < route.size(); ++i) {
                if (Dist3D(route[i], player.GetPosition()) < Dist3D(route[nearest], player.GetPosition())) {
                    nearest = i;
                }
            }
            p.via.insert(p.via.end(), route.begin() + static_cast<std::ptrdiff_t>(std::min(nearest, route.size())), route.end());
        }
        if (p.mode == "ai" && !p.via.empty()) {
            throw RpcError(code::invalidParams, "via and route are input-mode options");
        }
        if (p.mode == "ai" && !SameSpace(player, p.goal) && !p.goal.ref) {
            throw RpcError(code::invalidParams, "ai mode across cells needs a reference goal (refID, near or place)");
        }
        if (p.mode == "input") {
            if (const auto running = Running()) {
                p.restoreRunning = *running;
                *running = p.run;
            }
        }
        if (p.invulnerable && !RE::PlayerCharacter::IsGodMode()) {
            console::Run("tgm", nullptr);
            p.enabledGodMode = true;
        }
        p.id = nextId++;
        p.started = p.lastEmit = Now();
        BlockedDoors().clear();
        p.det = stuck::Detector(stuck::ParseConfig(params));
        p.det.Reset(p.started);
        p.start = p.last = player.GetPosition();
        // Far goals in the same worldspace take the long-range route when a graph exists for it:
        // the loaded navmeshes reach only a few cells, and the engine's own pathfinder reports
        // no path from some places.
        const auto world = player.GetWorldspace();
        const bool distant = SameSpace(player, p.goal) && world && Dist2D(player.GetPosition(), p.goal.point) > 8000.0;
        std::string globalNote;
        if (wantGlobal && (p.mode != "input" || !p.via.empty() || !SameSpace(player, p.goal) || !world)) {
            throw RpcError(code::invalidParams, "route global is an input-mode option for a goal in the player's worldspace, without via");
        }
        // Right after leaving a building the local navmeshes are few; the worldspace graph is the safer guide.
        const bool afterDoor = recentDoor.intoExterior && Now() - recentDoor.at < doorMemoryS &&
                               SameSpace(player, p.goal) && world && Dist2D(player.GetPosition(), p.goal.point) > 2500.0;
        if (wantGlobal || (!wantLocal && p.mode == "input" && p.via.empty() && (distant || afterDoor) && HasGlobalGraph(world))) {
            if (!RouteGlobal(p, player, p.started, "start", &globalNote) && wantGlobal) {
                throw RpcError(code::notFound, globalNote);
            }
        }
        Json reply{ { "id", p.id }, { "mode", p.mode }, { "goal", p.goal.describe }, { "goalPos", Point(p.goal.point) },
            { "straightDistance", Round(Dist3D(player.GetPosition(), p.goal.point)) }, { "via", p.via.size() },
            { "route", p.global ? "global" : "local" } };
        if (p.global) {
            reply["globalLength"] = Round(p.globalLength);
        } else if (!globalNote.empty()) {
            reply["globalNote"] = globalNote;
        }
        active = std::move(p);
        if (active->mode == "ai") {
            StartAiLeg(*active, player, Now());
            if (!active) {
                throw RpcError(code::inputUnavailable, lastSummary ? lastSummary->value("reason", "") : "ai mode failed to start");
            }
        } else {
            CurrentDrive().owner = "nav";
        }
        return reply;
    }, { .thread = Thread::Main, .timeout = std::chrono::seconds(10) });

    Register("Nav.status", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        if (active) {
            return Json{ { "active", true }, { "nav", Status(*active) } };
        }
        return Json{ { "active", false }, { "last", lastSummary ? *lastSummary : Json(nullptr) } };
    }, { .thread = Thread::Main });

    Register("Nav.places", [](const Json& params) {
        AllowOnly(params, { "contains", "limit" });
        const auto contains = Optional<std::string>(params, "contains", "");
        const auto limit = std::clamp(Optional<int>(params, "limit", 50), 1, 1000);
        const auto data = RE::TESDataHandler::GetSingleton();
        Json places = Json::array();
        std::size_t total = 0;
        const auto player = RE::PlayerCharacter::GetSingleton();
        if (!data) {
            throw RpcError(code::notReady, "game data is not loaded");
        }
        for (const auto location : data->GetFormArray<RE::BGSLocation>()) {
            const auto marker = location ? location->worldLocMarker.get() : nullptr;
            const auto full = location ? location->GetFullName() : nullptr;
            if (!marker || !full || !*full || (!contains.empty() && std::string_view(full).find(contains) == std::string_view::npos)) {
                continue;
            }
            ++total;
            if (places.size() < static_cast<std::size_t>(limit)) {
                Json entry{ { "name", full }, { "location", Hex(location->GetFormID()) }, { "marker", Hex(marker->GetFormID()) },
                    { "pos", Point(marker->GetPosition()) }, { "world", DescribeWorld(marker->GetWorldspace()) } };
                if (player) {
                    entry["distance"] = Round(Dist3D(player->GetPosition(), marker->GetPosition()));
                }
                places.push_back(std::move(entry));
            }
        }
        return Json{ { "count", total }, { "places", std::move(places) } };
    }, { .thread = Thread::Main });

    // Goals need reference ids; this lists named ones around the player so a client can pick one.
    Register("Nav.nearby", [](const Json& params) {
        AllowOnly(params, { "radius", "limit", "kind" });
        RequirePlayable(params);
        const auto radius = std::clamp(Optional<float>(params, "radius", 3000.0F), 100.0F, 20000.0F);
        const auto limit = std::clamp(Optional<int>(params, "limit", 30), 1, 200);
        const auto kind = Optional<std::string>(params, "kind", "any");
        if (kind != "any" && kind != "actor" && kind != "door" && kind != "object") {
            throw RpcError(code::invalidParams, "kind is any, actor, door or object");
        }
        auto& player = Player();
        const auto origin = player.GetPosition();
        std::vector<std::pair<double, Json>> found;
        RE::TES::GetSingleton()->ForEachReferenceInRange(&player, radius, [&](RE::TESObjectREFR* ref) {
            if (!ref || ref == &player || ref->IsDisabled() || ref->IsDeleted()) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            const auto name = ref->GetDisplayFullName();
            if (!name || !*name) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            const bool actor = ref->Is(RE::FormType::ActorCharacter);
            const auto base = ref->GetBaseObject();
            const bool door = base && base->Is(RE::FormType::Door);
            if ((kind == "actor" && !actor) || (kind == "door" && !door) || (kind == "object" && (actor || door))) {
                return RE::BSContainer::ForEachResult::kContinue;
            }
            auto entry = DescribeRef(ref);
            const auto d = Dist3D(origin, ref->GetPosition());
            entry["distance"] = Round(d);
            entry["kind"] = actor ? "actor" : door ? "door" : "object";
            if (door && ref->extraList.HasType(RE::ExtraDataType::kTeleport)) {
                entry["loadDoor"] = true;
            }
            found.emplace_back(d, std::move(entry));
            return RE::BSContainer::ForEachResult::kContinue;
        });
        std::sort(found.begin(), found.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
        Json refs = Json::array();
        for (auto& [d, entry] : found) {
            if (refs.size() >= static_cast<std::size_t>(limit)) {
                break;
            }
            refs.push_back(std::move(entry));
        }
        return Json{ { "count", found.size() }, { "refs", std::move(refs) } };
    }, { .thread = Thread::Main });

    Register("Nav.cancel", [](const Json& params) {
        AllowOnly(params, {});
        std::lock_guard lock(Lock());
        if (!active) {
            return Json{ { "cancelled", false } };
        }
        Finish(*active, "cancelled", "Nav.cancel");
        return Json{ { "cancelled", true }, { "last", *lastSummary } };
    }, { .thread = Thread::Main });
}
