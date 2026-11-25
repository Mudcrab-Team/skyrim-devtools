// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Gameplay notifications taken from the game's script event sources. Every sink is attached once
// at kDataLoaded and builds a payload only while a controller is subscribed to its type.
#include "events.h"
#include "game.h"
#include "methods.h"

#include <format>
#include <map>
#include <memory>

namespace
{
    using namespace devtools;

    constexpr RE::FormID playerId = 0x14;

    std::atomic_bool containerPlayerOnly = true;

    struct Counter
    {
        std::atomic<std::uint64_t> seen = 0;
        std::atomic<std::uint64_t> emitted = 0;
    };
    // Filled by the module install block, before any sink or handler can run, and only read afterwards.
    std::map<std::string, std::unique_ptr<Counter>, std::less<>> counters;

    std::string Hex(RE::FormID id) { return std::format("{:08X}", id); }

    void AddName(Json& out, const char* name, const char* editorId)
    {
        if (name && *name) {
            out["name"] = name;
        }
        if (editorId && *editorId) {
            out["editorId"] = editorId;
        }
    }

    Json Ref(const RE::TESObjectREFR* ref)
    {
        if (!ref) {
            return nullptr;
        }
        Json out{ { "id", Hex(ref->GetFormID()) } };
        if (const auto base = ref->GetBaseObject()) {
            out["base"] = Hex(base->GetFormID());
        }
        AddName(out, const_cast<RE::TESObjectREFR*>(ref)->GetName(), nullptr);
        return out;
    }

    Json Form(RE::FormID id)
    {
        if (!id) {
            return nullptr;
        }
        const auto form = RE::TESForm::LookupByID(id);
        if (!form) {
            return Json{ { "id", Hex(id) } };
        }
        if (const auto ref = form->AsReference()) {
            return Ref(ref);
        }
        Json out{ { "id", Hex(id) } };
        AddName(out, form->GetName(), form->GetFormEditorID());
        return out;
    }

    const char* CombatState(RE::ACTOR_COMBAT_STATE state)
    {
        switch (state) {
        case RE::ACTOR_COMBAT_STATE::kCombat:
            return "combat";
        case RE::ACTOR_COMBAT_STATE::kSearching:
            return "searching";
        default:
            return "none";
        }
    }

    bool PlayerInvolved(RE::FormID from, RE::FormID to) { return from == playerId || to == playerId; }

    template <class Event, class Build>
    class Sink final : public RE::BSTEventSink<Event>
    {
    public:
        Sink(const char* type, Build build) : type_(type), build_(std::move(build)), counter_(counters.at(type).get()) {}

        RE::BSEventNotifyControl ProcessEvent(const Event* event, RE::BSTEventSource<Event>*) override
        {
            if (!event) {
                return RE::BSEventNotifyControl::kContinue;
            }
            ++counter_->seen;
            if (events::Subscribed(type_)) {
                try {
                    auto data = build_(*event);
                    if (!data.is_null()) {
                        ++counter_->emitted;
                        events::Emit(type_, std::move(data));
                    }
                } catch (const std::exception&) {
                    // A malformed reference must never reach the game's event dispatch.
                }
            }
            return RE::BSEventNotifyControl::kContinue;
        }

    private:
        const char* type_;
        Build build_;
        Counter* counter_;
    };

    template <class Event, class Build>
    void Watch(RE::BSTEventSource<Event>* source, const char* type, Build build)
    {
        // Sinks live as long as the process; the game never calls back into a destroyed one.
        source->AddEventSink(new Sink<Event, Build>(type, std::move(build)));
    }

