// SPDX-License-Identifier: GPL-3.0-or-later
// Host-side checks of the save location rule; built and run by test_save_path.py.
#include "../native/save_path.h"

#include <cstdio>

using devtools::savepath::Classify;
using devtools::savepath::Layout;

namespace
{
    int failures = 0;

    void Check(bool ok, const char* what)
    {
        if (!ok) {
            std::printf("FAIL %s\n", what);
            ++failures;
        }
    }

    const std::filesystem::path root = std::filesystem::path("/work") / "mo2";
}

int main()
{
    Check(Classify("Saves\\", {}, false) == Layout::Isolated, "plain Saves folder");
    Check(Classify("saves", {}, false) == Layout::Isolated, "plain saves, no separator");
    Check(Classify("Saves\\Other\\", {}, false) == Layout::Refused, "nested folder");
    Check(Classify("", {}, false) == Layout::Refused, "empty setting");

    const auto clone = root / "profiles" / "Modlist-devtools" / "saves";
    Check(Classify("__MO_Saves\\", clone, false) == Layout::Mo2Local, "MO2 devtools clone");
    Check(Classify("__mo_saves", clone, false) == Layout::Mo2Local, "case and separator are loose");
    Check(Classify("__MO_Saves\\", root / "profiles" / "Modlist-DevTools" / "SAVES", false) == Layout::Mo2Local, "folder case is loose");
    Check(Classify("__MO_Saves\\", clone.string() + "/", false) == Layout::Mo2Local, "trailing separator on the folder");

    const auto played = root / "profiles" / "Modlist" / "saves";
    Check(Classify("__MO_Saves\\", played, false) == Layout::Refused, "a played profile is refused");
    Check(Classify("__MO_Saves\\", played, true) == Layout::Mo2Local, "the marker file accepts a profile");
    Check(Classify("__MO_Saves\\", {}, true) == Layout::Refused, "no folder named by the host");
    Check(Classify("__MO_Saves\\", "relative/profiles/X-devtools/saves", false) == Layout::Refused, "relative folder");
    Check(Classify("__MO_Saves\\", root / "profiles" / "X-devtools" / "other", false) == Layout::Refused, "not a saves folder");
    Check(Classify("__MO_Saves\\", root / "elsewhere" / "X-devtools" / "saves", false) == Layout::Refused, "not under profiles");
    Check(Classify("__MO_Saves\\", root / "profiles" / "-devtools" / "saves", false) == Layout::Refused, "suffix alone is no name");
    Check(Classify("__MO_Saves\\", root / "profiles" / "X-devtools" / ".." / "Played" / "saves", false) == Layout::Refused, "dot-dot escape");
    Check(Classify("Documents\\Saves", clone, true) == Layout::Refused, "an arbitrary redirected path");
    return failures ? 1 : 0;
}
