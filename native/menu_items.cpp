// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Item menus by meaning: list the entries of Inventory, Container, Barter and Favorites menus
// and act on one through the game's own equip and inventory APIs.
#include "menu_util.h"
#include "methods.h"

#include <algorithm>
#include <format>

namespace
{
    using namespace devtools;

    constexpr std::size_t maxItems = 500;

    struct Row
    {
        int index = 0;
        std::string name;
        std::uint32_t count = 0;
        std::uint32_t equipState = 0;
        bool favorited = false;
        bool selected = false;
        std::string category;
        RE::TESForm* form = nullptr;
        RE::TESBoundObject* object = nullptr;
        RE::ExtraDataList* extra = nullptr;
        RE::TESObjectREFR* owner = nullptr;
        std::int32_t value = 0;
        float weight = 0;
    };

    std::string Category(RE::TESForm* form)
    {
        if (!form) {
            return "unknown";
        }
        using F = RE::FormType;
        switch (form->GetFormType()) {
        case F::Weapon: return "weapon";
        case F::Armor: return "armor";
        case F::Ammo: return "ammo";
        case F::Book: return "book";
        case F::Ingredient: return "ingredient";
        case F::Misc: return "misc";
        case F::Scroll: return "scroll";
        case F::SoulGem: return "soulgem";
        case F::KeyMaster: return "key";
        case F::Light: return "light";
        case F::Spell: return "spell";
        case F::Shout: return "shout";
        case F::AlchemyItem: {
            const auto potion = form->As<RE::AlchemyItem>();
            return potion && potion->IsPoison() ? "poison" : potion && potion->IsFood() ? "food" : "potion";
        }
        default: return "other";
        }
    }

    std::string FormId(const RE::TESForm* form)
    {
        return form ? std::format("{:08X}", form->GetFormID()) : std::string();
    }

    struct Source
    {
        std::string menu;
        RE::ItemList* list = nullptr;     // Inventory, Container, Barter
        RE::FavoritesMenu* favorites = nullptr;
    };

    Source RequireSource(const std::string& name)
    {
        const auto menu = menu::RequireMenu(name);
        Source source{ name };
        if (name == RE::InventoryMenu::MENU_NAME) {
            source.list = static_cast<RE::InventoryMenu*>(menu.get())->GetRuntimeData().itemList;
        } else if (name == RE::ContainerMenu::MENU_NAME) {
            source.list = static_cast<RE::ContainerMenu*>(menu.get())->GetRuntimeData().itemList;
        } else if (name == RE::BarterMenu::MENU_NAME) {
            source.list = static_cast<RE::BarterMenu*>(menu.get())->GetRuntimeData().itemList;
        } else if (name == RE::FavoritesMenu::MENU_NAME) {
            source.favorites = static_cast<RE::FavoritesMenu*>(menu.get());
        } else {
            throw RpcError(code::invalidParams,
                name + " has no item API; use UI.list or UI.dump. Item menus are InventoryMenu, ContainerMenu, BarterMenu and FavoritesMenu.");
        }
        if (!source.list && !source.favorites) {
            throw RpcError(code::notReady, name + " has not built its item list yet");
        }
        return source;
    }

    std::vector<Row> Rows(const Source& source)
    {
        std::vector<Row> rows;
        if (source.favorites) {
            int index = 0;
            for (const auto& entry : source.favorites->GetRuntimeData().favorites) {
                Row row;
                row.index = index++;
                row.form = entry.item;
                row.object = entry.item ? entry.item->As<RE::TESBoundObject>() : nullptr;
                row.name = entry.item && entry.item->GetName() ? entry.item->GetName() : "";
                row.category = Category(entry.item);
                if (entry.entryData) {
                    row.count = static_cast<std::uint32_t>(std::max(entry.entryData->countDelta, 0));
                    row.equipState = entry.entryData->IsWorn() ? 1 : 0;
                    row.extra = entry.entryData->extraLists && !entry.entryData->extraLists->empty() ?
                                    entry.entryData->extraLists->front() : nullptr;
                }
                row.favorited = true;
                rows.push_back(std::move(row));
            }
            return rows;
        }
        const auto* selected = source.list->GetSelectedItem();
        int index = 0;
        for (const auto* item : source.list->items) {
            Row row;
            row.index = index++;
            if (!item) {
                continue;
            }
            auto& data = const_cast<RE::ItemList::Item*>(item)->data;
            const auto* desc = data.objDesc;
            row.object = desc ? desc->object : nullptr;
            row.form = row.object;
            const auto* name = data.GetName();
            row.name = name ? name : "";
            row.count = data.GetCount();
            row.equipState = data.GetEquipState();
            row.favorited = data.GetFavorite() != 0;
            row.selected = item == selected;
            row.category = Category(row.form);
            if (desc) {
                row.extra = desc->extraLists && !desc->extraLists->empty() ? desc->extraLists->front() : nullptr;
                row.value = const_cast<RE::InventoryEntryData*>(desc)->GetValue();
                row.weight = const_cast<RE::InventoryEntryData*>(desc)->GetWeight();
            }
            row.owner = RE::TESObjectREFR::LookupByHandle(data.owner.native_handle()).get();
            rows.push_back(std::move(row));
            if (rows.size() >= maxItems) {
                break;
            }
        }
        return rows;
    }

    Json Describe(const Row& row)
    {
        Json out{ { "index", row.index }, { "name", row.name }, { "count", row.count },
            { "equipped", row.equipState != 0 }, { "equipState", row.equipState },
            { "category", row.category }, { "favorited", row.favorited }, { "selected", row.selected },
            { "formId", FormId(row.form) }, { "value", row.value }, { "weight", row.weight } };
        if (row.owner) {
            out["owner"] = row.owner->IsPlayerRef() ? Json("player") : Json(FormId(row.owner));
        }
        return out;
    }

