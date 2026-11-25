// SPDX-License-Identifier: GPL-3.0-or-later
// Game-independent types shared by the transport, the command bus and every module.
#pragma once

#include <nlohmann/json.hpp>

#include <initializer_list>
#include <stdexcept>
#include <string>
#include <string_view>

namespace devtools
{
    using Json = nlohmann::json;

    namespace code
    {
        constexpr int parse = -32700;
        constexpr int invalidRequest = -32600;
        constexpr int unknownMethod = -32601;
        constexpr int invalidParams = -32602;
        constexpr int internal = -32603;
        constexpr int notReady = -32004;
        constexpr int staleEpoch = -32005;
        constexpr int inputUnavailable = -32006;
        constexpr int consoleUnavailable = -32007;
        constexpr int timeout = -32008;
        constexpr int busy = -32009;
        constexpr int notFound = -32010;
    }

    struct RpcError : std::runtime_error
    {
        RpcError(int code, std::string message, Json data = nullptr) :
            std::runtime_error(message), code(code), message(std::move(message)), data(std::move(data))
        {}

        int code;
        std::string message;
        Json data;
    };

    inline void AllowOnly(const Json& params, std::initializer_list<std::string_view> keys)
    {
        for (const auto& [key, value] : params.items()) {
            bool known = false;
            for (const auto allowed : keys) {
                known = known || key == allowed;
            }
            if (!known) {
                throw RpcError(code::invalidParams, "Unexpected parameter '" + key + "'");
            }
        }
    }

    template <class T>
    T Require(const Json& params, const char* key)
    {
        const auto found = params.find(key);
        if (found == params.end()) {
            throw RpcError(code::invalidParams, std::string("Missing parameter '") + key + "'");
        }
        try {
            return found->get<T>();
        } catch (const Json::exception&) {
            throw RpcError(code::invalidParams, std::string("Parameter '") + key + "' has the wrong type");
        }
    }

    template <class T>
    T Optional(const Json& params, const char* key, T fallback)
    {
        const auto found = params.find(key);
        if (found == params.end() || found->is_null()) {
            return fallback;
        }
        return Require<T>(params, key);
    }
}
