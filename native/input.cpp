// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Synthetic input inside the game's own pipeline. Events are added to BSInputEventQueue at the
// start of BSInputDeviceManager::PollInputDevices, before control mapping and dispatch, so
// ControlMap assigns user events and every menu and handler sees them exactly like hardware
// input. Nothing goes through Windows, so the window needs neither focus nor the foreground.
#include "game.h"
#include "methods.h"

#include <MinHook.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <map>
#include <mutex>
#include <numbers>
#include <optional>
#include <unordered_map>
#include <vector>

namespace
{
    using namespace devtools;
    using namespace std::chrono_literals;
    using Clock = std::chrono::steady_clock;

    enum class Phase
    {
        Down,
        Up,
        Tap
    };

    struct Button
    {
        RE::INPUT_DEVICE device = RE::INPUT_DEVICE::kKeyboard;
        std::uint32_t id = 0;
        auto operator<=>(const Button&) const = default;
    };

    struct Request
    {
        std::optional<Button> button;
        std::string control;  // resolved to a button on the game thread
        Phase phase = Phase::Tap;
        std::chrono::milliseconds hold{ 0 };
        std::int32_t dx = 0;
        std::int32_t dy = 0;
        bool move = false;
        ReplyPtr reply;
    };

    struct Held
    {
        Clock::time_point pressed;
        std::optional<Clock::time_point> release;
        std::uint64_t downPoll = 0;
        ReplyPtr reply;  // a tap answers once its release has been dispatched
        Json result;
    };

    using Poll = void (*)(RE::BSInputDeviceManager*, float);
    Poll originalPoll = nullptr;
    bool hooked = false;

    std::mutex inboxMutex;
    std::vector<Request> inbox;
    std::atomic_bool releaseAll = false;

    // Owned by the polling thread.
    std::vector<Request> pending;
    std::map<Button, Held> held;
    std::atomic<std::uint64_t> polls = 0;
    std::atomic<std::uint64_t> injectedButtons = 0;
    std::atomic<std::uint64_t> injectedMoves = 0;
    std::atomic<std::int64_t> lastPollMs = 0;

    std::int64_t NowMs()
    {
        return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
    }

    float Seconds(Clock::duration duration)
    {
        return std::max(0.001F, std::chrono::duration<float>(duration).count());
    }

    const std::unordered_map<std::string, std::uint32_t>& KeyNames()
    {
        // DirectInput scancodes; names are matched without case.
        static const std::unordered_map<std::string, std::uint32_t> names = [] {
            std::unordered_map<std::string, std::uint32_t> map{ { "escape", 1 }, { "esc", 1 }, { "minus", 12 },
                { "equals", 13 }, { "backspace", 14 }, { "tab", 15 }, { "lbracket", 26 }, { "rbracket", 27 },
                { "enter", 28 }, { "return", 28 }, { "lctrl", 29 }, { "ctrl", 29 }, { "semicolon", 39 },
                { "apostrophe", 40 }, { "grave", 41 }, { "tilde", 41 }, { "lshift", 42 }, { "shift", 42 },
                { "backslash", 43 }, { "comma", 51 }, { "period", 52 }, { "slash", 53 }, { "rshift", 54 },
                { "numpadmultiply", 55 }, { "lalt", 56 }, { "alt", 56 }, { "space", 57 }, { "capslock", 58 },
                { "numlock", 69 }, { "scrolllock", 70 }, { "numpad7", 71 }, { "numpad8", 72 }, { "numpad9", 73 },
                { "numpadminus", 74 }, { "numpad4", 75 }, { "numpad5", 76 }, { "numpad6", 77 },
                { "numpadplus", 78 }, { "numpad1", 79 }, { "numpad2", 80 }, { "numpad3", 81 }, { "numpad0", 82 },
                { "numpadperiod", 83 }, { "f11", 87 }, { "f12", 88 }, { "numpadenter", 156 }, { "rctrl", 157 },
                { "numpaddivide", 181 }, { "printscreen", 183 }, { "ralt", 184 }, { "pause", 197 },
                { "home", 199 }, { "up", 200 }, { "pageup", 201 }, { "left", 203 }, { "right", 205 },
                { "end", 207 }, { "down", 208 }, { "pagedown", 209 }, { "insert", 210 }, { "delete", 211 } };
            const char* rows[] = { "1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm" };
            const std::uint32_t starts[] = { 2, 16, 30, 44 };
            for (int row = 0; row < 4; ++row) {
                for (std::uint32_t index = 0; rows[row][index]; ++index) {
                    map.emplace(std::string(1, rows[row][index]), starts[row] + index);
                }
            }
            for (std::uint32_t index = 0; index < 10; ++index) {
                map.emplace("f" + std::to_string(index + 1), 59 + index);
            }
            return map;
        }();
        return names;
    }

