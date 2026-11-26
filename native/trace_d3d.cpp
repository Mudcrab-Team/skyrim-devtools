// SPDX-License-Identifier: GPL-3.0-or-later
// D3D11 API tracing on the immediate context. Hooks go on the context's vtable targets the first time a
// traced frame is presented and stay; every detour checks the active flag first.
//
// The default "batched" mode keeps one record per render pass instead of one per call: a Skyrim frame makes
// thousands of draws, and a record per draw would cost more than the draws' own CPU time. A pass runs from one
// render-target or depth clear to the next, the same boundaries the GPU timer stamps, so pass k on the CPU
// lines up with GPU pass k of the same frame. "full" mode records every call with its arguments.
#include "trace.h"

#include "logs.h"
#include "profiler.h"

#include <Windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <MinHook.h>

#include <atomic>
#include <string>
#include <unordered_map>

namespace devtools::trace
{
    namespace
    {
        // ID3D11DeviceContext vtable slots (IUnknown 3 + ID3D11DeviceChild 4 come first).
        constexpr int drawIndexedSlot = 12;
        constexpr int drawSlot = 13;
        constexpr int mapSlot = 14;
        constexpr int unmapSlot = 15;
        constexpr int drawIndexedInstancedSlot = 20;
        constexpr int drawInstancedSlot = 21;
        constexpr int omSetRenderTargetsSlot = 33;
        constexpr int dispatchSlot = 41;
        constexpr int copySubresourceRegionSlot = 46;
        constexpr int copyResourceSlot = 47;
        constexpr int updateSubresourceSlot = 48;
        constexpr int flushSlot = 111;

