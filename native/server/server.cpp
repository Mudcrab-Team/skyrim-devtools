// SPDX-License-Identifier: GPL-3.0-or-later
#include "server/server.h"

#include <atomic>
#include <fstream>
#include <mutex>
#include <nlohmann/json.hpp>
#include <thread>
#include <httplib.h>

namespace devtools
{
    bool Outbox::Push(std::string message, bool droppable)
    {
        {
            std::lock_guard lock(mutex_);
            if (shutdown_) {
                return false;
            }
            if (droppable) {
                if (droppableQueued_ >= droppableLimit) {
                    ++dropped_;
                    return false;
                }
                ++droppableQueued_;
            }
            queue_.emplace_back(std::move(message), droppable);
        }
        ready_.notify_one();
        return true;
    }

    void Outbox::RequestClose()
    {
        std::lock_guard lock(mutex_);
        closeRequested_ = true;
    }

    void Outbox::Shutdown()
    {
        {
            std::lock_guard lock(mutex_);
            shutdown_ = true;
        }
        ready_.notify_all();
    }

    bool Outbox::Pop(std::string& message, bool& droppable, std::chrono::milliseconds wait)
    {
        std::unique_lock lock(mutex_);
        writing_ = false;
        if (!ready_.wait_for(lock, wait, [this] { return shutdown_ || !queue_.empty(); }) || queue_.empty()) {
            return false;
        }
        message = std::move(queue_.front().first);
        droppable = queue_.front().second;
        queue_.pop_front();
        if (droppable) {
            --droppableQueued_;
        }
        writing_ = true;
        return true;
    }

    bool Outbox::IsShutdown() const
    {
        std::lock_guard lock(mutex_);
        return shutdown_;
    }

    bool Outbox::CloseRequested() const
    {
        std::lock_guard lock(mutex_);
        return closeRequested_;
    }

    bool Outbox::Drained() const
    {
        std::lock_guard lock(mutex_);
        return queue_.empty() && !writing_;
    }

    std::uint64_t Outbox::Dropped() const
    {
        std::lock_guard lock(mutex_);
        return dropped_;
    }

    namespace
    {
        bool TokenMatches(const std::string& presented, const std::string& expected)
        {
            if (presented.size() != expected.size()) {
                return false;
            }
            unsigned int difference = 0;
            for (std::size_t index = 0; index < expected.size(); ++index) {
                difference |= static_cast<unsigned char>(presented[index] ^ expected[index]);
            }
            return difference == 0;
        }

        class Transcript
        {
        public:
            explicit Transcript(const std::filesystem::path& path) : file_(path, std::ios::binary) {}
            explicit operator bool() const { return static_cast<bool>(file_); }

            void Write(const char* direction, const std::string& message)
            {
                std::lock_guard lock(mutex_);
                if (bytes_ >= limit) {
                    return;
                }
                const auto row = nlohmann::json{ { "dir", direction }, { "message", message } }.dump(
                    -1, ' ', false, nlohmann::json::error_handler_t::replace);
                file_ << row << '\n' << std::flush;
                bytes_ += row.size() + 1;
            }

        private:
            static constexpr std::size_t limit = 4 * 1024 * 1024;
            std::mutex mutex_;
            std::ofstream file_;
            std::size_t bytes_ = 0;
        };
    }

    bool StartServer(ServerOptions options, ServerHooks hooks)
    {
        auto transcript = std::make_shared<Transcript>(options.transcript);
        if (!*transcript) {
            return false;
        }
        auto server = std::make_unique<httplib::Server>();
        auto leased = std::make_shared<std::atomic_bool>(false);
        server->new_task_queue = [] { return new httplib::ThreadPool(2, 2, 4); };
        server->set_read_timeout(3, 0);
        // A large reply to a slow reader should not abort the connection.
        server->set_write_timeout(30, 0);
        server->set_pre_routing_handler(
            [expected = "Bearer " + options.token, leased](const auto& request, auto& response) {
                if (request.path != "/rpc" || !request.has_header("Upgrade")) {
                    response.status = 404;
                } else if (request.has_header("Origin") ||
                           request.get_header_value_count("Authorization") != 1 ||
                           !TokenMatches(request.get_header_value("Authorization"), expected)) {
                    response.status = 403;
                } else if (*leased) {
                    response.status = 409;
                } else {
                    return httplib::Server::HandlerResponse::Unhandled;
                }
                return httplib::Server::HandlerResponse::Handled;
            });
        server->WebSocket(
            "/rpc", [leased, transcript, hooks, idle = options.idleTimeout](
                        const httplib::Request&, httplib::ws::WebSocket& socket) {
                if (leased->exchange(true)) {
                    socket.close(httplib::ws::CloseStatus::PolicyViolation);
                    return;
                }
                auto outbox = std::make_shared<Outbox>();
                std::atomic_bool writerFailed = false;
                std::thread writer([&socket, outbox, transcript, &writerFailed] {
                    std::string message;
                    bool droppable = false;
                    for (;;) {
                        if (!outbox->Pop(message, droppable, std::chrono::milliseconds(200))) {
                            if (outbox->IsShutdown() || !socket.is_open()) {
                                return;
                            }
                            continue;
                        }
                        if (!droppable) {
                            transcript->Write("out", message);
                        }
                        if (!socket.send(message)) {
                            writerFailed = true;
                            return;
                        }
                    }
                });
                hooks.connected(outbox);

                // The read timeout is a short poll; the idle limit is tracked separately so a
                // controller that sends nothing for a long time is only dropped when configured.
                socket.set_read_timeout(1);
                auto lastActivity = std::chrono::steady_clock::now();
                std::string message;
                for (;;) {
                    const auto result = socket.read(message);
                    if (result == httplib::ws::ReadResult::Text) {
                        lastActivity = std::chrono::steady_clock::now();
                        transcript->Write("in", message);
                        hooks.message(message, outbox);
                    } else if (result != httplib::ws::ReadResult::Timeout &&
                               result != httplib::ws::ReadResult::Binary) {
                        break;
                    }
                    if (writerFailed) {
                        break;
                    }
                    if (outbox->CloseRequested() && outbox->Drained()) {
                        break;
                    }
                    if (idle.count() > 0 && std::chrono::steady_clock::now() - lastActivity > idle) {
                        break;
                    }
                }
                hooks.disconnected();
                outbox->Shutdown();
                writer.join();
                if (socket.is_open()) {
                    socket.close();
                }
                hooks.closeSession();
            });

        if (!server->bind_to_port("127.0.0.1", options.port)) {
            return false;
        }
        if (options.connectTimeout.count() > 0) {
            std::thread([leased, close = hooks.closeSession, wait = options.connectTimeout] {
                std::this_thread::sleep_for(wait);
                if (!leased->exchange(true)) {
                    close();
                }
            }).detach();
        }
        // The listener lives until the game exits, avoiding thread joins during DLL detach.
        std::thread([listener = server.get()] { listener->listen_after_bind(); }).detach();
        server.release();
        return true;
    }
}
