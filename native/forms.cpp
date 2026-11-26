// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Searching the loaded form table and reading a form's metadata. Form.get answers for a form whose
// ID or editor ID is already known; these methods are for finding it when only a type, a name
// fragment or the owning plugin is known.
#include "state_common.h"

#include <algorithm>
#include <cctype>
#include <set>

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace std::chrono_literals;

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }

    bool Contains(const char* haystack, const std::string& lowerNeedle)
    {
        return haystack && Lower(haystack).find(lowerNeedle) != std::string::npos;
    }

    bool FromPlugin(RE::TESForm* form, const std::string& lowerPlugin)
    {
        const auto file = form->GetFile(0);
        return file && Contains(std::string(file->GetFilename()).c_str(), lowerPlugin);
    }

    // The names a `type` filter accepts: the engine's own form type names.
    std::set<std::string> KnownTypes()
    {
        std::set<std::string> names;
        for (int i = 0; i < 0x80; ++i) {
            const auto name = std::string(RE::FormTypeToString(static_cast<RE::FormType>(i)));
            if (!name.empty() && name != "None" && name != "<unknown>") {
                names.insert(Lower(name));
            }
        }
        return names;
    }

    Json Brief(RE::TESForm* form)
    {
        Json out{ { "formID", Hex(form->GetFormID()) }, { "type", FormTypeName(form) }, { "name", Name(form->GetName()) },
            { "editorID", Name(form->GetFormEditorID()) } };
        if (const auto file = form->GetFile(0)) {
            out["plugin"] = std::string(file->GetFilename());
        }
        return out;
    }

    Json Keywords(RE::TESForm* form)
    {
        auto out = Json::array();
        if (const auto keywordForm = form->As<RE::BGSKeywordForm>()) {
            keywordForm->ForEachKeyword([&](RE::BGSKeyword* keyword) {
                if (keyword && out.size() < 200) {
                    out.push_back(Json{ { "formID", Hex(keyword->GetFormID()) }, { "editorID", Name(keyword->GetFormEditorID()) } });
                }
                return RE::BSContainer::ForEachResult::kContinue;
            });
        }
        return out;
    }
}

DEVTOOLS_MODULE(Forms)
{
    Register("Forms.find", [](const Json& params) {
        AllowOnly(params, { "type", "editorID", "name", "plugin", "offset", "limit" });
        const auto type = Lower(Optional<std::string>(params, "type", ""));
        const auto editorId = Lower(Optional<std::string>(params, "editorID", ""));
        const auto name = Lower(Optional<std::string>(params, "name", ""));
        const auto plugin = Lower(Optional<std::string>(params, "plugin", ""));
        const auto offset = Optional<std::size_t>(params, "offset", 0);
        const auto limit = std::clamp(Optional<std::size_t>(params, "limit", 50), std::size_t{ 1 }, std::size_t{ 200 });
        if (type.empty() && editorId.empty() && name.empty() && plugin.empty()) {
            throw RpcError(code::invalidParams, "Give at least one of type, editorID (substring), name (substring), plugin (substring)");
        }
        if (!type.empty() && !KnownTypes().contains(type)) {
            throw RpcError(code::invalidParams, "type is a form type name such as Weapon, NPC, Quest, Keyword, Idle, Cell");
        }

        const auto matches = [&](RE::TESForm* form) {
            if (!form || form->IsDeleted()) {
                return false;
            }
            if (!type.empty() && Lower(FormTypeName(form)) != type) {
                return false;
            }
            if (!editorId.empty() && !Contains(form->GetFormEditorID(), editorId)) {
                return false;
            }
            if (!name.empty() && !Contains(form->GetName(), name)) {
                return false;
            }
            return plugin.empty() || FromPlugin(form, plugin);
        };

        std::vector<RE::FormID> ids;
        {
            const auto& [map, lock] = RE::TESForm::GetAllForms();
            const RE::BSReadWriteLock guard{ lock };
            if (map) {
                for (const auto& entry : *map) {
                    if (matches(entry.second)) {
                        ids.push_back(entry.first);
                    }
                }
            }
        }
        if (!editorId.empty()) {
            // Editor IDs the engine does not keep per form can still be in the by-ID table when a mod fills it.
            const auto& [map, lock] = RE::TESForm::GetAllFormsByEditorID();
            const RE::BSReadWriteLock guard{ lock };
            if (map) {
                for (const auto& entry : *map) {
                    if (entry.second && Contains(entry.first.c_str(), editorId) && (type.empty() || Lower(FormTypeName(entry.second)) == type) &&
                        (name.empty() || Contains(entry.second->GetName(), name)) && (plugin.empty() || FromPlugin(entry.second, plugin))) {
                        ids.push_back(entry.second->GetFormID());
                    }
                }
            }
        }
        std::ranges::sort(ids);
        ids.erase(std::unique(ids.begin(), ids.end()), ids.end());

        auto forms = Json::array();
        for (std::size_t i = offset; i < ids.size() && forms.size() < limit; ++i) {
            if (const auto form = RE::TESForm::LookupByID(ids[i])) {
                forms.push_back(Brief(form));
            }
        }
        return Json{ { "total", ids.size() }, { "offset", offset }, { "forms", std::move(forms) } };
    }, { .thread = Thread::Main, .timeout = 30s });

    Register("Forms.info", [](const Json& params) {
        AllowOnly(params, { "formID", "editorID" });
        const auto form = RequireForm(params);
        auto out = Brief(form);
        out["localID"] = Hex(form->GetLocalFormID());
        out["dynamic"] = form->IsDynamicForm();
        auto plugins = Json::array();
        if (const auto array = form->sourceFiles.array) {
            for (const auto file : *array) {
                if (file) {
                    plugins.push_back(std::string(file->GetFilename()));
                }
            }
        }
        // First is the plugin that defines the form; later entries override it, the last one wins.
        out["plugins"] = std::move(plugins);
        out["keywords"] = Keywords(form);
        if (const auto ref = form->AsReference()) {
            if (const auto base = ref->GetBaseObject()) {
                out["base"] = Json{ { "formID", Hex(base->GetFormID()) }, { "type", FormTypeName(base) }, { "name", Name(base->GetName()) } };
            }
            out["pos"] = Vec3(ref->GetPosition());
            out["cell"] = DescribeCell(ref->GetParentCell());
        }
        return out;
    }, { .thread = Thread::Main, .timeout = 15s });
}