        using DrawIndexedFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, UINT, UINT, INT);
        using DrawFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, UINT, UINT);
        using MapFn = HRESULT(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, ID3D11Resource*, UINT, D3D11_MAP, UINT, D3D11_MAPPED_SUBRESOURCE*);
        using UnmapFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, ID3D11Resource*, UINT);
        using DrawIndexedInstancedFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, UINT, UINT, UINT, INT, UINT);
        using DrawInstancedFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, UINT, UINT, UINT, UINT);
        using OmSetRenderTargetsFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, UINT, ID3D11RenderTargetView* const*, ID3D11DepthStencilView*);
        using DispatchFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, UINT, UINT, UINT);
        using CopySubresourceRegionFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, ID3D11Resource*, UINT, UINT, UINT, UINT,
            ID3D11Resource*, UINT, const D3D11_BOX*);
        using CopyResourceFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, ID3D11Resource*, ID3D11Resource*);
        using UpdateSubresourceFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*, ID3D11Resource*, UINT, const D3D11_BOX*,
            const void*, UINT, UINT);
        using FlushFn = void(STDMETHODCALLTYPE*)(ID3D11DeviceContext*);

        DrawIndexedFn originalDrawIndexed = nullptr;
        DrawFn originalDraw = nullptr;
        MapFn originalMap = nullptr;
        UnmapFn originalUnmap = nullptr;
        DrawIndexedInstancedFn originalDrawIndexedInstanced = nullptr;
        DrawInstancedFn originalDrawInstanced = nullptr;
        OmSetRenderTargetsFn originalOmSetRenderTargets = nullptr;
        DispatchFn originalDispatch = nullptr;
        CopySubresourceRegionFn originalCopySubresourceRegion = nullptr;
        CopyResourceFn originalCopyResource = nullptr;
        UpdateSubresourceFn originalUpdateSubresource = nullptr;
        FlushFn originalFlush = nullptr;

        ID3D11DeviceContext* context = nullptr;
        bool installTried = false;
        Json status{ { "installed", false }, { "reason", "no traced frame yet" } };

        // Render-thread state; only the immediate context's thread touches it.
        struct Pass
        {
            std::int64_t start = 0;
            std::uint32_t name = 0;
            std::uint32_t index = 0;
            std::uint64_t draws = 0;
            std::uint64_t maps = 0;
            std::uint64_t dispatches = 0;
            std::uint64_t others = 0;
            std::int64_t apiTicks = 0;
        } pass;
        std::int64_t presentStart = 0;
        std::int64_t frequency = 1;
        std::atomic<std::uint64_t> calls = 0;
        std::atomic<std::uint64_t> frames = 0;
        std::unordered_map<void*, std::uint32_t> viewNames;
        // Start runs on a connection thread; the render thread applies the reset at its next Present.
        std::atomic_bool resetPending = false;

        void ApplyReset()
        {
            if (resetPending.exchange(false)) {
                pass = Pass{};
                presentStart = 0;
            }
        }

        struct Names
        {
            std::uint32_t drawIndexed, draw, map, unmap, drawIndexedInstanced, drawInstanced, omSetRenderTargets, dispatch,
                copySubresourceRegion, copyResource, updateSubresource, flush, present, framePass;
        } names{};

        bool Ours(ID3D11DeviceContext* self)
        {
            return self == context && Active() && Current().apiMode != 0;
        }

        bool Full()
        {
            return Current().apiMode == 2;
        }

        void FlushPass(std::int64_t at)
        {
            if (pass.start) {
                const auto apiUs = static_cast<std::uint64_t>(pass.apiTicks * 1'000'000 / frequency);
                Complete(pass.name, pass.start, at, pass.draws | (pass.maps << 32),
                    (pass.dispatches & 0xFFFF) | ((pass.others & 0xFFFF) << 16) | (apiUs << 32), pass.index & 0xFFF);
            }
            pass.start = at;
            pass.draws = pass.maps = pass.dispatches = pass.others = 0;
            pass.apiTicks = 0;
        }

        // Batched calls only add to the pass; full mode records each.
        template <class Call>
        void Timed(std::uint32_t name, std::uint64_t& counter, std::uint64_t a, std::uint64_t b, Call&& call)
        {
            const auto start = Ticks();
            call();
            const auto end = Ticks();
            calls.fetch_add(1, std::memory_order_relaxed);
            ++counter;
            pass.apiTicks += end - start;
            if (Full()) {
                Complete(name, start, end, a, b);
            }
        }

        // Rare or expensive calls are always their own record.
        template <class Call>
        void Single(std::uint32_t name, std::uint64_t a, std::uint64_t b, Call&& call)
        {
            const auto start = Ticks();
            call();
            const auto end = Ticks();
            calls.fetch_add(1, std::memory_order_relaxed);
            pass.apiTicks += end - start;
            Complete(name, start, end, a, b);
        }

        std::uint64_t ResourceBytes(ID3D11Resource* resource)
        {
            if (!resource) {
                return 0;
            }
            D3D11_RESOURCE_DIMENSION dimension{};
            resource->GetType(&dimension);
            if (dimension == D3D11_RESOURCE_DIMENSION_BUFFER) {
                D3D11_BUFFER_DESC desc{};
                static_cast<ID3D11Buffer*>(resource)->GetDesc(&desc);
                return desc.ByteWidth;
            }
            return 0;
        }

        void STDMETHODCALLTYPE HookedDrawIndexed(ID3D11DeviceContext* self, UINT count, UINT start, INT base)
        {
            if (!Ours(self)) {
                return originalDrawIndexed(self, count, start, base);
            }
            Timed(names.drawIndexed, pass.draws, count, 1, [&] { originalDrawIndexed(self, count, start, base); });
        }

        void STDMETHODCALLTYPE HookedDraw(ID3D11DeviceContext* self, UINT count, UINT start)
        {
            if (!Ours(self)) {
                return originalDraw(self, count, start);
            }
            Timed(names.draw, pass.draws, count, 1, [&] { originalDraw(self, count, start); });
        }

        HRESULT STDMETHODCALLTYPE HookedMap(ID3D11DeviceContext* self, ID3D11Resource* resource, UINT sub, D3D11_MAP type, UINT flags,
            D3D11_MAPPED_SUBRESOURCE* mapped)
        {
            if (!Ours(self)) {
                return originalMap(self, resource, sub, type, flags, mapped);
            }
            HRESULT result = S_OK;
            Timed(names.map, pass.maps, Full() ? ResourceBytes(resource) : 0, type,
                [&] { result = originalMap(self, resource, sub, type, flags, mapped); });
            return result;
        }

        void STDMETHODCALLTYPE HookedUnmap(ID3D11DeviceContext* self, ID3D11Resource* resource, UINT sub)
        {
            if (!Ours(self)) {
                return originalUnmap(self, resource, sub);
            }
            Timed(names.unmap, pass.others, 0, 0, [&] { originalUnmap(self, resource, sub); });
        }

        void STDMETHODCALLTYPE HookedDrawIndexedInstanced(ID3D11DeviceContext* self, UINT count, UINT instances, UINT start, INT base,
            UINT startInstance)
        {
            if (!Ours(self)) {
                return originalDrawIndexedInstanced(self, count, instances, start, base, startInstance);
            }
            Timed(names.drawIndexedInstanced, pass.draws, count, instances,
                [&] { originalDrawIndexedInstanced(self, count, instances, start, base, startInstance); });
        }

        void STDMETHODCALLTYPE HookedDrawInstanced(ID3D11DeviceContext* self, UINT count, UINT instances, UINT start, UINT startInstance)
        {
            if (!Ours(self)) {
                return originalDrawInstanced(self, count, instances, start, startInstance);
            }
            Timed(names.drawInstanced, pass.draws, count, instances,
                [&] { originalDrawInstanced(self, count, instances, start, startInstance); });
        }

        void STDMETHODCALLTYPE HookedOmSetRenderTargets(ID3D11DeviceContext* self, UINT count, ID3D11RenderTargetView* const* views,
            ID3D11DepthStencilView* depth)
        {
            if (!Ours(self)) {
                return originalOmSetRenderTargets(self, count, views, depth);
            }
            Timed(names.omSetRenderTargets, pass.others, count, depth != nullptr,
                [&] { originalOmSetRenderTargets(self, count, views, depth); });
        }

        void STDMETHODCALLTYPE HookedDispatch(ID3D11DeviceContext* self, UINT x, UINT y, UINT z)
        {
            if (!Ours(self)) {
                return originalDispatch(self, x, y, z);
            }
            Timed(names.dispatch, pass.dispatches, x | (static_cast<std::uint64_t>(y) << 21) | (static_cast<std::uint64_t>(z) << 42), 0,
                [&] { originalDispatch(self, x, y, z); });
        }

        void STDMETHODCALLTYPE HookedCopySubresourceRegion(ID3D11DeviceContext* self, ID3D11Resource* dst, UINT dstSub, UINT x, UINT y,
            UINT z, ID3D11Resource* src, UINT srcSub, const D3D11_BOX* box)
        {
            if (!Ours(self)) {
                return originalCopySubresourceRegion(self, dst, dstSub, x, y, z, src, srcSub, box);
            }
            Single(names.copySubresourceRegion, 0, 0, [&] { originalCopySubresourceRegion(self, dst, dstSub, x, y, z, src, srcSub, box); });
        }

        void STDMETHODCALLTYPE HookedCopyResource(ID3D11DeviceContext* self, ID3D11Resource* dst, ID3D11Resource* src)
        {
            if (!Ours(self)) {
                return originalCopyResource(self, dst, src);
            }
            Single(names.copyResource, 0, 0, [&] { originalCopyResource(self, dst, src); });
        }

        void STDMETHODCALLTYPE HookedUpdateSubresource(ID3D11DeviceContext* self, ID3D11Resource* dst, UINT sub, const D3D11_BOX* box,
            const void* data, UINT rowPitch, UINT depthPitch)
        {
            if (!Ours(self)) {
                return originalUpdateSubresource(self, dst, sub, box, data, rowPitch, depthPitch);
            }
            Timed(names.updateSubresource, pass.others, Full() ? ResourceBytes(dst) : 0, 0,
                [&] { originalUpdateSubresource(self, dst, sub, box, data, rowPitch, depthPitch); });
        }

        void STDMETHODCALLTYPE HookedFlush(ID3D11DeviceContext* self)
        {
            if (!Ours(self)) {
                return originalFlush(self);
            }
            Single(names.flush, 0, 0, [&] { originalFlush(self); });
        }

        template <class Fn>
        bool Hook(void** vtable, int slot, Fn detour, Fn& original, std::unordered_map<void*, bool>& seen, Json& slots)
        {
            void* target = vtable[slot];
            if (seen.contains(target)) {
                slots[std::to_string(slot)] = "shared target";
                return true;
            }
            seen[target] = true;
            const auto created = MH_CreateHook(target, reinterpret_cast<void*>(detour), reinterpret_cast<void**>(&original));
            if (created != MH_OK) {
                slots[std::to_string(slot)] = MH_StatusToString(created);
                return false;
            }
            MH_QueueEnableHook(target);
            slots[std::to_string(slot)] = "ok";
            return true;
        }

        void Install(void* swapChainPointer)
        {
            installTried = true;
            auto* swapChain = static_cast<IDXGISwapChain*>(swapChainPointer);
            ID3D11Device* device = nullptr;
            if (!swapChain || FAILED(swapChain->GetDevice(__uuidof(ID3D11Device), reinterpret_cast<void**>(&device))) || !device) {
                status = Json{ { "installed", false }, { "reason", "swap chain has no D3D11 device" } };
                return;
            }
            device->GetImmediateContext(&context);
            device->Release();
            if (!context) {
                status = Json{ { "installed", false }, { "reason", "no immediate context" } };
                return;
            }
            // The context stays alive for the game's lifetime; the reference is dropped so it is not leaked twice.
            context->Release();
            LARGE_INTEGER f{};
            QueryPerformanceFrequency(&f);
            frequency = f.QuadPart;
            names = Names{ Name("DrawIndexed", "d3d11"), Name("Draw", "d3d11"), Name("Map", "d3d11"), Name("Unmap", "d3d11"),
                Name("DrawIndexedInstanced", "d3d11"), Name("DrawInstanced", "d3d11"), Name("OMSetRenderTargets", "d3d11"),
                Name("Dispatch", "d3d11"), Name("CopySubresourceRegion", "d3d11"), Name("CopyResource", "d3d11"),
                Name("UpdateSubresource", "d3d11"), Name("Flush", "d3d11"), Name("Present", "d3d11"), Name("pass frame", "pass") };
            auto** vtable = *reinterpret_cast<void***>(context);
            std::unordered_map<void*, bool> seen;
            Json slots = Json::object();
            bool ok = true;
            ok &= Hook(vtable, drawIndexedSlot, &HookedDrawIndexed, originalDrawIndexed, seen, slots);
            ok &= Hook(vtable, drawSlot, &HookedDraw, originalDraw, seen, slots);
            ok &= Hook(vtable, mapSlot, &HookedMap, originalMap, seen, slots);
            ok &= Hook(vtable, unmapSlot, &HookedUnmap, originalUnmap, seen, slots);
            ok &= Hook(vtable, drawIndexedInstancedSlot, &HookedDrawIndexedInstanced, originalDrawIndexedInstanced, seen, slots);
            ok &= Hook(vtable, drawInstancedSlot, &HookedDrawInstanced, originalDrawInstanced, seen, slots);
            ok &= Hook(vtable, omSetRenderTargetsSlot, &HookedOmSetRenderTargets, originalOmSetRenderTargets, seen, slots);
            ok &= Hook(vtable, dispatchSlot, &HookedDispatch, originalDispatch, seen, slots);
            ok &= Hook(vtable, copySubresourceRegionSlot, &HookedCopySubresourceRegion, originalCopySubresourceRegion, seen, slots);
            ok &= Hook(vtable, copyResourceSlot, &HookedCopyResource, originalCopyResource, seen, slots);
            ok &= Hook(vtable, updateSubresourceSlot, &HookedUpdateSubresource, originalUpdateSubresource, seen, slots);
            ok &= Hook(vtable, flushSlot, &HookedFlush, originalFlush, seen, slots);
            const auto applied = MH_ApplyQueued();
            status = Json{ { "installed", ok && applied == MH_OK }, { "slots", std::move(slots) },
                { "reason", applied == MH_OK ? std::string(ok ? "" : "some slots failed") : MH_StatusToString(applied) } };
            if (applied != MH_OK || !ok) {
                logs::Plugin("warn", "trace: some D3D11 hooks were not installed");
            }
        }
    }

    void BeforePresent(void* swapChain)
    {
        if (!Active() || Current().apiMode == 0) {
            return;
        }
        if (!installTried) {
            Install(swapChain);
            return;
        }
        ApplyReset();
        presentStart = Ticks();
        FlushPass(presentStart);
    }

    void AfterPresent(std::uint64_t nextFrame)
    {
        if (!Active() || !context || !presentStart) {
            return;
        }
        const auto now = Ticks();
        Complete(names.present, presentStart, now, nextFrame - 1);
        frames.fetch_add(1, std::memory_order_relaxed);
        presentStart = 0;
        pass.name = names.framePass;
        pass.index = 0;
        pass.start = 0;
        FlushPass(now);
    }

    void OnPassBoundary(void* self, const char* kind, void* view)
    {
        if (!Active() || self != context || !view || Current().apiMode == 0 || !pass.start) {
            return;
        }
        const auto now = Ticks();
        FlushPass(now);
        // Views are long-lived render targets; their labels are cached so a boundary costs a map lookup. A freed
        // view whose address is reused keeps the old label, which only names a pass, never splits one.
        auto [it, added] = viewNames.try_emplace(view, 0);
        if (added) {
            it->second = Name("pass " + profiler::ViewLabel(kind, view), "pass");
        }
        pass.name = it->second;
        ++pass.index;
    }

    void ResetApiSession()
    {
        calls.store(0);
        frames.store(0);
        resetPending.store(true);
    }

    Json ApiStatus()
    {
        auto out = status;
        out["calls"] = calls.load();
        out["frames"] = frames.load();
        return out;
    }
}
