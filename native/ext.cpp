// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// The extension API: methods and events contributed by other plugins (see ext/skyrim_devtools_ext.h and
// docs/extending.md). The C surface only translates; registration, threading and timeouts reuse the same
// registry and bus as the built-in modules.
#include "events.h"
#include "game.h"
#include "methods.h"
#include "skyrim_devtools_ext.h"

#include <Windows.h>

#include <algorithm>
#include <cctype>
#include <map>
#include <mutex>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;

    constexpr std::size_t maxName = 64;
    constexpr std::uint32_t maxTimeoutMs = 10 * 60 * 1000;

    // A reply handle is an id, not a pointer, so a plugin that answers twice or after the timeout cannot
    // touch freed memory.
    std::mutex pendingMutex;
    std::map<std::uintptr_t, ReplyPtr> pending;
    std::uintptr_t nextHandle = 1;

    std::mutex ownersMutex;
    std::map<std::string, std::string> eventOwners;

    bool ValidName(const char* text)
    {
        if (!text) {
            return false;
        }
        const std::string_view name(text);
        return !name.empty() && name.size() <= maxName && name.find('.') != std::string_view::npos &&
               name.front() != '.' && name.back() != '.' &&
               std::ranges::all_of(name, [](unsigned char c) { return std::isalnum(c) || c == '_' || c == '.'; });
    }

    bool ValidOwner(const char* text)
    {
        return text && *text && std::string_view(text).size() <= maxName;
    }

    void Changed(const char* what, const std::string& name)
    {
        events::Emit("methodsChanged", { { what, Json::array({ name }) } });
    }

    ReplyPtr TakeReply(SkyrimDevtoolsReply handle)
    {
        std::lock_guard lock(pendingMutex);
        const auto found = pending.find(reinterpret_cast<std::uintptr_t>(handle));
        if (found == pending.end()) {
            return nullptr;
        }
        auto reply = std::move(found->second);
        pending.erase(found);
        return reply;
    }

    void ReplyOk(SkyrimDevtoolsReply handle, const char* json)
    {
        if (const auto reply = TakeReply(handle)) {
            auto value = Json::parse(json ? json : "null", nullptr, false);
            if (value.is_discarded()) {
                reply->Fail(code::internal, "The extension answered with invalid JSON");
            } else {
                reply->Ok(std::move(value));
            }
        }
    }

    void ReplyFail(SkyrimDevtoolsReply handle, int errorCode, const char* message)
    {
        if (const auto reply = TakeReply(handle)) {
            reply->Fail(errorCode, message ? message : "The extension reported an error");
        }
    }

    int RegisterMethod(const char* owner, const SkyrimDevtoolsMethod* method)
    {
        if (!ValidOwner(owner) || !method || method->size < sizeof(SkyrimDevtoolsMethod) || !method->handler ||
            !ValidName(method->name) || method->thread > SKYRIM_DEVTOOLS_THREAD_UI || method->timeout_ms > maxTimeoutMs) {
            return SKYRIM_DEVTOOLS_ERR_ARGUMENT;
        }
        Json schema;
        if (method->schema_json) {
            schema = Json::parse(method->schema_json, nullptr, false);
            if (schema.is_discarded() || !schema.is_object()) {
                return SKYRIM_DEVTOOLS_ERR_SCHEMA;
            }
        }
        const std::string name = method->name;
        const std::string ownerName = owner;
        if (const auto existing = OwnerOf(name); existing && *existing != ownerName) {
            return SKYRIM_DEVTOOLS_ERR_CONFLICT;
        }
        const auto handler = method->handler;
        void* const user = method->user;
        const auto timeout = std::chrono::milliseconds(method->timeout_ms ? method->timeout_ms : 5000);
        RegisterDeferred(name,
            [handler, user](const Json& params, ReplyPtr reply) {
                std::uintptr_t id = 0;
                {
                    std::lock_guard lock(pendingMutex);
                    // Entries whose call already timed out would otherwise pile up behind a plugin that never answers.
                    std::erase_if(pending, [](const auto& entry) { return entry.second->Done(); });
                    id = nextHandle++;
                    pending.emplace(id, std::move(reply));
                }
                const auto text = params.dump(-1, ' ', false, Json::error_handler_t::replace);
                handler(text.c_str(), reinterpret_cast<SkyrimDevtoolsReply>(id), user);
            },
            { .thread = static_cast<Thread>(method->thread), .timeout = timeout },
            { method->description ? method->description : "", std::move(schema), ownerName });
        Changed("added", name);
        return SKYRIM_DEVTOOLS_OK;
    }

    int UnregisterMethod(const char* owner, const char* name)
    {
        if (!ValidOwner(owner) || !name) {
            return SKYRIM_DEVTOOLS_ERR_ARGUMENT;
        }
        if (!Unregister(name, owner)) {
            return SKYRIM_DEVTOOLS_ERR_UNKNOWN;
        }
        Changed("removed", name);
        return SKYRIM_DEVTOOLS_OK;
    }

    int DeclareEvent(const char* owner, const char* type, const char* description)
    {
        if (!ValidOwner(owner) || !ValidName(type)) {
            return SKYRIM_DEVTOOLS_ERR_ARGUMENT;
        }
        {
            std::lock_guard lock(ownersMutex);
            const auto [found, inserted] = eventOwners.emplace(type, owner);
            if (!inserted && found->second != owner) {
                return SKYRIM_DEVTOOLS_ERR_CONFLICT;
            }
        }
        events::Declare(type, description ? description : "");
        return SKYRIM_DEVTOOLS_OK;
    }

    int EmitEvent(const char* type, const char* dataJson)
    {
        if (!type) {
            return SKYRIM_DEVTOOLS_ERR_ARGUMENT;
        }
        {
            std::lock_guard lock(ownersMutex);
            if (!eventOwners.contains(type)) {
                return SKYRIM_DEVTOOLS_ERR_UNKNOWN;
            }
        }
        if (!events::Subscribed(type)) {
            return SKYRIM_DEVTOOLS_OK;
        }
        auto data = Json::parse(dataJson ? dataJson : "null", nullptr, false);
        if (data.is_discarded()) {
            return SKYRIM_DEVTOOLS_ERR_SCHEMA;
        }
        events::Emit(type, std::move(data));
        return SKYRIM_DEVTOOLS_OK;
    }

    const SkyrimDevtoolsApi api{ sizeof(SkyrimDevtoolsApi), SKYRIM_DEVTOOLS_API_VERSION, &RegisterMethod,
        &UnregisterMethod, &ReplyOk, &ReplyFail, &DeclareEvent, &EmitEvent };
}