    const Row& Pick(const Json& params, const std::vector<Row>& rows)
    {
        std::vector<std::string> labels;
        for (const auto& row : rows) {
            labels.push_back(row.name);
        }
        return rows[menu::PickIndex(params, labels, "item")];
    }

    // Moves `count` of the entry between the player and the other container of the open menu.
    Json Transfer(const std::string& menuName, const Row& row, std::int32_t count, bool toPlayer)
    {
        const auto player = RE::PlayerCharacter::GetSingleton();
        RE::TESObjectREFR* other = nullptr;
        RE::TESObjectREFR* from = row.owner;
        if (menuName == RE::ContainerMenu::MENU_NAME) {
            other = RE::TESObjectREFR::LookupByHandle(RE::ContainerMenu::GetTargetRefHandle()).get();
        } else if (menuName == RE::BarterMenu::MENU_NAME) {
            other = RE::TESObjectREFR::LookupByHandle(RE::BarterMenu::GetTargetRefHandle()).get();
        }
        if (!row.object || !other) {
            throw RpcError(code::invalidParams, "This item has no container to move it to or from");
        }
        // The list shows one side at a time; the owner of the entry says which.
        const bool ownedByPlayer = from ? from->IsPlayerRef() : !toPlayer;
        if (toPlayer && ownedByPlayer) {
            throw RpcError(code::invalidParams, "The item is already in the player's inventory");
        }
        if (!toPlayer && !ownedByPlayer) {
            throw RpcError(code::invalidParams, "The item is not in the player's inventory");
        }
        count = std::clamp<std::int32_t>(count, 1, static_cast<std::int32_t>(row.count));
        auto* extra = count == 1 ? row.extra : nullptr;
        if (toPlayer) {
            other->RemoveItem(row.object, count, RE::ITEM_REMOVE_REASON::kRemove, extra, player);
        } else {
            player->RemoveItem(row.object, count, RE::ITEM_REMOVE_REASON::kStoreInContainer, extra, other);
        }
        return Json{ { "moved", count }, { "direction", toPlayer ? "toPlayer" : "toContainer" } };
    }

    Json Act(const Json& params, const std::string& menuName, const Source& source, const Row& row)
    {
        auto action = Optional<std::string>(params, "action", "");
        const auto player = RE::PlayerCharacter::GetSingleton();
        const auto count = Optional<std::int32_t>(params, "count", 0);
        if (action.empty()) {
            action = menuName == RE::ContainerMenu::MENU_NAME ? "take" :
                     menuName == RE::BarterMenu::MENU_NAME ? "select" : "equip";
        }
        Json result{ { "action", action }, { "item", Describe(row) } };
        if (action == "select") {
            throw RpcError(code::invalidParams,
                "select only picks the entry; use UI.callDelegate or Input for the menu's confirm step");
        }
        if (action == "take" || action == "transfer") {
            const bool toPlayer = action == "take" ? true : !(row.owner && !row.owner->IsPlayerRef());
            result.update(Transfer(menuName, row, count > 0 ? count : static_cast<std::int32_t>(row.count), toPlayer));
            return result;
        }
        if (!row.object || !player) {
            throw RpcError(code::invalidParams, "This entry is not an inventory object");
        }
        if (action == "drop") {
            const auto dropped = count > 0 ? count : 1;
            player->DropObject(row.object, dropped == 1 ? row.extra : nullptr, dropped);
            result["dropped"] = dropped;
            return result;
        }
        if (action == "equip" || action == "use" || action == "unequip") {
            auto* manager = RE::ActorEquipManager::GetSingleton();
            if (const auto spell = row.form ? row.form->As<RE::SpellItem>() : nullptr) {
                manager->EquipSpell(player, spell);
                return result;
            }
            if (const auto shout = row.form ? row.form->As<RE::TESShout>() : nullptr) {
                manager->EquipShout(player, shout);
                return result;
            }
            const bool worn = row.equipState != 0;
            const bool wearable = row.form->Is(RE::FormType::Weapon, RE::FormType::Armor, RE::FormType::Ammo, RE::FormType::Light);
            if (action == "unequip" || (action == "equip" && worn && wearable)) {
                if (!worn) {
                    result["note"] = "not equipped";
                    return result;
                }
                manager->UnequipObject(player, row.object, row.extra, 1, nullptr, true, true, true, true);
                result["did"] = "unequip";
                return result;
            }
            if (worn && wearable) {
                result["note"] = "already equipped";
                return result;
            }
            manager->EquipObject(player, row.object, row.extra, 1, nullptr, true, false, true, true);
            result["did"] = wearable ? "equip" : "use";
            return result;
        }
        throw RpcError(code::invalidParams, "action is equip, use, unequip, drop, take, transfer or select");
    }
}

DEVTOOLS_MODULE(MenuItems)
{
    Register("UI.listItems", [](const Json& params) {
        AllowOnly(params, { "menu" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto rows = Rows(RequireSource(menuName));
        auto items = Json::array();
        int selected = -1;
        for (const auto& row : rows) {
            items.push_back(Describe(row));
            if (row.selected) {
                selected = row.index;
            }
        }
        return Json{ { "menu", menuName }, { "count", rows.size() }, { "selectedIndex", selected }, { "items", items } };
    }, { .thread = Thread::UI });

    Register("UI.selectItem", [](const Json& params) {
        AllowOnly(params, { "menu", "index", "text", "action", "count" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto source = RequireSource(menuName);
        const auto rows = Rows(source);
        return Act(params, menuName, source, Pick(params, rows));
    }, { .thread = Thread::UI });
}
