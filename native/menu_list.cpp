// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// List-style menus (main menu, tween menu, journal tabs, training, ...): discover the CLIK lists a
// menu shows and press an entry by index or text.
#include "menu_util.h"
#include "methods.h"

#include <algorithm>

namespace
{
    using namespace devtools;
    using namespace devtools::menu;

    constexpr int maxDepth = 8;
    constexpr int maxNodes = 2500;
    constexpr std::uint32_t maxEntries = 150;

    struct Found
    {
        std::string path;
        RE::GFxValue list;
        RE::GFxValue entries;
    };

    class Names final : public RE::GFxValue::ObjectInterface::ObjVisitor
    {
    public:
        void Visit(const char* name, const RE::GFxValue& value) override
        {
            if (name && name[0] != '_' && (value.IsObject() || value.IsDisplayObject()) && !value.IsArray()) {
                children.emplace_back(name, value);
            }
        }
        std::vector<std::pair<std::string, RE::GFxValue>> children;
    };

    RE::GFxValue EntriesOf(const RE::GFxValue& object)
    {
        for (const char* name : { "entryList", "EntriesA", "dataProvider" }) {
            const auto candidate = Member(object, name);
            if (candidate.IsArray()) {
                return candidate;
            }
        }
        return {};
    }

    std::vector<Found> Discover(RE::GFxMovieView& view)
    {
        std::vector<Found> found;
        RE::GFxValue root;
        if (!view.GetVariable(&root, "_root")) {
            return found;
        }
        struct Node
        {
            std::string path;
            RE::GFxValue value;
            int depth;
        };
        std::vector<Node> queue{ { "_root", root, 0 } };
        int visited = 0;
        for (std::size_t head = 0; head < queue.size() && visited < maxNodes; ++head) {
            ++visited;
            // Copy: pushing to the queue below may reallocate it.
            const auto node = queue[head];
            if (auto entries = EntriesOf(node.value); entries.IsArray()) {
                found.push_back({ node.path, node.value, entries });
            }
            if (node.depth >= maxDepth) {
                continue;
            }
            Names names;
            node.value.VisitMembers(&names);
            for (auto& [name, child] : names.children) {
                queue.push_back({ node.path + "." + name, child, node.depth + 1 });
            }
        }
        return found;
    }

    std::string EntryText(const RE::GFxValue& entry)
    {
        if (!entry.IsObject()) {
            return StringOf(entry);
        }
        for (const char* name : { "text", "label", "name", "displayName", "title" }) {
            const auto text = StringOf(Member(entry, name));
            if (!text.empty()) {
                return text;
            }
        }
        return {};
    }

    Json DescribeList(const Found& found)
    {
        const auto size = found.entries.GetArraySize();
        auto entries = Json::array();
        for (std::uint32_t i = 0; i < size && i < maxEntries; ++i) {
            RE::GFxValue entry;
            found.entries.GetElement(i, &entry);
            Json row{ { "index", i }, { "text", EntryText(entry) } };
            for (const char* flag : { "disabled", "enabled", "selected" }) {
                const auto value = Member(entry, flag);
                if (value.IsBool()) {
                    row[flag] = value.GetBool();
                }
            }
            entries.push_back(std::move(row));
        }
        const auto selected = Member(found.list, "selectedIndex");
        return Json{ { "path", found.path }, { "count", size }, { "selectedIndex", selected.IsNumber() ? Json(selected.GetNumber()) : Json(nullptr) },
            { "entries", entries } };
    }
}

DEVTOOLS_MODULE(MenuList)
{
    Register("UI.list", [](const Json& params) {
        AllowOnly(params, { "menu" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto view = RequireMovie(menuName);
        auto lists = Json::array();
        for (const auto& found : Discover(*view)) {
            if (found.entries.GetArraySize() > 0) {
                lists.push_back(DescribeList(found));
            }
        }
        return Json{ { "menu", menuName }, { "lists", lists } };
    }, { .thread = Thread::UI });

    Register("UI.select", [](const Json& params) {
        AllowOnly(params, { "menu", "path", "index", "text", "press" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto view = RequireMovie(menuName);
        const auto wanted = Optional<std::string>(params, "path", "");
        std::vector<Found> candidates;
        for (auto& found : Discover(*view)) {
            if (found.entries.GetArraySize() > 0 && (wanted.empty() || found.path == wanted)) {
                candidates.push_back(std::move(found));
            }
        }
        if (candidates.empty()) {
            throw RpcError(code::notFound, "No list with entries in " + menuName);
        }
        // Without a path the first list that has the requested text wins; by index, the first list.
        const Found* chosen = nullptr;
        int index = -1;
        for (const auto& candidate : candidates) {
            std::vector<std::string> labels;
            for (std::uint32_t i = 0; i < candidate.entries.GetArraySize(); ++i) {
                RE::GFxValue entry;
                candidate.entries.GetElement(i, &entry);
                labels.push_back(EntryText(entry));
            }
            try {
                index = PickIndex(params, labels, "list entry");
                chosen = &candidate;
                break;
            } catch (const RpcError& error) {
                if (&candidate == &candidates.back() || params.contains("index")) {
                    throw;
                }
            }
        }
        if (!chosen) {
            throw RpcError(code::notFound, "No list entry matches");
        }
        RE::GFxValue entry;
        chosen->entries.GetElement(static_cast<std::uint32_t>(index), &entry);
        auto list = chosen->list;
        list.SetMember("selectedIndex", RE::GFxValue(static_cast<double>(index)));
        bool pressed = false;
        if (Optional(params, "press", true)) {
            RE::GFxValue event;
            view->CreateObject(&event);
            event.SetMember("type", RE::GFxValue("itemPress"));
            event.SetMember("index", RE::GFxValue(static_cast<double>(index)));
            event.SetMember("entry", entry);
            event.SetMember("keyboardOrMouse", RE::GFxValue(0.0));
            pressed = list.Invoke("dispatchEvent", nullptr, &event, 1);
        }
        return Json{ { "menu", menuName }, { "path", chosen->path }, { "index", index }, { "text", EntryText(entry) }, { "pressed", pressed } };
    }, { .thread = Thread::UI });

    Register("UI.callDelegate", [](const Json& params) {
        AllowOnly(params, { "menu", "name", "args" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto name = Require<std::string>(params, "name");
        const auto args = Optional<Json>(params, "args", Json::array());
        if (!args.is_array() || args.size() > 16) {
            throw RpcError(code::invalidParams, "args is an array of up to 16 values");
        }
        const auto view = RequireMovie(menuName);
        std::vector<RE::GFxValue> values;
        for (const auto& arg : args) {
            values.push_back(FromJson(*view, arg));
        }
        return Json{ { "menu", menuName }, { "name", name }, { "called", CallDelegate(*view, name.c_str(), values) } };
    }, { .thread = Thread::UI });
}
