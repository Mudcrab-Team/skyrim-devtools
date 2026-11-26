// SPDX-License-Identifier: GPL-3.0-or-later
// The method registry. A module adds methods from its DEVTOOLS_MODULE block:
//
//     DEVTOOLS_MODULE(Example)
//     {
//         devtools::Register("Example.echo", [](const devtools::Json& p) { return p; }, {.thread = devtools::Thread::Any});
//     }
//
// Handlers run on the thread named in the spec. A thrown RpcError becomes the JSON-RPC error.
// A deferred handler receives the Reply and may complete it later from any thread; once the
// timeout fires the client has its -32008 and a late Ok/Fail is dropped.
#pragma once

#include "core.h"

#include <atomic>
#include <chrono>
#include <functional>
#include <memory>

namespace devtools
{
    enum class Thread
    {
        Any,  // the connection thread; never touch game state here
        Main, // SKSE AddTask
        UI    // SKSE AddUITask
    };

    struct MethodSpec
    {
        Thread thread = Thread::UI;
        std::chrono::milliseconds timeout{ 5000 };  // 0 waits forever
    };

    class Reply
    {
    public:
        explicit Reply(std::function<void(Json)> deliver) : deliver_(std::move(deliver)) {}

        bool Ok(Json result) { return Finish({ { "result", std::move(result) } }); }
        bool Fail(int code, std::string message, Json data = nullptr)
        {
            Json error{ { "code", code }, { "message", std::move(message) } };
            if (!data.is_null()) {
                error["data"] = std::move(data);
            }
            return Finish({ { "error", std::move(error) } });
        }
        [[nodiscard]] bool Done() const { return done_.load(std::memory_order_acquire); }

    private:
        bool Finish(Json body);

        std::atomic_bool done_ = false;
        std::function<void(Json)> deliver_;
    };

    using ReplyPtr = std::shared_ptr<Reply>;
    using Handler = std::function<Json(const Json& params)>;
    using DeferredHandler = std::function<void(const Json& params, ReplyPtr reply)>;

    void Register(std::string name, Handler handler, MethodSpec spec = {});
    void RegisterDeferred(std::string name, DeferredHandler handler, MethodSpec spec = {});
    [[nodiscard]] Json ListMethods();

    // Features a client can rely on, reported by Session.getInfo. A module marks its feature
    // unavailable when, for example, a hook could not be installed.
    void SetCapability(std::string name, bool available);
    [[nodiscard]] Json Capabilities();

    struct ModuleRegistrar
    {
        ModuleRegistrar(const char* name, void (*install)());
    };

    // Runs every module's install block once SKSE is initialised.
    // An empty allow-list installs every module; otherwise only the named ones, so a leak or a hook can be
    // bisected by module without rebuilding.
    void InstallModules(const std::vector<std::string>& only = {});
}

#define DEVTOOLS_MODULE(id)                                                           \
    static void devtools_install_##id();                                              \
    static const ::devtools::ModuleRegistrar devtools_registrar_##id{ #id, &devtools_install_##id }; \
    static void devtools_install_##id()