extern "C" __declspec(dllexport) const SkyrimDevtoolsApi* SkyrimDevtools_GetApi(std::uint32_t version)
{
    return version == SKYRIM_DEVTOOLS_API_VERSION ? &api : nullptr;
}

DEVTOOLS_MODULE(Extensions)
{
    events::Declare("methodsChanged", "Methods were added or removed at runtime: {added: [names]} or {removed: [names]}");
    SetCapability("extensions", true);
    OnSkseMessage([](SKSE::MessagingInterface::Message& message) {
        if (message.type == SKSE::MessagingInterface::kPostPostLoad) {
            if (const auto messages = SKSE::GetMessagingInterface()) {
                messages->Dispatch(SKYRIM_DEVTOOLS_MESSAGE_API, const_cast<SkyrimDevtoolsApi*>(&api),
                    static_cast<std::uint32_t>(sizeof(api)), nullptr);
            }
        }
    });

    Register("Extensions.info", [](const Json& params) {
        AllowOnly(params, {});
        std::map<std::string, int> owners;
        for (const auto& method : ListMethods()) {
            if (method.contains("owner")) {
                ++owners[method["owner"].get<std::string>()];
            }
        }
        return Json{ { "apiVersion", SKYRIM_DEVTOOLS_API_VERSION }, { "owners", owners } };
    }, { .thread = Thread::Any });
}
