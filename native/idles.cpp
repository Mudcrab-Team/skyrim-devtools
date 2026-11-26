// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Idle animations an actor can play: which ones its conditions allow right now, and playing or
// interrupting one. Useful for checking a mod's custom idles on a real NPC without a script.
#include "state_common.h"

#include <algorithm>
#include <cctype>

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

    RE::Actor* RequireActor(const Json& params)
    {
        const auto ref = RequireRef(params, "ref");
        const auto actor = ref->As<RE::Actor>();
        if (!actor) {
            throw RpcError(code::invalidParams, "ref is not an actor");
        }
        if (!actor->Is3DLoaded()) {
            throw RpcError(code::notReady, "The actor is not loaded (its 3D is not in the world); move close to it first");
        }
        return actor;
    }

    RE::TESIdleForm* RequireIdle(const Json& params)
    {
        Json key = Json::object();
        if (params.contains("idle")) {
            const auto& idle = params["idle"];
            if (idle.is_string() && !idle.get<std::string>().empty() && !std::isxdigit(static_cast<unsigned char>(idle.get<std::string>()[0]))) {
                key["editorID"] = idle;
            } else if (idle.is_string() || idle.is_number()) {
                key["formID"] = idle;
            } else if (idle.is_object()) {
                key = idle;
            }
        }
        if (key.empty()) {
            throw RpcError(code::invalidParams, "Give idle (a form ID or editor ID)");
        }
        const auto form = RequireForm(key);
        const auto idle = form->As<RE::TESIdleForm>();
        if (!idle) {
            throw RpcError(code::invalidParams, "That form is not an idle");
        }
        return idle;
    }

    Json DescribeIdle(RE::TESIdleForm* idle)
    {
        Json out{ { "formID", Hex(idle->GetFormID()) }, { "editorID", Name(idle->GetFormEditorID()) },
            { "event", Name(idle->animEventName.c_str()) }, { "file", Name(idle->animFileName.c_str()) } };
        if (const auto file = idle->GetFile(0)) {
            out["plugin"] = std::string(file->GetFilename());
        }
        if (idle->parentIdle) {
            out["parent"] = Hex(idle->parentIdle->GetFormID());
        }
        return out;
    }

    class PlayResult final : public RE::BSScript::IStackCallbackFunctor
    {
    public:
        PlayResult(ReplyPtr reply, Json envelope) : reply_(std::move(reply)), envelope_(std::move(envelope)) {}

        void operator()(RE::BSScript::Variable result) override
        {
            // Actor.PlayIdle answers whether the actor accepted the idle.
            envelope_["accepted"] = result.IsBool() ? result.GetBool() : true;
            reply_->Ok(envelope_);
        }

        void SetObject(const RE::BSTSmartPointer<RE::BSScript::Object>&) override {}

    private:
        ReplyPtr reply_;
        Json envelope_;
    };
}

DEVTOOLS_MODULE(Idles)
{
    Register("Idles.list", [](const Json& params) {
        AllowOnly(params, { "ref", "filter", "plugin", "playable", "offset", "limit" });
        const auto actor = RequireActor(params);
        const auto filter = Lower(Optional<std::string>(params, "filter", ""));
        const auto plugin = Lower(Optional<std::string>(params, "plugin", ""));
        const auto playable = Optional<bool>(params, "playable", true);
        const auto offset = Optional<std::size_t>(params, "offset", 0);
        const auto limit = std::clamp(Optional<std::size_t>(params, "limit", 50), std::size_t{ 1 }, std::size_t{ 300 });
        const auto data = RE::TESDataHandler::GetSingleton();
        if (!data) {
            throw RpcError(code::notReady, "The data handler is not available yet");
        }

        std::vector<RE::TESIdleForm*> found;
        for (const auto idle : data->GetFormArray<RE::TESIdleForm>()) {
            if (!idle || idle->IsDeleted()) {
                continue;
            }
            if (!filter.empty()) {
                const auto id = Lower(idle->GetFormEditorID() ? idle->GetFormEditorID() : "");
                const auto event = Lower(idle->animEventName.c_str() ? idle->animEventName.c_str() : "");
                if (id.find(filter) == std::string::npos && event.find(filter) == std::string::npos) {
                    continue;
                }
            }
            if (!plugin.empty()) {
                const auto file = idle->GetFile(0);
                if (!file || Lower(std::string(file->GetFilename())).find(plugin) == std::string::npos) {
                    continue;
                }
            }
            if (playable && !idle->CheckConditions(actor, nullptr, false)) {
                continue;
            }
            found.push_back(idle);
        }
        std::ranges::sort(found, [](auto* a, auto* b) { return a->GetFormID() < b->GetFormID(); });
        auto idles = Json::array();
        for (std::size_t i = offset; i < found.size() && idles.size() < limit; ++i) {
            idles.push_back(DescribeIdle(found[i]));
        }
        return Json{ { "actor", DescribeRef(actor, nullptr) }, { "total", found.size() }, { "offset", offset },
            { "conditionsChecked", playable }, { "idles", std::move(idles) } };
    }, { .thread = Thread::Main, .timeout = 20s });

    RegisterDeferred("Idles.play", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "ref", "idle", "loadEpoch" });
        RequirePlayable(params);
        const auto actor = RequireActor(params);
        const auto idle = RequireIdle(params);
        const auto vm = RE::BSScript::Internal::VirtualMachine::GetSingleton();
        if (!vm || !vm->initialized) {
            throw RpcError(code::notReady, "The Papyrus VM is not running yet");
        }
        // The same entry point a script's PlayIdle uses, so conditions, furniture and weapon state apply.
        const auto policy = vm->GetObjectHandlePolicy();
        const auto handle = policy->GetHandleForObject(static_cast<RE::VMTypeID>(actor->GetFormType()), actor);
        RE::BSTSmartPointer<RE::BSScript::IStackCallbackFunctor> callback(
            new PlayResult(reply, Json{ { "actor", Hex(actor->GetFormID()) }, { "idle", DescribeIdle(idle) } }));
        if (!vm->DispatchMethodCall(handle, "Actor", "PlayIdle", RE::MakeFunctionArguments(static_cast<RE::TESIdleForm*>(idle)), callback)) {
            throw RpcError(code::internal, "The VM rejected Actor.PlayIdle");
        }
    }, { .thread = Thread::Main, .timeout = 10s });

    Register("Idles.stop", [](const Json& params) {
        AllowOnly(params, { "ref" });
        const auto actor = RequireActor(params);
        // The graph event Papyrus' Debug.SendAnimationEvent uses to drop back to the default idle.
        const bool sent = actor->NotifyAnimationGraph("IdleForceDefaultState");
        return Json{ { "actor", Hex(actor->GetFormID()) }, { "sent", sent } };
    }, { .thread = Thread::Main });
}
