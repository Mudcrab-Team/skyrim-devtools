// SPDX-License-Identifier: GPL-3.0-or-later
// Decides whether the game's configured save location is one a test may load from. Kept free of game
// types so it can be tested off the PC.
#pragma once

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <string>
#include <string_view>

namespace devtools::savepath
{
    enum class Layout
    {
        Isolated,  // the host's own profile: sLocalSavePath is the plain Saves folder
        Mo2Local,  // MO2 profile-local saves, reached through the __MO_Saves virtual folder
        Refused,
    };

    // Marker file a profile folder may carry to declare that it is a throwaway test profile.
    inline constexpr const char* profileMarker = "skyrim-devtools-profile";

    inline std::string Lower(std::string text)
    {
        std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }

    // The setting value without trailing separators, so "Saves\\" and "Saves" compare equal.
    inline std::string TrimSeparators(std::string_view text)
    {
        while (!text.empty() && (text.back() == '\\' || text.back() == '/')) {
            text.remove_suffix(1);
        }
        return std::string(text);
    }

    // The real saves are redirected by MO2 into <instance>/profiles/<profile>/saves, and the game only ever
    // sees the virtual name, so the folder the host names is the only way to learn which profile is active.
    // That profile is accepted only when it is recognisably a test profile (its name ends in -devtools, or it
    // carries the marker file), so a user's own profile with local saves is never loaded from or listed.
    inline bool IsTestProfileSaves(const std::filesystem::path& savesDir, bool markerPresent)
    {
        if (savesDir.empty() || !savesDir.is_absolute()) {
            return false;
        }
        const auto saves = savesDir.lexically_normal();
        const auto folder = saves.has_filename() ? saves : saves.parent_path();
        const auto profile = folder.parent_path();
        if (Lower(folder.filename().string()) != "saves" || profile.empty() || profile.parent_path().empty() ||
            Lower(profile.parent_path().filename().string()) != "profiles") {
            return false;
        }
        const auto name = Lower(profile.filename().string());
        constexpr std::string_view suffix = "-devtools";
        const bool named = name.size() > suffix.size() && name.compare(name.size() - suffix.size(), suffix.size(), suffix) == 0;
        return named || markerPresent;
    }

    inline Layout Classify(std::string_view localSavePath, const std::filesystem::path& savesDir, bool markerPresent)
    {
        const auto path = Lower(TrimSeparators(localSavePath));
        if (path == "saves") {
            return Layout::Isolated;
        }
        if (path == "__mo_saves") {
            return IsTestProfileSaves(savesDir, markerPresent) ? Layout::Mo2Local : Layout::Refused;
        }
        return Layout::Refused;
    }
}
