// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
#include "bus.h"

#include "game.h"
#include "methods.h"
#include "perf.h"

#include <algorithm>
#include <condition_variable>
#include <map>
#include <mutex>
#include <shared_mutex>
#include <thread>
#include <unordered_map>
#include <vector>

namespace devtools
{
    namespace
    {
        struct Method
        {
            DeferredHandler handler;
            MethodSpec spec;
            MethodInfo info;
        };

        // Other plugins add and remove methods while connections are being served, so lookups copy a
        // shared_ptr under the lock and a call in flight keeps its method alive after it is removed.
        using MethodPtr = std::shared_ptr<const Method>;

        std::shared_mutex methodsMutex;

        std::unordered_map<std::string, MethodPtr>& Methods()
        {
            static std::unordered_map<std::string, MethodPtr> methods;
            return methods;
        }

        MethodPtr FindMethod(const std::string& name)
        {
            std::shared_lock lock(methodsMutex);
            const auto found = Methods().find(name);
            return found == Methods().end() ? nullptr : found->second;
        }

        std::vector<std::pair<const char*, void (*)()>>& Modules()
        {
            static std::vector<std::pair<const char*, void (*)()>> modules;
            return modules;
        }

        constexpr int maxInFlight = 64;
        std::atomic_int inFlight = 0;
        std::atomic<std::uint64_t> completed = 0;
        std::atomic<std::uint64_t> timedOut = 0;
        std::atomic<std::uint64_t> late = 0;

        // One thread owns every deadline so a slow game thread never holds a connection thread.
        class Timer
        {
        public:
            void Add(std::chrono::steady_clock::time_point deadline, std::weak_ptr<Reply> reply, Json data)
            {
                {
                    std::lock_guard lock(mutex_);
                    entries_.emplace(deadline, Entry{ std::move(reply), std::move(data) });
                    if (!thread_.joinable()) {
                        thread_ = std::thread([this] { Run(); });
                        thread_.detach();
                    }
                }
                wake_.notify_one();
            }

        private:
            struct Entry
            {
                std::weak_ptr<Reply> reply;
                Json data;
            };

            void Run()
            {
                std::unique_lock lock(mutex_);
                for (;;) {
                    if (entries_.empty()) {
                        wake_.wait(lock);
                        continue;
                    }
                    const auto next = entries_.begin()->first;
                    if (std::chrono::steady_clock::now() < next) {
                        wake_.wait_until(lock, next);
                        continue;
                    }
                    auto entry = std::move(entries_.begin()->second);
                    entries_.erase(entries_.begin());
                    lock.unlock();
                    if (const auto reply = entry.reply.lock()) {
                        if (reply->Fail(code::timeout, "Timed out waiting for the game", std::move(entry.data))) {
                            ++timedOut;
                        }
                    }
                    lock.lock();
                }
            }

            std::mutex mutex_;
            std::condition_variable wake_;
            std::multimap<std::chrono::steady_clock::time_point, Entry> entries_;
            std::thread thread_;
        };

        Timer& Deadlines()
        {
            static auto* timer = new Timer();  // never destroyed: its thread outlives static teardown
            return *timer;
        }

        Json ErrorEnvelope(const Json& id, int errorCode, const std::string& message)
        {
            return { { "jsonrpc", "2.0" }, { "id", id }, { "error", { { "code", errorCode }, { "message", message } } } };
        }

        const char* ThreadName(Thread thread)
        {
            switch (thread) {
            case Thread::Main:
                return "main";
            case Thread::UI:
                return "ui";
            default:
                return "any";
            }
        }

        void Run(const MethodPtr& method, const Json& params, const ReplyPtr& reply, const std::string& name, Thread thread,
            double enqueuedAt)
        {
            // A call that timed out before the game reached it is not run: its client has moved on.
            if (reply->Done()) {
                return;
            }
            const double started = perf::Now();
            struct Timing
            {
                const std::string& name;
                Thread thread;
                double enqueuedAt;
                double started;
                ~Timing()
                {
                    if (thread != Thread::Any) {
                        const double ended = perf::Now();
                        perf::NoteCall(name, thread == Thread::Main ? "main" : "ui", started, (started - enqueuedAt) * 1000.0,
                            (ended - started) * 1000.0);
                    }
                }
            } timing{ name, thread, enqueuedAt, started };
            try {
                method->handler(params, reply);
            } catch (const RpcError& error) {
                reply->Fail(error.code, error.message, error.data);
            } catch (const Json::exception& error) {
                reply->Fail(code::invalidParams, error.what());
            } catch (const std::exception& error) {
                reply->Fail(code::internal, error.what());
            } catch (...) {
                reply->Fail(code::internal, "Internal error");
            }
        }
    }

    bool Reply::Finish(Json body)
    {
        if (done_.exchange(true, std::memory_order_acq_rel)) {
            ++late;
            return false;
        }
        deliver_(std::move(body));
        return true;
    }

    void Register(std::string name, Handler handler, MethodSpec spec)
    {
        RegisterDeferred(std::move(name),
            [handler = std::move(handler)](const Json& params, ReplyPtr reply) { reply->Ok(handler(params)); },
            spec);
    }