    void Attach()
    {
        const auto holder = RE::ScriptEventSourceHolder::GetSingleton();
        if (!holder) {
            return;
        }
        Watch(holder->GetEventSource<RE::TESCombatEvent>(), "combat", [](const RE::TESCombatEvent& e) {
            return Json{ { "actor", Ref(e.actor.get()) }, { "target", Ref(e.targetActor.get()) },
                { "state", CombatState(e.newState.get()) } };
        });
        Watch(holder->GetEventSource<RE::TESDeathEvent>(), "death", [](const RE::TESDeathEvent& e) {
            return Json{ { "victim", Ref(e.actorDying.get()) }, { "killer", Ref(e.actorKiller.get()) },
                { "dead", e.dead } };
        });
        Watch(holder->GetEventSource<RE::TESHitEvent>(), "hit", [](const RE::TESHitEvent& e) {
            auto flags = Json::array();
            using Flag = RE::TESHitEvent::Flag;
            if (e.flags.any(Flag::kPowerAttack)) flags.push_back("power");
            if (e.flags.any(Flag::kSneakAttack)) flags.push_back("sneak");
            if (e.flags.any(Flag::kBashAttack)) flags.push_back("bash");
            if (e.flags.any(Flag::kHitBlocked)) flags.push_back("blocked");
            return Json{ { "aggressor", Ref(e.cause.get()) }, { "target", Ref(e.target.get()) },
                { "source", Form(e.source) }, { "projectile", Form(e.projectile) }, { "flags", std::move(flags) } };
        });
        Watch(holder->GetEventSource<RE::TESContainerChangedEvent>(), "containerChanged",
            [](const RE::TESContainerChangedEvent& e) {
                if (containerPlayerOnly && !PlayerInvolved(e.oldContainer, e.newContainer)) {
                    return Json(nullptr);
                }
                return Json{ { "item", Form(e.baseObj) }, { "count", e.itemCount }, { "from", Form(e.oldContainer) },
                    { "to", Form(e.newContainer) } };
            });
        Watch(holder->GetEventSource<RE::TESQuestStageEvent>(), "questStage", [](const RE::TESQuestStageEvent& e) {
            return Json{ { "quest", Form(e.formID) }, { "stage", e.stage } };
        });
        Watch(holder->GetEventSource<RE::TESActivateEvent>(), "activate", [](const RE::TESActivateEvent& e) {
            return Json{ { "activator", Ref(e.actionRef.get()) }, { "target", Ref(e.objectActivated.get()) } };
        });
        Watch(holder->GetEventSource<RE::TESEquipEvent>(), "equip", [](const RE::TESEquipEvent& e) {
            return Json{ { "actor", Ref(e.actor.get()) }, { "item", Form(e.baseObject) }, { "equipped", e.equipped } };
        });
        Watch(holder->GetEventSource<RE::TESSpellCastEvent>(), "spellCast", [](const RE::TESSpellCastEvent& e) {
            return Json{ { "caster", Ref(e.object.get()) }, { "spell", Form(e.spell) } };
        });
        Watch(holder->GetEventSource<RE::TESMagicEffectApplyEvent>(), "magicEffectApply",
            [](const RE::TESMagicEffectApplyEvent& e) {
                return Json{ { "target", Ref(e.target.get()) }, { "caster", Ref(e.caster.get()) },
                    { "effect", Form(e.magicEffect) } };
            });
        if (const auto levels = RE::LevelIncrease::GetEventSource()) {
            Watch(levels, "levelUp", [](const RE::LevelIncrease::Event& e) {
                return Json{ { "level", e.newLevel } };
            });
        }
    }
}

DEVTOOLS_MODULE(GameEvents)
{
    // Descriptions are declared here so Events.list is complete before the game data is loaded.
    const std::pair<const char*, const char*> types[] = {
        { "combat", "An actor's combat state changed: {actor, target, state: none|combat|searching}" },
        { "death", "An actor died or was revived: {victim, killer, dead}" },
        { "hit", "A hit landed: {aggressor, target, source, projectile, flags: power|sneak|bash|blocked}" },
        { "containerChanged", "An item moved between containers: {item, count, from, to}; player-involved only by default" },
        { "questStage", "A quest stage was set: {quest, stage}" },
        { "activate", "A reference was activated: {activator, target}" },
        { "equip", "An actor equipped or unequipped an item: {actor, item, equipped}" },
        { "levelUp", "The player gained a level: {level}" },
        { "spellCast", "A spell was cast: {caster, spell}" },
        { "magicEffectApply", "A magic effect was applied: {target, caster, effect}" },
    };
    static const std::map<std::string, std::string> descriptions(std::begin(types), std::end(types));
    for (const auto& [type, description] : descriptions) {
        events::Declare(type, description);
        counters.emplace(type, std::make_unique<Counter>());
    }
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            Attach();
        }
    });

    Register("GameEvents.configure", [](const Json& params) {
        AllowOnly(params, { "containerPlayerOnly" });
        if (params.contains("containerPlayerOnly")) {
            containerPlayerOnly = Require<bool>(params, "containerPlayerOnly");
        }
        return Json{ { "containerPlayerOnly", containerPlayerOnly.load() } };
    }, { .thread = Thread::Any });
    // Counts include events nobody subscribed to, so a test can tell "did not happen" from "not listening".
    Register("GameEvents.counts", [](const Json& params) {
        AllowOnly(params, {});
        Json out = Json::object();
        for (const auto& [type, counter] : counters) {
            out[type] = { { "seen", counter->seen.load() }, { "emitted", counter->emitted.load() } };
        }
        return out;
    }, { .thread = Thread::Any });
}
