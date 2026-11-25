// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Raw Scaleform access to any open menu: read, write, call and dump ActionScript values.
#include "menu_util.h"
#include "methods.h"

#include <algorithm>
#include <cmath>
#include <map>

namespace devtools::menu
{
    namespace
    {
        std::string Narrow(const wchar_t* text)
        {
            if (!text) {
                return {};
            }
            const int length = WideCharToMultiByte(CP_UTF8, 0, text, -1, nullptr, 0, nullptr, nullptr);
            if (length <= 1) {
                return {};
            }
            std::string out(static_cast<std::size_t>(length - 1), '\0');
            WideCharToMultiByte(CP_UTF8, 0, text, -1, out.data(), length, nullptr, nullptr);
            return out;
        }

        // The game stores some strings with invalid UTF-8; JSON must stay valid.
        std::string Clean(std::string text)
        {
            if (text.size() > maxString) {
                text.resize(maxString);
                text += "...";
            }
            return Json::parse(Json(text).dump(-1, ' ', false, Json::error_handler_t::replace)).get<std::string>();
        }

        bool Numeric(const std::string& step, std::uint32_t& index)
        {
            if (step.empty() || !std::ranges::all_of(step, [](unsigned char c) { return std::isdigit(c) != 0; })) {
                return false;
            }
            index = static_cast<std::uint32_t>(std::stoul(step));
            return true;
        }

        std::vector<std::string> Steps(const std::string& path)
        {
            std::vector<std::string> steps;
            std::string current;
            const auto flush = [&] {
                if (!current.empty()) {
                    steps.push_back(current);
                    current.clear();
                }
            };
            for (const char c : path) {
                if (c == '.' || c == '[' || c == ']') {
                    flush();
                } else {
                    current += c;
                }
            }
            flush();
            return steps;
        }

        class Collector final : public RE::GFxValue::ObjectInterface::ObjVisitor
        {
        public:
            void Visit(const char* name, const RE::GFxValue& value) override { members.emplace_back(name ? name : "", value); }
            std::vector<std::pair<std::string, RE::GFxValue>> members;
        };
    }

    RE::GPtr<RE::IMenu> RequireMenu(const std::string& name)
    {
        auto& ui = RequireUI();
        if (ui.menuMap.find(RE::BSFixedString(name)) == ui.menuMap.end()) {
            throw RpcError(code::notFound, "No menu is registered as " + name);
        }
        auto menu = ui.GetMenu(name);
        if (!menu || !menu->uiMovie) {
            throw RpcError(code::notReady, name + " is not open");
        }
        return menu;
    }

    RE::GPtr<RE::GFxMovieView> RequireMovie(const std::string& name)
    {
        return RequireMenu(name)->uiMovie;
    }

    std::string StringOf(const RE::GFxValue& value)
    {
        if (value.IsString()) {
            return value.GetString() ? value.GetString() : "";
        }
        if (value.IsStringW()) {
            return Narrow(value.GetStringW());
        }
        if (value.IsNumber()) {
            return Json(value.GetNumber()).dump();
        }
        if (value.IsBool()) {
            return value.GetBool() ? "true" : "false";
        }
        return {};
    }

    RE::GFxValue Member(const RE::GFxValue& object, const char* name)
    {
        RE::GFxValue out;
        if (object.IsObject()) {
            object.GetMember(name, &out);
        }
        return out;
    }

