// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Screen.capture reads the game's own back buffer on the render thread just before Present, so
// it works with the window off-screen or unfocused and never touches the desktop. Scaling and
// encoding run on a short-lived worker thread.
#include "game.h"
#include "methods.h"
#include "screen.h"
#include "screen_gfx.h"

#include <algorithm>
#include <array>
#include <deque>
#include <fstream>
#include <mutex>
#include <thread>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;
    using Clock = std::chrono::steady_clock;

    constexpr std::size_t maxQueued = 4;
    constexpr std::size_t maxReplyBytes = 12 * 1024 * 1024;
    // Frames to let pass after hiding the menus so Scaleform has redrawn without them.
    constexpr int hiddenUiFrames = 4;

    struct Request
    {
        ReplyPtr reply;
        Clock::time_point began;
        std::uint32_t maxWidth;
        bool jpeg;
        int quality;
        std::filesystem::path path;  // empty: return the bytes inline
        int skipFrames;
        bool restoreUi;
    };

    std::mutex queueMutex;
    std::deque<Request> queue;
    std::atomic_bool pending = false;

    std::filesystem::path RunDir()
    {
        std::array<wchar_t, 4096> buffer{};
        const auto size = GetEnvironmentVariableW(L"SKYRIM_DEVTOOLS_RUN_DIR", buffer.data(), static_cast<DWORD>(buffer.size()));
        return size && size < buffer.size() ? std::filesystem::path(std::wstring(buffer.data(), size)) : std::filesystem::path{};
    }

    double Ms(Clock::duration d) { return std::chrono::duration<double, std::milli>(d).count(); }

    void RestoreUi()
    {
        SKSE::GetTaskInterface()->AddUITask([] {
            if (const auto ui = RE::UI::GetSingleton()) {
                ui->ShowMenus(true);
            }
        });
    }

    void Finish(Request request, gfx::Frame frame, std::uint64_t number, double readbackMs)
    {
        try {
            const auto encodeStart = Clock::now();
            auto image = gfx::Encode(frame, request.maxWidth, request.jpeg, request.quality);
            const double encodeMs = Ms(Clock::now() - encodeStart);
            Json result{ { "frame", number }, { "width", image.width }, { "height", image.height },
                { "sourceWidth", frame.width }, { "sourceHeight", frame.height },
                { "format", request.jpeg ? "jpeg" : "png" }, { "bytes", image.bytes.size() },
                { "readbackMs", readbackMs }, { "encodeMs", encodeMs } };
            if (request.path.empty()) {
                auto data = gfx::Base64(image.bytes);
                if (data.size() > maxReplyBytes) {
                    request.reply->Fail(code::internal, "The encoded image is too large; lower maxWidth or use jpeg");
                    return;
                }
                result["data"] = std::move(data);
            } else {
                std::filesystem::create_directories(request.path.parent_path());
                std::ofstream file(request.path, std::ios::binary);
                file.write(reinterpret_cast<const char*>(image.bytes.data()), static_cast<std::streamsize>(image.bytes.size()));
                if (!file.good()) {
                    request.reply->Fail(code::internal, "Could not write the image file");
                    return;
                }
                result["path"] = request.path.filename().string();
            }
            result["totalMs"] = Ms(Clock::now() - request.began);
            request.reply->Ok(std::move(result));
        } catch (const std::exception& error) {
            request.reply->Fail(code::internal, error.what());
        }
    }

    std::filesystem::path TargetPath(const std::string& name, bool jpeg)
    {
        const auto extension = jpeg ? ".jpg" : ".png";
        const bool plain = !name.empty() && name.size() <= 96 &&
                           std::ranges::all_of(name, [](unsigned char c) { return std::isalnum(c) || c == '_' || c == '-' || c == '.'; }) &&
                           name.front() != '.' && name.find("..") == std::string::npos;
        if (!plain) {
            throw RpcError(code::invalidParams, "path is a plain file name such as shot1.png, saved under the run's screens directory");
        }
        const auto runDir = RunDir();
        if (runDir.empty()) {
            throw RpcError(code::internal, "The run directory is unknown");
        }
        std::filesystem::path file = name;
        if (file.extension() != extension) {
            file += extension;
        }
        return runDir / "screens" / file;
    }
}

