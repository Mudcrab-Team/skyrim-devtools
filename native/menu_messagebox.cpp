// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Read and answer the active message box by meaning: its text and its button labels.
#include "menu_util.h"
#include "methods.h"

#include <atomic>

namespace
{
    using namespace devtools;

    // The button the last message box shown by UI.showMessageBox was answered with, -1 until then.
    std::atomic_int lastAnswer = -1;

    void Answered(std::uint8_t button)
    {
        lastAnswer = button;
    }

    std::string Text(const RE::BSString& text)
    {
        return text.c_str() ? std::string(text.c_str()) : std::string();
    }

    struct Box
    {
        std::string text;
        std::vector<std::string> buttons;
        int cancelIndex = -1;
        bool cancellable = false;
    };

    Box Read()
    {
        auto& ui = RequireUI();
        const auto data = RE::MessageBoxMenu::GetCurrentMessageBoxData();
        if (!data || !ui.IsMenuOpen("MessageBoxMenu")) {
            throw RpcError(code::notFound, "No message box is open");
        }
        Box box{ Text(data->bodyText), {}, data->cancelButtonIndex, data->isCancellable };
        for (const auto& button : data->buttonText) {
            box.buttons.push_back(Text(button));
        }
        return box;
    }

    Json Describe(const Box& box)
    {
        auto buttons = Json::array();
        for (std::size_t i = 0; i < box.buttons.size(); ++i) {
            buttons.push_back({ { "index", i }, { "text", box.buttons[i] } });
        }
        return Json{ { "open", true }, { "text", box.text }, { "buttons", buttons },
            { "cancelIndex", box.cancelIndex }, { "cancellable", box.cancellable } };
    }
}

DEVTOOLS_MODULE(MenuMessageBox)
{
    // Lets a test raise a message box on demand, since few vanilla actions show one.
    Register("UI.showMessageBox", [](const Json& params) {
        AllowOnly(params, { "text", "buttons" });
        const auto text = Require<std::string>(params, "text");
        const auto buttons = Optional<std::vector<std::string>>(params, "buttons", { "OK" });
        if (buttons.empty() || buttons.size() > 4) {
            throw RpcError(code::invalidParams, "buttons has one to four labels");
        }
        lastAnswer = -1;
        const auto b = [&](std::size_t i) { return buttons[i].c_str(); };
        bool made = false;
        switch (buttons.size()) {
        case 1: made = RE::MessageBoxMenu::Create(text.c_str(), &Answered, 0, 0, 10, b(0)); break;
        case 2: made = RE::MessageBoxMenu::Create(text.c_str(), &Answered, 0, 0, 10, b(0), b(1)); break;
        case 3: made = RE::MessageBoxMenu::Create(text.c_str(), &Answered, 0, 0, 10, b(0), b(1), b(2)); break;
        default: made = RE::MessageBoxMenu::Create(text.c_str(), &Answered, 0, 0, 10, b(0), b(1), b(2), b(3)); break;
        }
        return Json{ { "created", made } };
    }, { .thread = Thread::UI });

    Register("UI.messageBox", [](const Json& params) {
        AllowOnly(params, {});
        if (!RequireUI().IsMenuOpen("MessageBoxMenu")) {
            return Json{ { "open", false }, { "lastAnswer", lastAnswer.load() } };
        }
        return Describe(Read());
    }, { .thread = Thread::UI });

    Register("UI.messageBoxSelect", [](const Json& params) {
        AllowOnly(params, { "index", "text" });
        const auto box = Read();
        const int index = menu::PickIndex(params, box.buttons, "button");
        RE::MessageBoxMenu::SelectOption(index);
        return Json{ { "answer", lastAnswer.load() }, { "selected", index }, { "text", box.buttons[index] }, { "messageText", box.text } };
    }, { .thread = Thread::UI });
}
