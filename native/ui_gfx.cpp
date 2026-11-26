// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Finding values inside an open menu's Scaleform tree. UI.getVariable/setVariable/invoke address a value
// by path; this locates the path of a value an agent only knows by its label or member name, which is
// the usual situation with a mod menu (an MCM page has no documented structure).
#include "menu_util.h"
#include "methods.h"

#include <algorithm>
#include <cctype>

namespace
{
    using namespace devtools;
    using namespace devtools::menu;

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }

    class Collector final : public RE::GFxValue::ObjectInterface::ObjVisitor
    {
    public:
        void Visit(const char* name, const RE::GFxValue& value) override { members.emplace_back(name ? name : "", value); }
        std::vector<std::pair<std::string, RE::GFxValue>> members;
    };

    struct Search
    {
        std::string name;  // lower case member-name substring; empty matches any
        std::string text;  // lower case substring of a string value or of a text field's text; empty matches any
        std::size_t limit = 50;
        int visited = 0;
        int budget = 6000;
        bool truncated = false;
        Json hits = Json::array();
    };

    bool Matches(const Search& search, const std::string& member, const RE::GFxValue& value)
    {
        if (!search.name.empty() && Lower(member).find(search.name) == std::string::npos) {
            return false;
        }
        if (search.text.empty()) {
            return true;
        }
        RE::GFxValue shown = value;
        if (value.IsObject()) {
            // Text fields keep their label in `text`, which a member visit does not list.
            shown = Member(value, "text");
        }
        const auto content = StringOf(shown);
        return !content.empty() && Lower(content).find(search.text) != std::string::npos;
    }

    void Walk(Search& search, const std::string& path, const RE::GFxValue& value, int depth)
    {
        if (search.hits.size() >= search.limit) {
            search.truncated = true;
            return;
        }
        if (--search.budget < 0) {
            search.truncated = true;
            return;
        }
        ++search.visited;
        if (value.IsArray()) {
            if (depth <= 0) {
                return;
            }
            const auto size = std::min<std::uint32_t>(value.GetArraySize(), 300);
            for (std::uint32_t i = 0; i < size; ++i) {
                RE::GFxValue item;
                if (value.GetElement(i, &item)) {
                    Walk(search, path + "." + std::to_string(i), item, depth - 1);
                }
            }
            return;
        }
        if (!value.IsObject() || depth <= 0) {
            return;
        }
        Collector collector;
        value.VisitMembers(&collector);
        for (auto& [member, child] : collector.members) {
            const auto childPath = path + "." + member;
            if (Matches(search, member, child)) {
                Budget budget{ 40 };
                auto hit = Json{ { "path", childPath } };
                hit["value"] = ToJson(child, 0, budget);
                if (const auto text = Member(child, "text"); text.IsString() || text.IsStringW()) {
                    hit["text"] = StringOf(text);
                }
                search.hits.push_back(std::move(hit));
                if (search.hits.size() >= search.limit) {
                    search.truncated = true;
                    return;
                }
            }
            Walk(search, childPath, child, depth - 1);
        }
    }
}

DEVTOOLS_MODULE(UiGfx)
{
    Register("UI.find", [](const Json& params) {
        AllowOnly(params, { "menu", "path", "name", "text", "depth", "limit" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto path = Optional<std::string>(params, "path", "_root");
        Search search;
        search.name = Lower(Optional<std::string>(params, "name", ""));
        search.text = Lower(Optional<std::string>(params, "text", ""));
        if (search.name.empty() && search.text.empty()) {
            throw RpcError(code::invalidParams, "Give name (a member name substring) or text (a label substring)");
        }
        const auto depth = Optional<int>(params, "depth", 6);
        const auto limit = Optional<int>(params, "limit", 50);
        if (depth < 1 || depth > 12 || limit < 1 || limit > 500) {
            throw RpcError(code::invalidParams, "depth is 1 to 12 and limit is 1 to 500");
        }
        search.limit = static_cast<std::size_t>(limit);
        const auto view = RequireMovie(menuName);
        RE::GFxValue root;
        if (!Resolve(*view, path, root)) {
            throw RpcError(code::notFound, "No value at " + path + " in " + menuName);
        }
        Walk(search, path, root, depth);
        return Json{ { "menu", menuName }, { "from", path }, { "count", search.hits.size() }, { "visited", search.visited },
            { "truncated", search.truncated }, { "hits", std::move(search.hits) } };
    }, { .thread = Thread::UI, .timeout = std::chrono::seconds(15) });
}