namespace devtools::screen
{
    void BeforePresent(void* swapChain, std::uint64_t frame)
    {
        if (!pending.load(std::memory_order_relaxed)) {
            return;
        }
        std::vector<Request> ready;
        {
            std::lock_guard lock(queueMutex);
            for (auto it = queue.begin(); it != queue.end();) {
                if (it->skipFrames > 0) {
                    --it->skipFrames;
                    ++it;
                } else {
                    ready.push_back(std::move(*it));
                    it = queue.erase(it);
                }
            }
            pending.store(!queue.empty(), std::memory_order_relaxed);
        }
        for (auto& request : ready) {
            if (request.reply->Done()) {
                if (request.restoreUi) {
                    RestoreUi();
                }
                continue;
            }
            const auto start = Clock::now();
            gfx::Frame image;
            try {
                image = gfx::ReadBackbuffer(swapChain);
            } catch (const std::exception& error) {
                request.reply->Fail(code::internal, error.what());
                if (request.restoreUi) {
                    RestoreUi();
                }
                continue;
            }
            const double readbackMs = Ms(Clock::now() - start);
            if (request.restoreUi) {
                RestoreUi();
            }
            std::thread([request = std::move(request), image = std::move(image), frame, readbackMs]() mutable {
                Finish(std::move(request), std::move(image), frame, readbackMs);
            }).detach();
        }
    }
}

DEVTOOLS_MODULE(Screen)
{
    RegisterDeferred("Screen.capture", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "maxWidth", "format", "quality", "includeUI", "path" });
        if (!perf::PresentHooked()) {
            throw RpcError(code::notReady, "The Present hook is not installed");
        }
        const auto maxWidth = Optional<std::uint32_t>(params, "maxWidth", 1024);
        if (maxWidth < 16 || maxWidth > 16384) {
            throw RpcError(code::invalidParams, "maxWidth is 16 to 16384");
        }
        auto format = Optional<std::string>(params, "format", "png");
        std::ranges::transform(format, format.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        if (format != "png" && format != "jpeg" && format != "jpg") {
            throw RpcError(code::invalidParams, "format is png or jpeg");
        }
        const bool jpeg = format != "png";
        const auto quality = Optional<int>(params, "quality", 85);
        if (quality < 1 || quality > 100) {
            throw RpcError(code::invalidParams, "quality is 1 to 100");
        }
        const bool includeUi = Optional<bool>(params, "includeUI", true);
        std::filesystem::path path;
        if (params.contains("path") && !params["path"].is_null()) {
            path = TargetPath(Require<std::string>(params, "path"), jpeg);
        }

        bool restoreUi = false;
        if (!includeUi) {
            if (const auto ui = RE::UI::GetSingleton(); ui && ui->IsShowingMenus()) {
                restoreUi = true;
                SKSE::GetTaskInterface()->AddUITask([] {
                    if (const auto ui = RE::UI::GetSingleton()) {
                        ui->ShowMenus(false);
                    }
                });
            }
        }
        {
            std::lock_guard lock(queueMutex);
            if (queue.size() >= maxQueued) {
                if (restoreUi) {
                    RestoreUi();
                }
                throw RpcError(code::busy, "Too many captures are waiting for a frame");
            }
            queue.push_back({ std::move(reply), Clock::now(), maxWidth, jpeg, quality, std::move(path),
                restoreUi ? hiddenUiFrames : 0, restoreUi });
            pending.store(true, std::memory_order_relaxed);
        }
    }, { .thread = Thread::Any, .timeout = 15s });
}