    void RegisterDeferred(std::string name, DeferredHandler handler, MethodSpec spec, MethodInfo info)
    {
        auto method = std::make_shared<const Method>(Method{ std::move(handler), spec, std::move(info) });
        std::unique_lock lock(methodsMutex);
        Methods()[std::move(name)] = std::move(method);
    }

    bool Unregister(const std::string& name, const std::string& owner)
    {
        std::unique_lock lock(methodsMutex);
        const auto found = Methods().find(name);
        if (found == Methods().end() || found->second->info.owner != owner) {
            return false;
        }
        Methods().erase(found);
        return true;
    }

    std::optional<std::string> OwnerOf(const std::string& name)
    {
        const auto method = FindMethod(name);
        if (!method) {
            return std::nullopt;
        }
        return method->info.owner;
    }

    Json ListMethods()
    {
        auto list = Json::array();
        std::shared_lock lock(methodsMutex);
        for (const auto& [name, method] : Methods()) {
            Json entry{ { "name", name }, { "thread", ThreadName(method->spec.thread) },
                { "timeoutMs", method->spec.timeout.count() } };
            if (!method->info.owner.empty()) {
                entry["owner"] = method->info.owner;
                entry["description"] = method->info.description;
                if (!method->info.schema.is_null()) {
                    entry["schema"] = method->info.schema;
                }
            }
            list.push_back(std::move(entry));
        }
        std::sort(list.begin(), list.end(), [](const Json& a, const Json& b) { return a["name"] < b["name"]; });
        return list;
    }

    namespace
    {
        std::mutex capabilitiesMutex;
        std::map<std::string, bool> capabilities;
    }

    void SetCapability(std::string name, bool available)
    {
        std::lock_guard lock(capabilitiesMutex);
        capabilities[std::move(name)] = available;
    }

    Json Capabilities()
    {
        std::lock_guard lock(capabilitiesMutex);
        auto list = Json::array();
        for (const auto& [name, available] : capabilities) {
            if (available) {
                list.push_back(name);
            }
        }
        return list;
    }

    ModuleRegistrar::ModuleRegistrar(const char* name, void (*install)())
    {
        Modules().emplace_back(name, install);
    }

    void InstallModules()
    {
        for (const auto& [name, install] : Modules()) {
            install();
        }
    }

    namespace bus
    {
        Json Stats()
        {
            return { { "inFlight", inFlight.load() }, { "completed", completed.load() },
                { "timedOut", timedOut.load() }, { "lateCompletions", late.load() } };
        }

        void Handle(const std::string& text, const std::shared_ptr<Outbox>& outbox)
        {
            const auto request = Json::parse(text, nullptr, false);
            if (request.is_discarded()) {
                outbox->Push(ErrorEnvelope(nullptr, code::parse, "Invalid JSON").dump());
                return;
            }
            if (!request.is_object() || !request.contains("id") || !request["id"].is_number_unsigned() ||
                request.value("jsonrpc", "") != "2.0" || !request.contains("method") ||
                !request["method"].is_string()) {
                outbox->Push(ErrorEnvelope(nullptr, code::invalidRequest,
                                 "Expected a JSON-RPC 2.0 call with a numeric id").dump());
                return;
            }
            const auto id = request["id"];
            const auto name = request["method"].get<std::string>();
            auto params = request.value("params", Json::object());
            if (!params.is_object()) {
                outbox->Push(ErrorEnvelope(id, code::invalidParams, "Expected named parameters").dump());
                return;
            }
            const auto method = FindMethod(name);
            if (!method) {
                outbox->Push(ErrorEnvelope(id, code::unknownMethod, "Unknown method " + name).dump());
                return;
            }
            if (inFlight.fetch_add(1) >= maxInFlight) {
                --inFlight;
                outbox->Push(ErrorEnvelope(id, code::busy, "Too many calls in flight").dump());
                return;
            }
            auto reply = std::make_shared<Reply>([outbox, id](Json body) {
                body["jsonrpc"] = "2.0";
                body["id"] = id;
                --inFlight;
                ++completed;
                outbox->Push(body.dump(-1, ' ', false, Json::error_handler_t::replace));
            });
            if (method->spec.timeout.count() > 0) {
                Deadlines().Add(std::chrono::steady_clock::now() + method->spec.timeout, reply,
                    { { "method", name }, { "thread", ThreadName(method->spec.thread) },
                        { "timeoutMs", method->spec.timeout.count() } });
            }
            const auto tasks = SKSE::GetTaskInterface();
            const auto thread = method->spec.thread;
            const double enqueuedAt = perf::Now();
            switch (thread) {
            case Thread::Main:
                tasks->AddTask([method, params, reply, name, thread, enqueuedAt] { Run(method, params, reply, name, thread, enqueuedAt); });
                break;
            case Thread::UI:
                tasks->AddUITask([method, params, reply, name, thread, enqueuedAt] { Run(method, params, reply, name, thread, enqueuedAt); });
                break;
            default:
                Run(method, params, reply, name, thread, enqueuedAt);
                break;
            }
        }
    }
}
