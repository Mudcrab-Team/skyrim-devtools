// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Moving the player between cells and reporting cell changes.
#include "console.h"
#include "events.h"
#include "game.h"
#include "methods.h"
#include "transition.h"

#include <algorithm>
#include <cctype>
#include <format>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    Json DescribeCell(RE::FormID id)
    {
        Json cell{ { "formId", std::format("{:08X}", id) } };
        if (const auto form = RE::TESForm::LookupByID(id)) {
            const auto name = form->GetName();
            const auto editorId = form->GetFormEditorID();
            cell["name"] = name && *name ? Json(name) : Json(nullptr);
            cell["editorId"] = editorId && *editorId ? Json(editorId) : Json(nullptr);
        }
        return cell;
    }

    class CellSink final : public RE::BSTEventSink<RE::BGSActorCellEvent>
    {
    public:
        RE::BSEventNotifyControl ProcessEvent(const RE::BGSActorCellEvent* event,
            RE::BSTEventSource<RE::BGSActorCellEvent>*) override
        {
            if (event && events::Subscribed("cellChange")) {
                auto data = DescribeCell(event->cellID);
                data["entered"] = event->flags == RE::BGSActorCellEvent::CellFlag::kEnter;
                events::Emit("cellChange", std::move(data));
            }
            return RE::BSEventNotifyControl::kContinue;
        }
    };
}

DEVTOOLS_MODULE(World)
{
    events::Declare("cellChange", "The player entered or left a cell: {formId, name, editorId, entered}");
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kDataLoaded) {
            static CellSink sink;
            if (const auto player = RE::PlayerCharacter::GetSingleton()) {
                player->AsBGSActorCellEventSource()->AddEventSink(&sink);
            }
        }
    });

    RegisterDeferred("World.centerOnCell", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "cell", "loadEpoch" });
        RequirePlayable(params);
        const auto cell = Require<std::string>(params, "cell");
        if (cell.empty() || cell.size() > 128 ||
            !std::ranges::all_of(cell, [](unsigned char c) { return std::isalnum(c) || c == '_'; })) {
            throw RpcError(code::invalidParams, "cell is an editor ID such as WhiterunBanneredMare");
        }
        const auto command = "coc " + cell;
        if (!transition::Wanted(command)) {
            reply->Ok({ { "cell", cell }, { "lines", console::Run(command, nullptr) } });
            return;
        }
        transition::Bounce(reply, [reply, cell, command] {
            reply->Ok({ { "cell", cell }, { "bounced", true }, { "lines", console::Run(command, nullptr) } });
        });
    }, { .thread = Thread::Main, .timeout = 90s });
}
