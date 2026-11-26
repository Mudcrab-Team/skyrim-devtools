// SPDX-License-Identifier: GPL-3.0-or-later
// GPU timing for the profiler: D3D11 timestamp queries bracket each frame's GPU work (from the end of one
// Present to the next) and mark every render-target and depth clear, which is where the renderer starts a
// pass. Results are read at least two frames late with DONOTFLUSH, so the game never waits on a query.
// Plain D3D11 queries work on any vendor's driver. GPU timestamps run on their own clock; the client
// aligns them to the CPU clock. Nothing here runs unless a profile or the live view is sampling.
#include "logs.h"
#include "perf.h"
#include "profiler.h"
#include "trace.h"

#include <Windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <MinHook.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <deque>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace devtools::profiler
{
    namespace
    {
        constexpr int slotCount = 8;
        constexpr int maxStamps = 34;  // frame start, up to 32 passes, frame end
        constexpr int staleFrames = 30;
        constexpr std::size_t maxRecords = 40000;
        // ID3D11DeviceContext vtable slots (IUnknown 3 + ID3D11DeviceChild 4 come first).
        constexpr int clearRtvSlot = 50;
        constexpr int clearDsvSlot = 53;
        constexpr std::uint16_t frameLabel = 0;
        constexpr std::uint16_t presentLabel = 1;

        struct Slot
        {
            ID3D11Query* disjoint = nullptr;
            ID3D11Query* stamps[maxStamps]{};
            std::uint16_t labels[maxStamps]{};
            int used = 0;
            bool pending = false;
            int age = 0;
            int dropped = 0;
            std::uint64_t frame = 0;
            double cpuBegin = 0.0;
            double cpuEnd = 0.0;
            double hookSeconds = 0.0;
        };

        struct Record
        {
            std::uint64_t frame;
            double cpuBegin;
            double cpuEnd;
            double hookSeconds;
            int dropped;
            std::vector<std::pair<std::uint16_t, double>> stamps;  // label, GPU seconds
        };

        ID3D11Device* device = nullptr;
        ID3D11DeviceContext* context = nullptr;
        bool broken = false;
        std::string reason = "no sampled frame yet";
        UINT backWidth = 0;
        UINT backHeight = 0;
        Slot slots[slotCount];
        Slot* current = nullptr;
        bool passHooks = false;
        bool passHooksOk = false;
        std::uint64_t disjointFrames = 0;
        std::uint64_t staleDropped = 0;

        std::mutex recordMutex;
        std::deque<Record> records;
        std::vector<std::string> labelNames{ "frame", "present" };
        std::unordered_map<std::string, std::uint16_t> labelIndex{ { "frame", frameLabel }, { "present", presentLabel } };

        using ClearRtvFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, ID3D11RenderTargetView*, const FLOAT[4]);
        using ClearDsvFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, ID3D11DepthStencilView*, UINT, FLOAT, UINT8);
        ClearRtvFn originalClearRtv = nullptr;
        ClearDsvFn originalClearDsv = nullptr;

        std::uint16_t Label(const std::string& name)
        {
            std::lock_guard lock(recordMutex);
            const auto [it, added] = labelIndex.try_emplace(name, static_cast<std::uint16_t>(labelNames.size()));
            if (added) {
                labelNames.push_back(name);
            }
            return it->second;
        }

        void Stamp(std::uint16_t label)
        {
            // The last stamp is kept for Present so every frame has an end.
            if (current->used >= (label == presentLabel ? maxStamps : maxStamps - 1)) {
                ++current->dropped;
                return;
            }
            context->End(current->stamps[current->used]);
            current->labels[current->used++] = label;
        }

        void STDMETHODCALLTYPE HookedClearRtv(ID3D11DeviceContext* self, ID3D11RenderTargetView* view, const FLOAT color[4])
        {
            if (self == context && current && view) {
                const double started = perf::Now();
                Stamp(Label(ViewLabel("rtv", view)));
                current->hookSeconds += perf::Now() - started;
            }
            if (view && trace::Active()) {
                trace::OnPassBoundary(self, "rtv", view);
            }
            if (view && GpaWantsClears()) {
                GpaOnClear(self, "rtv", view);
            }
            originalClearRtv(self, view, color);
        }

        void STDMETHODCALLTYPE HookedClearDsv(ID3D11DeviceContext* self, ID3D11DepthStencilView* view, UINT flags, FLOAT depth,
            UINT8 stencil)
        {
            if (self == context && current && view) {
                const double started = perf::Now();
                Stamp(Label(ViewLabel("dsv", view)));
                current->hookSeconds += perf::Now() - started;
            }
            if (view && trace::Active()) {
                trace::OnPassBoundary(self, "dsv", view);
            }
            if (view && GpaWantsClears()) {
                GpaOnClear(self, "dsv", view);
            }
            originalClearDsv(self, view, flags, depth, stencil);
        }

        bool Init(void* swapChainPointer)
        {
            auto* swapChain = static_cast<IDXGISwapChain*>(swapChainPointer);
            if (FAILED(swapChain->GetDevice(__uuidof(ID3D11Device), reinterpret_cast<void**>(&device))) || !device) {
                broken = true;
                reason = "the swap chain has no D3D11 device";
                return false;
            }
            DXGI_SWAP_CHAIN_DESC desc{};
            if (SUCCEEDED(swapChain->GetDesc(&desc))) {
                backWidth = desc.BufferDesc.Width;
                backHeight = desc.BufferDesc.Height;
            }
            D3D11_QUERY_DESC disjointDesc{ D3D11_QUERY_TIMESTAMP_DISJOINT, 0 };
            D3D11_QUERY_DESC stampDesc{ D3D11_QUERY_TIMESTAMP, 0 };
            for (auto& slot : slots) {
                if (FAILED(device->CreateQuery(&disjointDesc, &slot.disjoint))) {
                    broken = true;
                }
                for (auto*& stamp : slot.stamps) {
                    if (FAILED(device->CreateQuery(&stampDesc, &stamp))) {
                        broken = true;
                    }
                }
            }
            if (broken) {
                reason = "the device refused timestamp queries";
                return false;
            }
            device->GetImmediateContext(&context);
            reason.clear();
            return context != nullptr;
        }

        void Collect()
        {
            for (auto& slot : slots) {
                if (!slot.pending) {
                    continue;
                }
                D3D11_QUERY_DATA_TIMESTAMP_DISJOINT disjoint{};
                if (context->GetData(slot.disjoint, &disjoint, sizeof(disjoint), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) {
                    if (++slot.age > staleFrames) {
                        slot.pending = false;
                        ++staleDropped;
                    }
                    continue;
                }
                Record record{ slot.frame, slot.cpuBegin, slot.cpuEnd, slot.hookSeconds, slot.dropped, {} };
                bool ok = !disjoint.Disjoint && disjoint.Frequency;
                const double period = ok ? 1.0 / static_cast<double>(disjoint.Frequency) : 0.0;
                record.stamps.reserve(slot.used);
                for (int i = 0; ok && i < slot.used; ++i) {
                    UINT64 ticks = 0;
                    if (context->GetData(slot.stamps[i], &ticks, sizeof(ticks), D3D11_ASYNC_GETDATA_DONOTFLUSH) != S_OK) {
                        ok = false;
                        break;
                    }
                    record.stamps.emplace_back(slot.labels[i], static_cast<double>(ticks) * period);
                }
                if (!ok && slot.age++ <= staleFrames && !disjoint.Disjoint) {
                    continue;  // a stamp behind the disjoint query is not in yet; try next frame
                }
                slot.pending = false;
                if (!ok) {
                    ++disjointFrames;
                    continue;
                }
                std::lock_guard lock(recordMutex);
                records.push_back(std::move(record));
                if (records.size() > maxRecords) {
                    records.pop_front();
                }
            }
        }

        double Rounded(double value, double scale = 1e5)
        {
            return std::round(value * scale) / scale;
        }
    }

    std::string ViewLabel(const char* kind, void* viewPointer)
    {
        auto* view = static_cast<ID3D11View*>(viewPointer);
        ID3D11Resource* resource = nullptr;
        view->GetResource(&resource);
        if (!resource) {
            return kind;
        }
        D3D11_RESOURCE_DIMENSION dimension{};
        resource->GetType(&dimension);
        char text[64];
        if (dimension == D3D11_RESOURCE_DIMENSION_TEXTURE2D) {
            D3D11_TEXTURE2D_DESC desc{};
            static_cast<ID3D11Texture2D*>(resource)->GetDesc(&desc);
            std::snprintf(text, sizeof(text), "%s %ux%u%s", kind, desc.Width, desc.Height, desc.ArraySize > 1 ? " array" : "");
        } else {
            std::snprintf(text, sizeof(text), "%s other", kind);
        }
        resource->Release();
        return text;
    }

    // Clear hooks go in on the first sampled frame or GPA capture, not at load, so an unprofiled game runs untouched.
    void EnsurePassHooks(void* immediateContext)
    {
        if (passHooks || !immediateContext) {
            return;
        }
        passHooks = true;
        auto** vtable = *static_cast<void***>(immediateContext);
        const bool rtv = MH_CreateHook(vtable[clearRtvSlot], reinterpret_cast<void*>(&HookedClearRtv),
                             reinterpret_cast<void**>(&originalClearRtv)) == MH_OK &&
                         MH_EnableHook(vtable[clearRtvSlot]) == MH_OK;
        const bool dsv = MH_CreateHook(vtable[clearDsvSlot], reinterpret_cast<void*>(&HookedClearDsv),
                             reinterpret_cast<void**>(&originalClearDsv)) == MH_OK &&
                         MH_EnableHook(vtable[clearDsvSlot]) == MH_OK;
        passHooksOk = rtv && dsv;
        if (!passHooksOk) {
            logs::Plugin("warn", "profiler: GPU pass hooks not installed; frames are timed without passes");
        }
    }

    bool PassHooksOk()
    {
        return passHooksOk;
    }

    void GpuBeforePresent(void* swapChain)
    {
        GpaBeforePresent(swapChain);
        if (!context) {
            if (broken || !Sampling() || !Init(swapChain)) {
                return;
            }
        }
        const double started = perf::Now();
        Collect();
        if (current) {
            Stamp(presentLabel);
            context->End(current->disjoint);
            current->pending = true;
            current->age = 0;
            current->cpuEnd = started;
            current->hookSeconds += perf::Now() - started;
            current = nullptr;
        }
    }

    void GpuAfterPresent(std::uint64_t nextFrame)
    {
        GpaAfterPresent();
        if (!context || !Sampling()) {
            return;
        }
        const double started = perf::Now();
        EnsurePassHooks(context);
        Slot* slot = nullptr;
        for (auto& candidate : slots) {
            if (!candidate.pending) {
                slot = &candidate;
                break;
            }
        }
        if (!slot) {
            return;  // every slot still waits on the GPU; this frame goes untimed
        }
        slot->used = 0;
        slot->dropped = 0;
        slot->frame = nextFrame;
        slot->cpuBegin = started;
        slot->hookSeconds = 0.0;
        context->Begin(slot->disjoint);
        current = slot;
        Stamp(frameLabel);
        current->hookSeconds += perf::Now() - started;
    }

    Json GpuFrames(double from, double to, double origin)
    {
        auto frames = Json::array();
        std::lock_guard lock(recordMutex);
        for (const auto& record : records) {
            if (record.cpuEnd < from || record.cpuBegin > to) {
                continue;
            }
            auto stamps = Json::array();
            for (const auto& [label, at] : record.stamps) {
                stamps.push_back({ label, Rounded(at, 1e7) });
            }
            frames.push_back({ record.frame, Rounded((record.cpuBegin - origin) * 1000.0, 100.0),
                Rounded((record.cpuEnd - origin) * 1000.0, 100.0), Rounded(record.hookSeconds * 1e6, 10.0), record.dropped,
                std::move(stamps) });
        }
        return Json{ { "available", context != nullptr }, { "reason", context ? std::string() : reason },
            { "backbuffer", { backWidth, backHeight } }, { "labels", labelNames }, { "disjointFrames", disjointFrames },
            { "staleFrames", staleDropped }, { "frames", std::move(frames) } };
    }

    Json GpuRecent(double seconds)
    {
        const double now = perf::Now();
        std::vector<double> gpuMs;
        std::unordered_map<std::uint16_t, std::pair<double, int>> passes;
        double hookUs = 0.0;
        std::vector<std::string> names;
        {
            std::lock_guard lock(recordMutex);
            names = labelNames;
            for (auto it = records.rbegin(); it != records.rend() && it->cpuEnd >= now - seconds; ++it) {
                if (it->stamps.size() < 2) {
                    continue;
                }
                gpuMs.push_back((it->stamps.back().second - it->stamps.front().second) * 1000.0);
                hookUs += it->hookSeconds * 1e6;
                for (std::size_t i = 0; i + 1 < it->stamps.size(); ++i) {
                    auto& pass = passes[it->stamps[i].first];
                    pass.first += (it->stamps[i + 1].second - it->stamps[i].second) * 1000.0;
                    ++pass.second;
                }
            }
        }
        Json out{ { "available", context != nullptr }, { "reason", context ? std::string() : reason }, { "frames", gpuMs.size() } };
        if (gpuMs.empty()) {
            return out;
        }
        std::sort(gpuMs.begin(), gpuMs.end());
        const auto at = [&](double p) { return Rounded(gpuMs[static_cast<std::size_t>(p * static_cast<double>(gpuMs.size() - 1))], 100.0); };
        out["gpuMsP50"] = at(0.5);
        out["gpuMsP95"] = at(0.95);
        out["gpuMsMax"] = Rounded(gpuMs.back(), 100.0);
        out["hookUsPerFrame"] = Rounded(hookUs / static_cast<double>(gpuMs.size()), 10.0);
        std::vector<std::pair<double, std::uint16_t>> order;
        for (const auto& [label, sum] : passes) {
            order.emplace_back(sum.first / static_cast<double>(gpuMs.size()), label);
        }
        std::sort(order.rbegin(), order.rend());
        auto top = Json::array();
        for (std::size_t i = 0; i < order.size() && i < 10; ++i) {
            top.push_back({ { "pass", names[order[i].second] }, { "msPerFrame", Rounded(order[i].first, 1000.0) } });
        }
        out["passes"] = std::move(top);
        return out;
    }
}
