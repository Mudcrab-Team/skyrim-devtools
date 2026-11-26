// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Alerts: conditions that mean a playtesting agent is wasting time (stalled quest, combat stalemate,
// death loop, falling out of the world, blind camera, a menu left open, low fps, theft, no way to heal).
// A poller thread asks the game thread for one small state sample about twice a second and runs all
// detectors on its own thread, so the game thread only copies a few values. Raised and cleared alerts
// are events; Alerts.state returns what is active now plus a history.
#include "events.h"
#include "game.h"
#include "input.h"
#include "methods.h"
#include "perf.h"
#include "quest_targets.h"
#include "state_common.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <format>
#include <map>
#include <mutex>
#include <set>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    constexpr float radiansToDegrees = 57.29577951308232f;
    constexpr std::size_t maxHistory = 200;

    // Seconds unless the key says otherwise. Every key can be changed with Alerts.configure.
    const std::map<std::string, double> defaultThresholds{
        { "pollMs", 500 },
        { "questStalledMin", 8 },       // tracked quest stage and objectives unchanged
        { "combatStalemateS", 20 },     // in combat with nobody's health moving
        { "deathCount", 2 },            // deaths within deathWindowMin, or near one spot
        { "deathWindowMin", 10 },
        { "deathNearDist", 1500 },
        { "fallDrop", 2000 },           // units below the last ground the player stood on
        { "fallTimeS", 3 },             // continuous falling
        { "swimStallS", 30 },           // swimming this long...
        { "swimProgressDist", 600 },    // ...without getting this far from where it began
        { "pitchLimitDeg", 70 },        // camera pointing at sky or floor...
        { "pitchBlindS", 15 },          // ...for this long
        { "menuOpenS", 30 },            // a menu open with no input request
        { "notFocusedS", 5 },
        { "lowFpsBelow", 30 },
        { "lowFpsS", 10 },
        { "hitchMs", 1000 },
        { "lowHealthPct", 30 },
        { "targetCheckS", 10 },         // how often quest targets are inspected
        { "questCheckS", 5 },
        { "healCheckS", 5 },
    };

    std::mutex stateMutex;
    std::map<std::string, double> thresholds = defaultThresholds;
    std::set<std::string> disabledKinds;
    std::map<std::string, double> snoozedUntil;  // kind -> epoch seconds

    struct Alert
    {
        std::string kind;
        std::string severity;
        std::string hint;
        std::string detail;
        std::int64_t sinceMs = 0;
    };
    std::map<std::string, Alert> active;
    std::deque<Json> history;

    struct Cost
    {
        std::uint64_t ticks = 0;
        double totalUs = 0.0;
        double maxUs = 0.0;
        double lastUs = 0.0;
        double workTotalUs = 0.0;  // detector work on the poller thread
        double workMaxUs = 0.0;
    } cost;

    double Threshold(const std::string& key)
    {
        std::lock_guard lock(stateMutex);
        return thresholds.at(key);
    }

    std::int64_t EpochMs()
    {
        return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    }

    Json AlertJson(const Alert& alert, std::int64_t now)
    {
        return { { "kind", alert.kind }, { "severity", alert.severity }, { "sinceMs", alert.sinceMs },
            { "ageS", std::max<std::int64_t>(0, (now - alert.sinceMs) / 1000) }, { "hint", alert.hint }, { "detail", alert.detail } };
    }

    void Remember(Json entry)
    {
        history.push_back(std::move(entry));
        if (history.size() > maxHistory) {
            history.pop_front();
        }
    }

    void Raise(const std::string& kind, const char* severity, const char* hint, const std::string& detail)
    {
        Json emit;
        {
            std::lock_guard lock(stateMutex);
            if (disabledKinds.contains(kind)) {
                return;
            }
            if (const auto snooze = snoozedUntil.find(kind); snooze != snoozedUntil.end() && snooze->second > EpochMs() / 1000.0) {
                return;
            }
            if (const auto found = active.find(kind); found != active.end()) {
                found->second.detail = detail;
                return;
            }
            Alert alert{ kind, severity, hint, detail, EpochMs() };
            emit = AlertJson(alert, alert.sinceMs);
            Remember({ { "action", "raised" }, { "ms", alert.sinceMs }, { "kind", kind }, { "severity", severity },
                { "hint", hint }, { "detail", detail } });
            active.emplace(kind, std::move(alert));
        }
        events::Emit("alertRaised", std::move(emit));
    }

    void Clear(const std::string& kind, const char* why = "condition ended")
    {
        Json emit;
        {
            std::lock_guard lock(stateMutex);
            const auto found = active.find(kind);
            if (found == active.end()) {
                return;
            }
            const auto now = EpochMs();
            emit = { { "kind", kind }, { "sinceMs", found->second.sinceMs }, { "durationMs", now - found->second.sinceMs }, { "why", why } };
            Remember({ { "action", "cleared" }, { "ms", now }, { "kind", kind }, { "durationMs", now - found->second.sinceMs }, { "why", why } });
            active.erase(found);
        }
        events::Emit("alertCleared", std::move(emit));
    }

    void Set(bool condition, const std::string& kind, const char* severity, const char* hint, const std::string& detail)
    {
        if (condition) {
            Raise(kind, severity, hint, detail);
        } else {
            Clear(kind);
        }
    }

    // ---- sampling on the game thread ------------------------------------------------------------

    struct Sample
    {
        bool valid = false;
        bool playable = false;
        std::uint64_t epoch = 0;
        bool paused = false;
        std::vector<std::string> menus;
        float x = 0, y = 0, z = 0;
        float health = 0, healthMax = 0;
        bool inCombat = false, dead = false, swimming = false;
        float fallTime = 0, fallStartZ = 0;
        float pitchDeg = 0;
        float carry = 0, carryMax = 0;
        double hostileHealth = 0;
        int hostiles = 0;
        int guardsHostile = 0;
        int bounty = 0;
        int stolen = 0;
        bool healChecked = false;
        bool hasHeal = true;
        bool questChecked = false;
        std::uint64_t questSignature = 0;
        std::string questName;
        bool targetsChecked = false;
        std::string targetDead;
        std::string targetUnreachable;
    };

    const std::set<std::string> ambientMenus{ "HUD Menu", "Cursor Menu", "Fader Menu", "Overlay Interaction Menu",
        "Overlay Menu", "Mist Menu", "Top Menu", "Loading Menu", "Lock Picking Menu Backdrop" };

    std::mutex sampleMutex;
    std::condition_variable sampleCv;
    Sample latest;
    std::uint64_t sampleGeneration = 0;
    std::atomic_bool samplePending = false;

    float RestoreMagnitude(RE::AlchemyItem& potion)
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
            if (data.primaryAV == RE::ActorValue::kHealth && data.archetype == RE::EffectSetting::Archetype::kValueModifier &&
                !data.flags.any(RE::EffectSetting::EffectSettingData::Flag::kDetrimental)) {
                total += effect->effectItem.magnitude;
            }
        }
        return total;
    }

    bool SpellHeals(const RE::SpellItem* spell)
    {
        if (!spell || spell->GetSpellType() != RE::MagicSystem::SpellType::kSpell) {
            return false;
        }
        return std::ranges::any_of(spell->effects, [](const RE::Effect* effect) {
            if (!effect || !effect->baseEffect) {
                return false;
            }
            const auto& data = effect->baseEffect->data;
            return data.primaryAV == RE::ActorValue::kHealth && data.archetype == RE::EffectSetting::Archetype::kValueModifier &&
                   !data.flags.any(RE::EffectSetting::EffectSettingData::Flag::kDetrimental);
        });
    }

    bool CanHeal(RE::PlayerCharacter& player)
    {
        bool potion = false;
        (void)player.GetInventory([&](RE::TESBoundObject& object) {
            if (const auto alchemy = object.As<RE::AlchemyItem>(); alchemy && RestoreMagnitude(*alchemy) > 0.0f) {
                potion = true;
            }
            return false;  // only the filter's side effect is wanted, not a copy of the inventory
        });
        if (potion) {
            return true;
        }
        for (const auto spell : player.GetActorRuntimeData().addedSpells) {
            if (SpellHeals(spell)) {
                return true;
            }
        }
        if (const auto base = player.GetActorBase(); base && base->actorEffects) {
            for (std::uint32_t i = 0; i < base->actorEffects->numSpells; ++i) {
                if (SpellHeals(base->actorEffects->spells[i])) {
                    return true;
                }
            }
        }
        return false;
    }

    std::uint64_t Mix(std::uint64_t hash, std::uint64_t value)
    {
        return (hash ^ value) * 1099511628211ull;
    }

    void QuestSignature(Sample& s)
    {
        const auto handler = RE::TESDataHandler::GetSingleton();
        if (!handler) {
            return;
        }
        s.questChecked = true;
        for (const auto quest : handler->GetFormArray<RE::TESQuest>()) {
            if (!quest || !quest->IsRunning() || !quest->IsActive()) {
                continue;
            }
            auto hash = Mix(14695981039346656037ull, quest->GetFormID());
            hash = Mix(hash, quest->GetCurrentStageID());
            for (const auto objective : quest->objectives) {
                if (objective) {
                    hash = Mix(hash, objective->index);
                    hash = Mix(hash, static_cast<std::uint64_t>(objective->state.get()));
                }
            }
            s.questSignature = hash;
            const auto name = quest->GetName();
            s.questName = name && *name ? name : (quest->GetFormEditorID() ? quest->GetFormEditorID() : "");
            return;
        }
    }

    void TargetProblems(Sample& s)
    {
        try {
            s.targetsChecked = true;
            for (const auto& t : quest::CurrentTargets("", true)) {
                if (!t.quest || !t.quest->IsActive() || !t.target) {
                    continue;
                }
                const auto name = std::string(view::RefName(t.target));
                if (const auto actor = t.target->As<RE::Actor>(); actor && actor->IsDead()) {
                    s.targetDead = std::format("{} (objective {}){}", name, t.objectiveIndex, actor->IsEssential() ? ", essential" : "");
                } else if (t.target->IsDisabled() || (!t.target->GetParentCell() && !t.tracking)) {
                    s.targetUnreachable = std::format("{} (objective {}) is disabled or in no cell", name, t.objectiveIndex);
                }
            }
        } catch (const RpcError&) {
            s.targetsChecked = false;
        }
    }

    // Runs on the game thread; keep it to a handful of reads. The quest and target checks are heavier
    // and run on a slower cadence chosen by the poller.
    void TakeSample(bool wantQuest, bool wantTargets, bool wantHeal)
    {
        const double began = perf::Now();
        Sample s;
        const auto player = RE::PlayerCharacter::GetSingleton();
        const auto ui = RE::UI::GetSingleton();
        if (player && ui) {
            s.valid = true;
            s.playable = Playable(*ui);
            s.epoch = state::epoch.load();
            s.paused = ui->GameIsPaused();
            for (const auto& entry : ui->menuStack) {
                if (!entry) {
                    continue;
                }
                for (const auto& item : ui->menuMap) {
                    if (item.second.menu.get() == entry.get()) {
                        s.menus.emplace_back(item.first.c_str());
                        break;
                    }
                }
            }
        }
        if (s.valid && s.playable) {
            const auto pos = player->GetPosition();
            s.x = pos.x;
            s.y = pos.y;
            s.z = pos.z;
            const auto values = player->AsActorValueOwner();
            s.health = values->GetActorValue(RE::ActorValue::kHealth);
            s.healthMax = player->GetActorValueMax(RE::ActorValue::kHealth);
            s.carry = values->GetActorValue(RE::ActorValue::kInventoryWeight);
            s.carryMax = values->GetActorValue(RE::ActorValue::kCarryWeight);
            s.inCombat = view::ActiveCombatants(*player) > 0;
            s.dead = player->IsDead();
            s.swimming = player->AsActorState()->IsSwimming();
            s.pitchDeg = player->GetAngleX() * radiansToDegrees;
            if (const auto controller = player->GetCharController()) {
                s.fallTime = controller->fallTime;
                s.fallStartZ = controller->fallStartHeight;
            }
            auto& runtime = player->GetPlayerRuntimeData();
            for (const auto& entry : runtime.crimeGoldMap) {
                s.bounty += static_cast<int>(entry.second.violentCur + entry.second.nonViolentCur);
            }
            for (const auto& entry : runtime.stolenItemValueMap) {
                s.stolen += entry.second.unwitnessed + entry.second.witnessed;
            }
            if (s.inCombat) {
                if (const auto tes = RE::TES::GetSingleton()) {
                    tes->ForEachReferenceInRange(player, 3000.0f, [&](RE::TESObjectREFR* ref) {
                        const auto actor = ref ? ref->As<RE::Actor>() : nullptr;
                        if (actor && actor != player && !actor->IsDead() && !actor->IsDisabled() && actor->Is3DLoaded() &&
                            actor->GetActorRuntimeData().currentCombatTarget.get().get() == player) {
                            ++s.hostiles;
                            s.hostileHealth += actor->AsActorValueOwner()->GetActorValue(RE::ActorValue::kHealth);
                            if (actor->IsGuard()) {
                                ++s.guardsHostile;
                            }
                        }
                        return RE::BSContainer::ForEachResult::kContinue;
                    });
                }
            }
            if (wantHeal) {
                s.healChecked = true;
                s.hasHeal = CanHeal(*player);
            }
            if (wantQuest) {
                QuestSignature(s);
            }
            if (wantTargets) {
                TargetProblems(s);
            }
        }
        const double us = (perf::Now() - began) * 1e6;
        {
            std::lock_guard lock(stateMutex);
            ++cost.ticks;
            cost.totalUs += us;
            cost.maxUs = std::max(cost.maxUs, us);
            cost.lastUs = us;
        }
        perf::NoteCall("Alerts.tick", "main", began, 0.0, us / 1000.0);
        {
            std::lock_guard lock(sampleMutex);
            latest = std::move(s);
            ++sampleGeneration;
        }
        samplePending = false;
        sampleCv.notify_all();
    }

    // ---- detectors on the poller thread ----------------------------------------------------------

    struct Death
    {
        double at;
        float x, y, z;
    };

    struct Detectors
    {
        std::uint64_t epoch = ~0ull;
        bool wasPlayable = false;

        std::uint64_t questSignature = 0;
        double questChangedAt = 0;
        std::string questSeen;

        double combatSince = 0;
        double combatChangedAt = 0;
        float lastHealth = -1;
        double lastHostileHealth = -1;
        int lastHostiles = -1;

        bool wasDead = false;
        std::vector<Death> deaths;
        double lastDeathAt = 0;
        std::string deathDetail;

        float groundZ = 0;
        bool haveGround = false;
        double swimSince = 0;
        float swimX = 0, swimY = 0;
        float swimReach = 0;

        double blindSince = 0;

        double menuSince = 0;
        std::string menuKey;
        double unfocusedSince = 0;
        double idlePausedSince = 0;

        double lowFpsSince = 0;
        double hitchAt = 0;
        double hitchMs = 0;

        int lastBounty = -1;
        int lastStolen = -1;
        double crimeAt = 0;
        std::string crimeDetail;

        double lowHealthSince = 0;
        bool hasHeal = true;

        void Reset(const Sample& s, double now)
        {
            questSignature = 0;
            questChangedAt = now;
            combatSince = 0;
            combatChangedAt = now;
            lastHealth = -1;
            lastHostileHealth = -1;
            lastHostiles = -1;
            haveGround = false;
            swimSince = 0;
            blindSince = 0;
            lowFpsSince = 0;
            lowHealthSince = 0;
            lastBounty = s.bounty;
            lastStolen = s.stolen;
            crimeAt = 0;
            wasDead = s.dead;
            for (const auto* kind : { "questStalled", "combatStalemate", "fellOrOutOfBounds", "blindCamera", "lowFps", "overEncumbered",
                     "lowHealthNoHeal", "targetDead", "targetUnreachable", "menuLeftOpen", "paused", "crime" }) {
                Clear(kind, "new world");
            }
        }
    } d;

    std::string Fixed(double value, int decimals = 0) { return std::format("{:.{}f}", value, decimals); }

    void Evaluate(const Sample& s, double now)
    {
        const bool visible = !GetConfig().headless;

        // The load screen, the main menu and the first moments of a world are not play time.
        if (!s.valid || !s.playable) {
            d.wasPlayable = false;
            d.menuSince = 0;
            d.lowFpsSince = 0;
            d.unfocusedSince = 0;
            return;
        }
        if (!d.wasPlayable || s.epoch != d.epoch) {
            d.Reset(s, now);
            d.epoch = s.epoch;
            d.wasPlayable = true;
        }

        // questStalled
        if (s.questChecked) {
            if (s.questSignature != d.questSignature || s.questName != d.questSeen) {
                d.questSignature = s.questSignature;
                d.questSeen = s.questName;
                d.questChangedAt = now;
            }
            const double stalled = now - d.questChangedAt;
            Set(s.questSignature != 0 && stalled >= Threshold("questStalledMin") * 60.0, "questStalled", "warn",
                "re-read quest_targets and the objective text; try another approach, another route or another NPC",
                std::format("'{}': stage and objectives unchanged for {} min", d.questSeen, Fixed(stalled / 60.0, 1)));
        }

        // combatStalemate: nobody's health moves while in combat
        if (s.inCombat) {
            if (d.combatSince == 0) {
                d.combatSince = now;
                d.combatChangedAt = now;
            }
            if (std::abs(s.health - d.lastHealth) >= 1.0f || std::abs(s.hostileHealth - d.lastHostileHealth) >= 1.0 ||
                s.hostiles != d.lastHostiles) {
                d.combatChangedAt = now;
            }
            d.lastHealth = s.health;
            d.lastHostileHealth = s.hostileHealth;
            d.lastHostiles = s.hostiles;
            const double still = now - d.combatChangedAt;
            Set(still >= Threshold("combatStalemateS"), "combatStalemate", "warn",
                "no damage either way: the target may be unreachable or on a ledge; reposition, use a ranged attack, or leave",
                std::format("in combat, nobody's health changed for {} s ({} hostile)", Fixed(still), s.hostiles));
        } else {
            d.combatSince = 0;
            d.lastHealth = -1;
            Clear("combatStalemate");
        }

        // deathLoop
        if (s.dead && !d.wasDead) {
            d.deaths.push_back({ now, s.x, s.y, s.z });
            d.lastDeathAt = now;
            const double window = Threshold("deathWindowMin") * 60.0;
            std::erase_if(d.deaths, [&](const Death& x) { return now - x.at > 6 * window; });
            int recent = 0;
            for (const auto& x : d.deaths) {
                recent += now - x.at <= window ? 1 : 0;
            }
            int nearCount = 0;
            for (const auto& x : d.deaths) {
                nearCount = std::max(nearCount, static_cast<int>(std::ranges::count_if(d.deaths, [&](const Death& y) {
                    return std::hypot(x.x - y.x, x.y - y.y) <= Threshold("deathNearDist");
                })));
            }
            const int need = static_cast<int>(Threshold("deathCount"));
            if (recent >= need || nearCount >= need) {
                d.deathDetail = recent >= need ? std::format("died {} times in the last {} min", recent, Fixed(Threshold("deathWindowMin")))
                                               : std::format("died {} times near the same spot", nearCount);
                Raise("deathLoop", "error",
                    "kit up (better gear, potions), level first, avoid this fight, or fall back to the invulnerable fallback", d.deathDetail);
            }
        }
        d.wasDead = s.dead;
        if (d.lastDeathAt > 0 && now - d.lastDeathAt > Threshold("deathWindowMin") * 60.0) {
            Clear("deathLoop", "no recent deaths");
        }

        // fellOrOutOfBounds: far below the last ground, a long fall, or swimming without getting anywhere
        const bool falling = s.fallTime > 0.3f && !s.swimming;
        if (!falling && !s.swimming && !s.dead) {
            d.groundZ = s.z;
            d.haveGround = true;
        }
        const double drop = d.haveGround ? d.groundZ - s.z : 0.0;
        if (s.swimming) {
            if (d.swimSince == 0) {
                d.swimSince = now;
                d.swimX = s.x;
                d.swimY = s.y;
                d.swimReach = 0;
            }
            d.swimReach = std::max(d.swimReach, std::hypot(s.x - d.swimX, s.y - d.swimY));
        } else {
            d.swimSince = 0;
        }
        const bool swimStalled = d.swimSince > 0 && now - d.swimSince >= Threshold("swimStallS") && d.swimReach < Threshold("swimProgressDist");
        const bool longFall = falling && s.fallTime >= Threshold("fallTimeS");
        const bool farBelow = !s.dead && drop >= Threshold("fallDrop") && (falling || drop >= 2 * Threshold("fallDrop"));
        Set(longFall || farBelow || swimStalled, "fellOrOutOfBounds", "error",
            swimStalled ? "swimming in circles: head for the nearest shore or use console to move out of the water"
                        : "probably out of bounds or in a pit: use console `player.moveto` a known ref, coc to a cell, or reload the save",
            swimStalled ? std::format("swimming for {} s, never more than {} units from where it began", Fixed(now - d.swimSince), Fixed(d.swimReach))
                        : std::format("falling {} s, {} units below the last ground", Fixed(s.fallTime, 1), Fixed(drop)));

        // blindCamera from the view direction; the screenshot tool adds the pixel-based check
        const bool skyOrFloor = std::abs(s.pitchDeg) >= Threshold("pitchLimitDeg");
        d.blindSince = skyOrFloor ? (d.blindSince > 0 ? d.blindSince : now) : 0;
        Set(d.blindSince > 0 && now - d.blindSince >= Threshold("pitchBlindS"), "blindCamera", "info",
            "the camera points at the sky or the floor; look to level (look pitch_deg) before judging the scene",
            std::format("pitch {} deg for {} s", Fixed(s.pitchDeg), Fixed(now - d.blindSince)));

        // menuLeftOpen: a menu stays up while no input was sent
        std::string menuKey;
        for (const auto& menu : s.menus) {
            if (!ambientMenus.contains(menu)) {
                menuKey += menu + "|";
            }
        }
        const double lastInput = input::LastRequestAt();
        if (menuKey.empty() || menuKey != d.menuKey || lastInput > d.menuSince) {
            d.menuSince = menuKey.empty() ? 0 : std::max(now, lastInput);
        }
        d.menuKey = menuKey;
        Set(d.menuSince > 0 && now - d.menuSince >= Threshold("menuOpenS"), "menuLeftOpen", "warn",
            "close it: press_control Cancel, dialogue_close or close_menu; the game is waiting on this menu",
            std::format("{} open for {} s without input", menuKey, Fixed(now - d.menuSince)));

        // paused: unfocused window (visible runs) or a paused game with no menu to blame
        const bool unfocused = visible && !GameIsForeground();
        d.unfocusedSince = unfocused ? (d.unfocusedSince > 0 ? d.unfocusedSince : now) : 0;
        const bool pausedNoMenu = s.paused && menuKey.empty();
        d.idlePausedSince = pausedNoMenu ? (d.idlePausedSince > 0 ? d.idlePausedSince : now) : 0;
        const bool notFocused = d.unfocusedSince > 0 && now - d.unfocusedSince >= Threshold("notFocusedS");
        const bool paused = d.idlePausedSince > 0 && now - d.idlePausedSince >= Threshold("notFocusedS");
        Set(notFocused || paused, "paused", "warn",
            notFocused ? "the game window lost focus, so input may be ignored; refocus it or start headless"
                       : "the game is paused with no menu open: close the stuck menu or press the pause key again",
            notFocused ? "the game window is not in the foreground" : "the game is paused with no menu open");

        // lowFps from the frame ring; the hitch alert holds for 30 s so a poll never misses it
        if (const auto frames = perf::Recent(2.0); frames.hooked) {
            const double fps = static_cast<double>(frames.count) / 2.0;
            d.lowFpsSince = fps < Threshold("lowFpsBelow") ? (d.lowFpsSince > 0 ? d.lowFpsSince : now) : 0;
            if (frames.maxMs >= Threshold("hitchMs")) {
                d.hitchAt = now;
                d.hitchMs = std::max(d.hitchMs, frames.maxMs);
            }
            const bool slow = d.lowFpsSince > 0 && now - d.lowFpsSince >= Threshold("lowFpsS");
            const bool hitched = d.hitchAt > 0 && now - d.hitchAt < 30.0;
            Set(slow || hitched, "lowFps", "warn",
                "pause actions until it recovers, then log perf_hitches to see what held the frame thread",
                slow ? std::format("fps {} for {} s", Fixed(fps, 1), Fixed(now - d.lowFpsSince))
                     : std::format("a frame took {} ms", Fixed(d.hitchMs)));
            if (!hitched) {
                d.hitchMs = 0;
            }
        }

        // overEncumbered
        Set(s.carryMax > 0 && s.carry > s.carryMax, "overEncumbered", "warn",
            "drop or store heavy items (world_nearby for a container) or the player cannot run or fast travel",
            std::format("carrying {} of {}", Fixed(s.carry), Fixed(s.carryMax)));

        // crime: new bounty, hostile guards, or stolen item value rising
        if (d.lastBounty >= 0 && s.bounty > d.lastBounty) {
            d.crimeAt = now;
            d.crimeDetail = std::format("bounty rose to {}", s.bounty);
        }
        if (d.lastStolen >= 0 && s.stolen > d.lastStolen) {
            d.crimeAt = now;
            d.crimeDetail = std::format("stolen goods value rose to {}", s.stolen);
        }
        if (s.guardsHostile > 0) {
            d.crimeAt = now;
            d.crimeDetail = std::format("{} guard(s) are hostile, bounty {}", s.guardsHostile, s.bounty);
        }
        d.lastBounty = s.bounty;
        d.lastStolen = s.stolen;
        Set(d.crimeAt > 0 && now - d.crimeAt < 60.0, "crime", "warn",
            "pay the bounty, go to jail, or avoid guards; a theft in sight of an NPC will turn the town hostile",
            d.crimeDetail);

        // lowHealthNoHeal
        if (s.healChecked) {
            d.hasHeal = s.hasHeal;
        }
        const bool low = s.healthMax > 0 && s.health / s.healthMax * 100.0f < Threshold("lowHealthPct") && !s.dead;
        d.lowHealthSince = low ? (d.lowHealthSince > 0 ? d.lowHealthSince : now) : 0;
        Set(low && !d.hasHeal && now - d.lowHealthSince >= 2.0, "lowHealthNoHeal", "error",
            "no potion or healing spell: retreat or avoid combat, rest or wait to regenerate, or use console to heal",
            std::format("health {} of {} with no potion or healing spell", Fixed(s.health), Fixed(s.healthMax)));

        // quest target problems
        if (s.targetsChecked) {
            Set(!s.targetDead.empty(), "targetDead", "error",
                "a quest target is dead (a failed or broken objective): check quest_get, reload an earlier save or take another route",
                s.targetDead);
            Set(!s.targetUnreachable.empty(), "targetUnreachable", "warn",
                "the quest target is disabled or in no cell: it may not be available yet; check other objectives or prerequisites",
                s.targetUnreachable);
        }
    }

    void Poll()
    {
        double lastQuest = 0, lastTargets = 0, lastHeal = 0;
        std::uint64_t seen = 0;
        for (;;) {
            const double began = perf::Now();
            const auto wait = std::chrono::milliseconds(static_cast<int>(std::clamp(Threshold("pollMs"), 100.0, 10000.0)));
            if (!samplePending.exchange(true)) {
                const bool quest = began - lastQuest >= Threshold("questCheckS");
                const bool targets = began - lastTargets >= Threshold("targetCheckS");
                const bool heal = began - lastHeal >= Threshold("healCheckS");
                lastQuest = quest ? began : lastQuest;
                lastTargets = targets ? began : lastTargets;
                lastHeal = heal ? began : lastHeal;
                if (const auto tasks = SKSE::GetTaskInterface()) {
                    tasks->AddTask([=] { TakeSample(quest, targets, heal); });
                } else {
                    samplePending = false;
                }
            }
            Sample sample;
            {
                std::unique_lock lock(sampleMutex);
                sampleCv.wait_for(lock, wait, [&] { return sampleGeneration != seen; });
                if (sampleGeneration != seen) {
                    seen = sampleGeneration;
                    sample = latest;
                }
            }
            const double start = perf::Now();
            if (sample.valid) {
                Evaluate(sample, start);
            }
            const double us = (perf::Now() - start) * 1e6;
            {
                std::lock_guard lock(stateMutex);
                cost.workTotalUs += us;
                cost.workMaxUs = std::max(cost.workMaxUs, us);
            }
            const double spent = perf::Now() - began;
            const auto left = std::chrono::duration<double>(wait.count() / 1000.0 - spent);
            if (left.count() > 0) {
                std::this_thread::sleep_for(left);
            }
        }
    }

    Json ThresholdJson()
    {
        Json out = Json::object();
        for (const auto& [key, value] : thresholds) {
            out[key] = value;
        }
        return out;
    }
}

