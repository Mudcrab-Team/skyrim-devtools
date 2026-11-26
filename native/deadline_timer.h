// SPDX-License-Identifier: GPL-3.0-or-later
// One joinable worker thread that fires callbacks at their deadlines. The thread starts on the first
// Add and is the only one ever created; Stop() joins it, so nothing runs plugin code afterwards.
#pragma once

#include <chrono>
#include <condition_variable>
#include <functional>
#include <map>
#include <mutex>
#include <thread>

namespace devtools
{
    class DeadlineTimer
    {
    public:
        using Clock = std::chrono::steady_clock;

        DeadlineTimer() = default;
        DeadlineTimer(const DeadlineTimer&) = delete;
        DeadlineTimer& operator=(const DeadlineTimer&) = delete;
        ~DeadlineTimer() { Stop(); }

        // Returns false once stopped; the callback is then dropped.
        bool Add(Clock::time_point deadline, std::function<void()> fire)
        {
            {
                std::lock_guard lock(mutex_);
                if (stopping_) {
                    return false;
                }
                entries_.emplace(deadline, std::move(fire));
                if (!thread_.joinable()) {
                    thread_ = std::thread([this] { Run(); });
                }
            }
            wake_.notify_one();
            return true;
        }

        // Pending callbacks are discarded. Must not be called from inside a callback.
        void Stop()
        {
            std::thread worker;
            {
                std::lock_guard lock(mutex_);
                stopping_ = true;
                entries_.clear();
                worker = std::move(thread_);
            }
            wake_.notify_all();
            if (worker.joinable()) {
                worker.join();
            }
        }

    private:
        void Run()
        {
            std::unique_lock lock(mutex_);
            while (!stopping_) {
                if (entries_.empty()) {
                    wake_.wait(lock);
                    continue;
                }
                const auto next = entries_.begin()->first;
                if (Clock::now() < next) {
                    wake_.wait_until(lock, next);
                    continue;
                }
                auto fire = std::move(entries_.begin()->second);
                entries_.erase(entries_.begin());
                lock.unlock();
                fire();
                lock.lock();
            }
        }

        std::mutex mutex_;
        std::condition_variable wake_;
        std::multimap<Clock::time_point, std::function<void()>> entries_;
        std::thread thread_;
        bool stopping_ = false;
    };
}