    Json ToJson(const RE::GFxValue& value, int depth, Budget& budget)
    {
        if (--budget.nodes < 0) {
            budget.truncated = true;
            return "[budget]";
        }
        using T = RE::GFxValue::ValueType;
        const auto kind = static_cast<T>(static_cast<int>(value.GetType()) & 0x0F);
        switch (kind) {
        case T::kUndefined:
            return nullptr;
        case T::kNull:
            return nullptr;
        case T::kBoolean:
            return value.GetBool();
        case T::kNumber: {
            const double number = value.GetNumber();
            if (std::isfinite(number) && number == std::floor(number) && std::fabs(number) < 9e15) {
                return static_cast<std::int64_t>(number);
            }
            return std::isfinite(number) ? Json(number) : Json(nullptr);
        }
        case T::kString:
        case T::kStringW:
            return Clean(StringOf(value));
        case T::kArray: {
            const auto size = value.GetArraySize();
            if (depth <= 0) {
                budget.truncated = true;
                return "[Array " + std::to_string(size) + "]";
            }
            auto out = Json::array();
            for (std::uint32_t i = 0; i < size && i < 500; ++i) {
                RE::GFxValue item;
                value.GetElement(i, &item);
                out.push_back(ToJson(item, depth - 1, budget));
            }
            if (size > 500) {
                budget.truncated = true;
            }
            return out;
        }
        case T::kObject:
        case T::kDisplayObject: {
            const bool display = kind == T::kDisplayObject;
            if (depth <= 0) {
                budget.truncated = true;
                return display ? "[MovieClip]" : "[Object]";
            }
            Collector collector;
            value.VisitMembers(&collector);
            Json out = Json::object();
            for (auto& [name, member] : collector.members) {
                out[name] = ToJson(member, depth - 1, budget);
            }
            if (display) {
                // Text fields do not enumerate their text, and it is what a reader wants most.
                if (!out.contains("text")) {
                    if (const auto text = Member(value, "text"); text.IsString() || text.IsStringW()) {
                        out["text"] = Clean(StringOf(text));
                    }
                }
                out["@display"] = true;
            }
            return out;
        }
        default:
            return nullptr;
        }
    }

    RE::GFxValue FromJson(RE::GFxMovieView& view, const Json& json)
    {
        RE::GFxValue out;
        if (json.is_null()) {
            return RE::GFxValue(nullptr);
        }
        if (json.is_boolean()) {
            return RE::GFxValue(json.get<bool>());
        }
        if (json.is_number()) {
            return RE::GFxValue(json.get<double>());
        }
        if (json.is_string()) {
            view.CreateString(&out, json.get<std::string>().c_str());
            return out;
        }
        if (json.is_array()) {
            view.CreateArray(&out);
            for (const auto& item : json) {
                out.PushBack(FromJson(view, item));
            }
            return out;
        }
        view.CreateObject(&out);
        for (const auto& [key, item] : json.items()) {
            out.SetMember(key.c_str(), FromJson(view, item));
        }
        return out;
    }

    bool Resolve(RE::GFxMovieView& view, const std::string& path, RE::GFxValue& out)
    {
        const auto steps = Steps(path);
        if (steps.empty()) {
            return false;
        }
        std::size_t next = 1;
        if (!view.GetVariable(&out, steps.front().c_str())) {
            // `_root` is implicit: a bare member name is looked up on it.
            if (!view.GetVariable(&out, "_root")) {
                return false;
            }
            next = 0;
        }
        for (; next < steps.size(); ++next) {
            RE::GFxValue child;
            std::uint32_t index = 0;
            if (out.IsArray() && Numeric(steps[next], index)) {
                if (!out.GetElement(index, &child)) {
                    return false;
                }
            } else if (!out.IsObject() || !out.GetMember(steps[next].c_str(), &child)) {
                return false;
            }
            out = child;
        }
        return true;
    }

    std::pair<std::string, std::string> SplitLast(const std::string& path)
    {
        const auto steps = Steps(path);
        if (steps.empty()) {
            return {};
        }
        std::string parent;
        for (std::size_t i = 0; i + 1 < steps.size(); ++i) {
            parent += (parent.empty() ? "" : ".") + steps[i];
        }
        return { parent, steps.back() };
    }

    int PickIndex(const Json& params, const std::vector<std::string>& labels, const char* what)
    {
        const auto lower = [](std::string text) {
            std::ranges::transform(text, text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            return text;
        };
        if (params.contains("index") && !params["index"].is_null()) {
            const auto index = Require<int>(params, "index");
            if (index < 0 || index >= static_cast<int>(labels.size())) {
                throw RpcError(code::invalidParams, std::string("index is outside the ") + what + " list",
                    { { "count", labels.size() } });
            }
            return index;
        }
        if (!params.contains("text")) {
            throw RpcError(code::invalidParams, "Give index or text");
        }
        const auto wanted = lower(Require<std::string>(params, "text"));
        std::vector<int> exact;
        std::vector<int> partial;
        for (std::size_t i = 0; i < labels.size(); ++i) {
            const auto label = lower(labels[i]);
            if (label == wanted) {
                exact.push_back(static_cast<int>(i));
            } else if (label.find(wanted) != std::string::npos) {
                partial.push_back(static_cast<int>(i));
            }
        }
        const auto& hits = exact.empty() ? partial : exact;
        if (hits.size() != 1) {
            throw RpcError(code::invalidParams,
                hits.empty() ? std::string("No ") + what + " matches that text" : std::string("Several ") + what + " entries match that text",
                { { "matches", hits } });
        }
        return hits.front();
    }

    bool CallDelegate(RE::GFxMovieView& view, const char* name, const std::vector<RE::GFxValue>& args)
    {
        RE::GFxValue list;
        view.CreateArray(&list);
        for (const auto& arg : args) {
            list.PushBack(arg);
        }
        RE::GFxValue delegate;
        if (!view.GetVariable(&delegate, "_global.gfx.io.GameDelegate")) {
            return false;
        }
        const RE::GFxValue call[2]{ RE::GFxValue(name), list };
        return delegate.Invoke("call", nullptr, call, 2);
    }
}

namespace
{
    using namespace devtools;
    using namespace devtools::menu;

