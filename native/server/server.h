// SPDX-License-Identifier: GPL-3.0-or-later
// Loopback WebSocket transport. It knows nothing about methods or the game.
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <string>

namespace devtools
{
    // Messages for one controller connection. Any thread may push; the connection's writer
    // thread drains it, so game threads never block on the socket.
    class Outbox
    {
    public:
        // Notifications are droppable so a flood of events cannot starve replies of memory.
        bool Push(std::string message, bool droppable = false);
        void RequestClose();
        void Shutdown();

        bool Pop(std::string& message, bool& droppable, std::chrono::milliseconds wait);
        [[nodiscard]] bool IsShutdown() const;
        [[nodiscard]] bool CloseRequested() const;
        [[nodiscard]] bool Drained() const;
        [[nodiscard]] std::uint64_t Dropped() const;

    private:
        static constexpr std::size_t droppableLimit = 4096;

        mutable std::mutex mutex_;
        std::condition_variable ready_;
        std::deque<std::pair<std::string, bool>> queue_;
        std::size_t droppableQueued_ = 0;
        std::uint64_t dropped_ = 0;
        bool closeRequested_ = false;
        bool shutdown_ = false;
        bool writing_ = false;
    };

    struct ServerOptions
    {
        int port = 0;
        std::string token;
        std::filesystem::path transcript;
        std::chrono::seconds idleTimeout{ 0 };     // 0: a silent controller is never dropped
        std::chrono::seconds connectTimeout{ 0 };  // 0: wait for the first controller forever
    };

    struct ServerHooks
    {
        std::function<void(std::shared_ptr<Outbox>)> connected;
        std::function<void(const std::string&, const std::shared_ptr<Outbox>&)> message;
        std::function<void()> disconnected;
        // Ends the game session; called once the controller leaves or never arrives.
        std::function<void()> closeSession;
    };

    bool StartServer(ServerOptions options, ServerHooks hooks);
}
