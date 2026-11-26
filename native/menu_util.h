// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Scaleform helpers shared by the menu modules. Include this only from DLL sources.
#pragma once

#include "game.h"

#include <string>
#include <string_view>
#include <vector>

namespace devtools::menu
{
    // Bounds every conversion of a Scaleform value so a huge object cannot flood a reply.
    struct Budget
    {
        int nodes = 4000;
        bool truncated = false;
    };

    constexpr std::size_t maxString = 600;

    // The movie of an open menu. Throws not-found when the menu is not registered and not-ready
    // when it is closed or its movie is not loaded yet. UI thread only.
    RE::GPtr<RE::GFxMovieView> RequireMovie(const std::string& name);
    RE::GPtr<RE::IMenu> RequireMenu(const std::string& name);

    Json ToJson(const RE::GFxValue& value, int depth, Budget& budget);
    RE::GFxValue FromJson(RE::GFxMovieView& view, const Json& json);

    // Walks `a.b.0.c` (numeric steps index arrays, `[n]` is accepted too) from `_root` or `_levelN`.
    bool Resolve(RE::GFxMovieView& view, const std::string& path, RE::GFxValue& out);
    // Splits a path into its parent path and last step.
    std::pair<std::string, std::string> SplitLast(const std::string& path);

    std::string StringOf(const RE::GFxValue& value);
    // Member lookup that returns an undefined value when the member is missing.
    RE::GFxValue Member(const RE::GFxValue& object, const char* name);

    // Picks an entry by `index` or by `text` (exact match ignoring case, else a unique substring).
    // `index` is a position in `labels`. Throws invalid-params when nothing or several match.
    int PickIndex(const Json& params, const std::vector<std::string>& labels, const char* what);

    // Calls a function registered with the menu's GameDelegate (what ActionScript reaches through
    // GameDelegate.call), so the menu runs its own native handler.
    bool CallDelegate(RE::GFxMovieView& view, const char* name, const std::vector<RE::GFxValue>& args);
}

namespace devtools::menu
{
    // Subtitles the game is showing right now, as [{speaker, text}].
    Json CurrentSubtitleLines();

    struct SubtitleLine
    {
        RE::ObjectRefHandle speaker;
        std::string name;
        std::string text;
    };
    // Safe from any thread: the subtitle manager is read under its own lock.
    std::vector<SubtitleLine> SubtitleSnapshot();
}