    Button ParseKey(const Json& key)
    {
        if (key.is_number_unsigned() && key.get<std::uint64_t>() >= 1 && key.get<std::uint64_t>() <= 255) {
            return { RE::INPUT_DEVICE::kKeyboard, key.get<std::uint32_t>() };
        }
        if (key.is_string()) {
            auto name = key.get<std::string>();
            std::ranges::transform(name, name.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            if (const auto found = KeyNames().find(name); found != KeyNames().end()) {
                return { RE::INPUT_DEVICE::kKeyboard, found->second };
            }
        }
        throw RpcError(code::invalidParams, "key is a DirectInput scancode (1-255) or a key name such as Tab");
    }

    Button ParseMouseButton(const Json& button)
    {
        if (button.is_number_unsigned() && button.get<std::uint64_t>() <= 7) {
            return { RE::INPUT_DEVICE::kMouse, button.get<std::uint32_t>() };
        }
        static const std::unordered_map<std::string, std::uint32_t> names{ { "left", 0 }, { "right", 1 },
            { "middle", 2 } };
        if (button.is_string()) {
            if (const auto found = names.find(button.get<std::string>()); found != names.end()) {
                return { RE::INPUT_DEVICE::kMouse, found->second };
            }
        }
        throw RpcError(code::invalidParams, "button is left, right, middle or 0-7");
    }

    Phase ParsePhase(const Json& params)
    {
        const auto phase = Optional<std::string>(params, "phase", "tap");
        if (phase == "down") {
            return Phase::Down;
        }
        if (phase == "up") {
            return Phase::Up;
        }
        if (phase == "tap") {
            return Phase::Tap;
        }
        throw RpcError(code::invalidParams, "phase is down, up or tap");
    }

    std::chrono::milliseconds ParseHold(const Json& params)
    {
        const auto hold = Optional<std::int64_t>(params, "holdMs", 60);
        if (hold < 0 || hold > 10000) {
            throw RpcError(code::invalidParams, "holdMs is between 0 and 10000");
        }
        return std::chrono::milliseconds(hold);
    }

    void Submit(Request request)
    {
        if (!hooked) {
            throw RpcError(code::inputUnavailable, "The input polling hook is not installed");
        }
        std::lock_guard lock(inboxMutex);
        if (inbox.size() >= 256) {
            throw RpcError(code::busy, "Too many input requests are queued");
        }
        inbox.push_back(std::move(request));
    }

    // Resolves a ControlMap user event, preferring the innermost active input context so that,
    // for example, a menu's own binding wins over the gameplay binding.
    std::optional<Button> ResolveControl(const std::string& name)
    {
        const auto map = RE::ControlMap::GetSingleton();
        if (!map) {
            return std::nullopt;
        }
        std::vector<RE::ControlMap::InputContextID> contexts;
        const auto& stack = map->GetRuntimeData().contextPriorityStack;
        for (auto index = stack.size(); index > 0; --index) {
            contexts.push_back(stack[index - 1]);
        }
        contexts.push_back(RE::ControlMap::InputContextID::kGameplay);
        for (const auto context : contexts) {
            for (const auto device : { RE::INPUT_DEVICE::kKeyboard, RE::INPUT_DEVICE::kMouse }) {
                const auto key = map->GetMappedKey(name, device, context);
                if (key != static_cast<std::uint32_t>(RE::ControlMap::kInvalid) && key != 0xFFFFFFFF) {
                    return Button{ device, key };
                }
            }
        }
        return std::nullopt;
    }

    Json Describe(const Button& button)
    {
        return { { "device", button.device == RE::INPUT_DEVICE::kMouse ? "mouse" : "keyboard" },
            { "id", button.id } };
    }

    void PollHook(RE::BSInputDeviceManager* manager, float seconds)
    {
        const auto poll = ++polls;
        lastPollMs = NowMs();
        {
            std::lock_guard lock(inboxMutex);
            std::ranges::move(inbox, std::back_inserter(pending));
            inbox.clear();
        }
        auto* queue = RE::BSInputEventQueue::GetSingleton();
        std::vector<std::pair<ReplyPtr, Json>> answered;
        if (!queue) {
            originalPoll(manager, seconds);
            return;
        }
        const auto now = Clock::now();
        auto room = [&] { return queue->buttonEventCount < RE::BSInputEventQueue::MAX_BUTTON_EVENTS; };
        auto press = [&](const Button& button, float value, float duration) {
            queue->AddButtonEvent(button.device, static_cast<std::int32_t>(button.id), value, duration);
            ++injectedButtons;
        };

        if (releaseAll.exchange(false)) {
            for (auto& [button, state] : held) {
                if (room()) {
                    press(button, 0.0F, Seconds(now - state.pressed));
                }
                if (state.reply) {
                    answered.emplace_back(state.reply, state.result);
                }
            }
            held.clear();
        }

        // Keys already down: release the ones that are due, repeat the rest as held.
        for (auto it = held.begin(); it != held.end();) {
            auto& [button, state] = *it;
            if (state.downPoll == poll || !room()) {
                ++it;
                continue;
            }
            if (state.release && now >= *state.release) {
                press(button, 0.0F, Seconds(now - state.pressed));
                if (state.reply) {
                    answered.emplace_back(state.reply, state.result);
                }
                it = held.erase(it);
                continue;
            }
            press(button, 1.0F, Seconds(now - state.pressed));
            ++it;
        }

        std::vector<Request> later;
        for (auto& request : pending) {
            if (request.reply->Done()) {
                continue;
            }
            if (request.move) {
                if (queue->mouseEventCount >= RE::BSInputEventQueue::MAX_MOUSE_EVENTS) {
                    later.push_back(std::move(request));
                    continue;
                }
                queue->AddMouseMoveEvent(request.dx, request.dy);
                ++injectedMoves;
                answered.emplace_back(request.reply, Json{ { "dx", request.dx }, { "dy", request.dy } });
                continue;
            }
            if (!request.button) {
                request.button = ResolveControl(request.control);
                if (!request.button) {
                    request.reply->Fail(code::notFound, "No key or mouse button is mapped to " + request.control);
                    continue;
                }
            }
            const auto button = *request.button;
            Json result = Describe(button);
            if (!request.control.empty()) {
                result["control"] = request.control;
            }
            const auto existing = held.find(button);
            if (request.phase == Phase::Up) {
                if (existing == held.end()) {
                    result["wasDown"] = false;
                    answered.emplace_back(request.reply, result);
                } else if (room()) {
                    press(button, 0.0F, Seconds(now - existing->second.pressed));
                    if (existing->second.reply) {
                        answered.emplace_back(existing->second.reply, existing->second.result);
                    }
                    held.erase(existing);
                    result["wasDown"] = true;
                    answered.emplace_back(request.reply, result);
                } else {
                    later.push_back(std::move(request));
                }
                continue;
            }
            if (existing != held.end()) {
                // A second press of a held key is answered without a duplicate down event.
                if (request.phase == Phase::Tap) {
                    existing->second.release = now + request.hold;
                }
                result["alreadyDown"] = true;
                answered.emplace_back(request.reply, result);
                continue;
            }
            if (!room()) {
                later.push_back(std::move(request));
                continue;
            }
            press(button, 1.0F, 0.0F);
            Held state{ now, std::nullopt, poll, nullptr, result };
            if (request.phase == Phase::Tap) {
                state.release = now + request.hold;
                state.reply = request.reply;
            } else {
                answered.emplace_back(request.reply, result);
            }
            held.emplace(button, std::move(state));
        }
        pending = std::move(later);

        originalPoll(manager, seconds);

        // Answer only after the poll dispatched the events, so a caller can rely on them having
        // reached the game's input handlers.
        for (auto& [reply, result] : answered) {
            result["poll"] = poll;
            reply->Ok(std::move(result));
        }
    }

    bool InstallHook()
    {
        REL::Relocation<std::uintptr_t> target{ RELOCATION_ID(67315, 68617) };
        auto* address = reinterpret_cast<void*>(target.address());
        return MH_CreateHook(address, reinterpret_cast<void*>(&PollHook), reinterpret_cast<void**>(&originalPoll)) ==
                   MH_OK &&
               MH_EnableHook(address) == MH_OK;
    }

    void SubmitButton(const Json& params, ReplyPtr reply, std::optional<Button> button, std::string control)
    {
        Request request;
        request.button = button;
        request.control = std::move(control);
        request.phase = ParsePhase(params);
        request.hold = ParseHold(params);
        request.reply = std::move(reply);
        Submit(std::move(request));
    }

    float Radians(double degrees) { return static_cast<float>(degrees * std::numbers::pi / 180.0); }
}

namespace devtools
{
    bool TapControl(const std::string& control)
    {
        try {
            Request request;
            request.control = control;
            request.phase = Phase::Tap;
            request.hold = 60ms;
            request.reply = std::make_shared<Reply>([](Json) {});
            Submit(std::move(request));
            return true;
        } catch (const RpcError&) {
            return false;
        }
    }
}

DEVTOOLS_MODULE(Input)
{
    hooked = InstallHook();
    SetCapability("input", hooked);
    const MethodSpec spec{ .thread = Thread::Any, .timeout = 15s };

    RegisterDeferred("Input.key", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "key", "phase", "holdMs" });
        SubmitButton(params, std::move(reply), ParseKey(params.at("key")), {});
    }, spec);
    RegisterDeferred("Input.control", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "name", "phase", "holdMs" });
        const auto name = Require<std::string>(params, "name");
        if (name.empty()) {
            throw RpcError(code::invalidParams, "name is a ControlMap user event such as Forward");
        }
        SubmitButton(params, std::move(reply), std::nullopt, name);
    }, spec);
    RegisterDeferred("Input.mouse", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "dx", "dy", "button", "phase", "holdMs" });
        if (params.contains("button")) {
            if (params.contains("dx") || params.contains("dy")) {
                throw RpcError(code::invalidParams, "Send a move and a button press as separate calls");
            }
            SubmitButton(params, std::move(reply), ParseMouseButton(params["button"]), {});
            return;
        }
        Request request;
        request.move = true;
        request.dx = Optional<std::int32_t>(params, "dx", 0);
        request.dy = Optional<std::int32_t>(params, "dy", 0);
        request.reply = std::move(reply);
        Submit(std::move(request));
    }, spec);
    Register("Input.releaseAll", [](const Json& params) {
        AllowOnly(params, {});
        releaseAll = true;
        return Json{ { "accepted", true } };
    }, { .thread = Thread::Any });
    Register("Input.stats", [](const Json& params) {
        AllowOnly(params, {});
        const auto last = lastPollMs.load();
        return Json{ { "hooked", hooked }, { "polls", polls.load() },
            { "msSinceLastPoll", last ? Json(NowMs() - last) : Json(nullptr) },
            { "injectedButtons", injectedButtons.load() }, { "injectedMoves", injectedMoves.load() } };
    }, { .thread = Thread::Any });

    // Turns the player directly; Input.mouse moves the camera through the normal look controls.
    Register("Player.look", [](const Json& params) {
        AllowOnly(params, { "yawDeg", "pitchDeg", "loadEpoch" });
        RequirePlayable(params);
        const auto player = RE::PlayerCharacter::GetSingleton();
        const auto yaw = Optional<double>(params, "yawDeg", 0.0);
        const auto pitch = Optional<double>(params, "pitchDeg", 0.0);
        player->SetHeading(player->GetAngleZ() + Radians(yaw));
        const auto limit = Radians(85.0);
        player->SetLooking(std::clamp(player->GetAngleX() + Radians(pitch), -limit, limit));
        return Json{ { "headingDeg", player->GetAngleZ() * 180.0 / std::numbers::pi },
            { "pitchDeg", player->GetAngleX() * 180.0 / std::numbers::pi } };
    }, { .thread = Thread::Main });
}
