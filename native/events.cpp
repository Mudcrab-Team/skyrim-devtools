// SPDX-License-Identifier: GPL-3.0-or-later
#include "events.h"

#include "game.h"
#include "methods.h"

#include <map>
#include <mutex>
#include <set>
#include <shared_mutex>

namespace devtools::events
{
    namespace
    {
        std::shared_mutex mutex;
        std::map<std::string, std::string, std::less<>> declared;
        std::set<std::string, std::less<>> subscribed;
        std::shared_ptr<Outbox> current;
        std::atomic<std::uint64_t> sequence = 0;

        std::set<std::string, std::less<>> TypesFrom(const Json& params)
        {
            AllowOnly(params, { "types" });
            std::set<std::string, std::less<>> types;
            for (const auto& type : Require<std::vector<std::string>>(params, "types")) {
                if (type == "*") {
                    for (const auto& [name, description] : declared) {
                        types.insert(name);
                    }
                } else if (!declared.contains(type)) {
                    throw RpcError(code::notFound, "Unknown event type " + type);
                } else {
                    types.insert(type);
                }
            }
            return types;
        }
    }

    void Declare(std::string_view type, std::string_view description)
    {
        std::unique_lock lock(mutex);
        declared.emplace(type, description);
    }

    bool Subscribed(std::string_view type)
    {
        std::shared_lock lock(mutex);
        return current && subscribed.contains(type);
    }

    void Emit(std::string_view type, Json data)
    {
        std::shared_ptr<Outbox> outbox;
        {
            std::shared_lock lock(mutex);
            if (!current || !subscribed.contains(type)) {
                return;
            }
            outbox = current;
        }
        const Json notification{ { "jsonrpc", "2.0" }, { "method", "event" },
            { "params", { { "type", type }, { "seq", ++sequence }, { "epoch", state::epoch.load() },
                            { "data", std::move(data) } } } };
        outbox->Push(notification.dump(-1, ' ', false, Json::error_handler_t::replace), true);
    }

    void Attach(std::shared_ptr<Outbox> outbox)
    {
        std::unique_lock lock(mutex);
        subscribed.clear();
        current = std::move(outbox);
    }

    void CloseConnection()
    {
        std::shared_lock lock(mutex);
        if (current) {
            current->RequestClose();
        }
    }

    void Detach()
    {
        std::unique_lock lock(mutex);
        subscribed.clear();
        current.reset();
    }
}

DEVTOOLS_MODULE(Events)
{
    using namespace devtools;
    Register("Events.list", [](const Json& params) {
        AllowOnly(params, {});
        std::shared_lock lock(events::mutex);
        auto list = Json::array();
        for (const auto& [name, description] : events::declared) {
            list.push_back({ { "type", name }, { "description", description },
                { "subscribed", events::subscribed.contains(name) } });
        }
        return list;
    }, { .thread = Thread::Any });
    Register("Events.subscribe", [](const Json& params) {
        std::unique_lock lock(events::mutex);
        const auto types = events::TypesFrom(params);
        events::subscribed.insert(types.begin(), types.end());
        return Json{ { "subscribed", events::subscribed } };
    }, { .thread = Thread::Any });
    Register("Events.unsubscribe", [](const Json& params) {
        std::unique_lock lock(events::mutex);
        for (const auto& type : events::TypesFrom(params)) {
            events::subscribed.erase(type);
        }
        return Json{ { "subscribed", events::subscribed } };
    }, { .thread = Thread::Any });
}