DEVTOOLS_MODULE(Alerts)
{
    events::Declare("alertRaised", "An alert became active: {kind, severity, sinceMs, ageS, hint, detail}");
    events::Declare("alertCleared", "An alert ended: {kind, sinceMs, durationMs, why}");

    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            std::thread(Poll).detach();
        }
    });
    SetCapability("alerts", true);

    Register("Alerts.state", [](const Json& params) {
        AllowOnly(params, { "history", "limit" });
        const auto limit = std::clamp(Optional<std::size_t>(params, "limit", 50), std::size_t{ 1 }, maxHistory);
        std::lock_guard lock(stateMutex);
        const auto now = EpochMs();
        auto list = Json::array();
        for (const auto& [kind, alert] : active) {
            list.push_back(AlertJson(alert, now));
        }
        Json out{ { "active", std::move(list) } };
        if (Optional<bool>(params, "history", false)) {
            auto entries = Json::array();
            const auto begin = history.size() > limit ? history.end() - static_cast<std::ptrdiff_t>(limit) : history.begin();
            for (auto it = begin; it != history.end(); ++it) {
                entries.push_back(*it);
            }
            out["history"] = std::move(entries);
        }
        const double ticks = static_cast<double>(std::max<std::uint64_t>(cost.ticks, 1));
        out["cost"] = { { "ticks", cost.ticks }, { "mainMeanUs", std::round(cost.totalUs / ticks) }, { "mainMaxUs", std::round(cost.maxUs) },
            { "mainLastUs", std::round(cost.lastUs) }, { "pollerMeanUs", std::round(cost.workTotalUs / ticks) },
            { "pollerMaxUs", std::round(cost.workMaxUs) } };
        out["thresholds"] = ThresholdJson();
        out["disabled"] = Json(std::vector<std::string>(disabledKinds.begin(), disabledKinds.end()));
        return out;
    }, { .thread = Thread::Any });

    Register("Alerts.configure", [](const Json& params) {
        AllowOnly(params, { "thresholds", "disable", "enable", "reset" });
        std::lock_guard lock(stateMutex);
        if (Optional<bool>(params, "reset", false)) {
            thresholds = defaultThresholds;
            disabledKinds.clear();
        }
        if (params.contains("thresholds")) {
            if (!params["thresholds"].is_object()) {
                throw RpcError(code::invalidParams, "thresholds is an object of name to number");
            }
            for (const auto& [key, value] : params["thresholds"].items()) {
                if (!thresholds.contains(key) || !value.is_number()) {
                    throw RpcError(code::invalidParams, "Unknown threshold or non-numeric value: " + key);
                }
                thresholds[key] = value.get<double>();
            }
        }
        for (const auto& kind : Optional<std::vector<std::string>>(params, "disable", {})) {
            disabledKinds.insert(kind);
        }
        for (const auto& kind : Optional<std::vector<std::string>>(params, "enable", {})) {
            disabledKinds.erase(kind);
        }
        return Json{ { "thresholds", ThresholdJson() }, { "disabled", Json(std::vector<std::string>(disabledKinds.begin(), disabledKinds.end())) } };
    }, { .thread = Thread::Any });

    // Dismissing hides an alert the agent has acted on, so it does not keep nagging while the condition lasts.
    Register("Alerts.dismiss", [](const Json& params) {
        AllowOnly(params, { "kind", "forS" });
        const auto kind = Require<std::string>(params, "kind");
        const auto forS = std::clamp(Optional<double>(params, "forS", 300.0), 1.0, 86400.0);
        {
            std::lock_guard lock(stateMutex);
            snoozedUntil[kind] = EpochMs() / 1000.0 + forS;
        }
        Clear(kind, "dismissed");
        return Json{ { "kind", kind }, { "forS", forS } };
    }, { .thread = Thread::Any });
}
