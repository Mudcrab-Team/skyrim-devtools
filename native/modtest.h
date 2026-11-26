// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Helpers shared by the dialogue explorer and the coverage report: which plugin a record comes from,
// plugin-name filters, and condition evaluation for dialogue infos.
#pragma once

#include "game.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <string>
#include <vector>

namespace devtools::modtest
{
    inline std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }

    inline std::int64_t UnixMs()
    {
        using namespace std::chrono;
        return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
    }

    // The plugin that created the record (not the last one to override it); empty for runtime forms.
    inline std::string Plugin(const RE::TESForm* form)
    {
        if (!form) {
            return {};
        }
        const auto file = form->GetFile(0);
        return file ? std::string(file->GetFilename()) : std::string();
    }

    // Case-insensitive plugin names; an empty filter matches everything.
    struct PluginFilter
    {
        std::vector<std::string> names;

        static PluginFilter From(const std::vector<std::string>& plugins)
        {
            PluginFilter filter;
            for (const auto& name : plugins) {
                filter.names.push_back(Lower(name));
            }
            return filter;
        }

        [[nodiscard]] bool Matches(const RE::TESForm* form) const
        {
            return names.empty() || std::ranges::find(names, Lower(Plugin(form))) != names.end();
        }
    };

    using Function = RE::FUNCTION_DATA::FunctionID;

    // Conditions that say who may speak a line. Dialogue is normally addressed to its NPC through these.
    inline bool IsIdentity(Function function)
    {
        switch (function) {
        case Function::kGetIsID:
        case Function::kGetIsRace:
        case Function::kGetIsSex:
        case Function::kGetInFaction:
        case Function::kGetIsClass:
        case Function::kGetIsVoiceType:
            return true;
        default:
            return false;
        }
    }

    struct Verdict
    {
        bool pass = true;
        bool hasIdentity = false;
        std::vector<int> failing;  // function indexes of the items that were false
    };

    // Items run in order; an item flagged OR closes its AND group. With identityOnly every other item
    // counts as true, which answers "is this line addressed to the speaker" without the rest of its state.
    inline Verdict Evaluate(const RE::TESCondition& condition, RE::ConditionCheckParams& params, bool identityOnly)
    {
        Verdict verdict;
        bool group = true;
        bool decided = false;
        for (const auto* item = condition.head; item; item = item->next) {
            const auto function = item->data.functionData.function.get();
            const bool identity = IsIdentity(function);
            verdict.hasIdentity = verdict.hasIdentity || identity;
            bool value = true;
            if (!identityOnly || identity) {
                value = (*item)(params);
                if (!value) {
                    verdict.failing.push_back(static_cast<int>(function));
                }
            }
            group = group && value;
            if (item->data.flags.isOR) {
                if (group) {
                    decided = true;
                    break;
                }
                group = true;
            }
        }
        verdict.pass = decided || group;
        return verdict;
    }
}