    int Depth(const Json& params, int fallback)
    {
        const auto depth = Optional<int>(params, "depth", fallback);
        if (depth < 0 || depth > 10) {
            throw RpcError(code::invalidParams, "depth is between 0 and 10");
        }
        return depth;
    }

    RE::GFxValue Need(RE::GFxMovieView& view, const std::string& menu, const std::string& path)
    {
        RE::GFxValue value;
        if (!Resolve(view, path, value)) {
            throw RpcError(code::notFound, "No value at " + path + " in " + menu);
        }
        return value;
    }

    Json Described(const std::string& menu, const std::string& path, const RE::GFxValue& value, int depth)
    {
        Budget budget;
        auto out = Json{ { "menu", menu }, { "path", path } };
        out["value"] = ToJson(value, depth, budget);
        out["truncated"] = budget.truncated;
        return out;
    }
}

DEVTOOLS_MODULE(MenuGfx)
{
    Register("UI.getVariable", [](const Json& params) {
        AllowOnly(params, { "menu", "path", "depth" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto path = Require<std::string>(params, "path");
        const auto view = RequireMovie(menuName);
        return Described(menuName, path, Need(*view, menuName, path), Depth(params, 1));
    }, { .thread = Thread::UI });

    Register("UI.dump", [](const Json& params) {
        AllowOnly(params, { "menu", "path", "depth" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto path = Optional<std::string>(params, "path", "_root");
        const auto view = RequireMovie(menuName);
        return Described(menuName, path, Need(*view, menuName, path), Depth(params, 3));
    }, { .thread = Thread::UI });

    Register("UI.setVariable", [](const Json& params) {
        AllowOnly(params, { "menu", "path", "value" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto path = Require<std::string>(params, "path");
        if (!params.contains("value")) {
            throw RpcError(code::invalidParams, "Missing parameter 'value'");
        }
        const auto view = RequireMovie(menuName);
        const auto [parentPath, last] = SplitLast(path);
        RE::GFxValue parent = Need(*view, menuName, parentPath.empty() ? "_root" : parentPath);
        if (!parent.IsObject() || !parent.SetMember(last.c_str(), FromJson(*view, params["value"]))) {
            throw RpcError(code::invalidParams, "Cannot set " + path + " in " + menuName);
        }
        return Described(menuName, path, Need(*view, menuName, path), 1);
    }, { .thread = Thread::UI });

    Register("UI.invoke", [](const Json& params) {
        AllowOnly(params, { "menu", "path", "args", "depth" });
        const auto menuName = Require<std::string>(params, "menu");
        const auto path = Require<std::string>(params, "path");
        const auto args = Optional<Json>(params, "args", Json::array());
        if (!args.is_array() || args.size() > 16) {
            throw RpcError(code::invalidParams, "args is an array of up to 16 values");
        }
        const auto view = RequireMovie(menuName);
        const auto [parentPath, last] = SplitLast(path);
        RE::GFxValue parent = Need(*view, menuName, parentPath.empty() ? "_root" : parentPath);
        std::vector<RE::GFxValue> values;
        for (const auto& arg : args) {
            values.push_back(FromJson(*view, arg));
        }
        RE::GFxValue result;
        if (!parent.IsObject() || !parent.Invoke(last.c_str(), &result, values.data(), values.size())) {
            throw RpcError(code::invalidParams, "Cannot call " + path + " in " + menuName);
        }
        Budget budget;
        return Json{ { "menu", menuName }, { "path", path }, { "result", ToJson(result, Depth(params, 2), budget) },
            { "truncated", budget.truncated } };
    }, { .thread = Thread::UI });
}
