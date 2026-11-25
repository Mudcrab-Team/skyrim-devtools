// SPDX-License-Identifier: GPL-3.0-or-later
#include "screen_gfx.h"

#include <Windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <wincodec.h>
#include <wrl/client.h>

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <stdexcept>

#pragma comment(lib, "windowscodecs.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "uuid.lib")

namespace devtools::gfx
{
    using Microsoft::WRL::ComPtr;

    namespace
    {
        void Check(HRESULT result, const char* what)
        {
            if (FAILED(result)) {
                char text[96];
                std::snprintf(text, sizeof(text), "%s failed (0x%08lX)", what, static_cast<unsigned long>(result));
                throw std::runtime_error(text);
            }
        }

        bool Supported(std::uint32_t format)
        {
            switch (format) {
            case DXGI_FORMAT_R8G8B8A8_UNORM:
            case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
            case DXGI_FORMAT_B8G8R8A8_UNORM:
            case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
            case DXGI_FORMAT_B8G8R8X8_UNORM:
            case DXGI_FORMAT_B8G8R8X8_UNORM_SRGB:
            case DXGI_FORMAT_R10G10B10A2_UNORM:
                return true;
            default:
                return false;
            }
        }

        // Writes one source pixel as blue, green, red.
        inline void ToBgr(std::uint32_t format, const std::uint8_t* p, std::uint32_t& b, std::uint32_t& g, std::uint32_t& r)
        {
            switch (format) {
            case DXGI_FORMAT_R8G8B8A8_UNORM:
            case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB:
                r = p[0]; g = p[1]; b = p[2];
                break;
            case DXGI_FORMAT_R10G10B10A2_UNORM: {
                std::uint32_t v;
                std::memcpy(&v, p, 4);
                r = ((v >> 0) & 0x3FF) >> 2;
                g = ((v >> 10) & 0x3FF) >> 2;
                b = ((v >> 20) & 0x3FF) >> 2;
                break;
            }
            default:
                b = p[0]; g = p[1]; r = p[2];
                break;
            }
        }
    }

    Frame ReadBackbuffer(void* swapChainPointer)
    {
        auto* swapChain = static_cast<IDXGISwapChain*>(swapChainPointer);
        ComPtr<ID3D11Texture2D> backbuffer;
        Check(swapChain->GetBuffer(0, __uuidof(ID3D11Texture2D), reinterpret_cast<void**>(backbuffer.GetAddressOf())),
            "GetBuffer");
        D3D11_TEXTURE2D_DESC desc{};
        backbuffer->GetDesc(&desc);
        if (!Supported(desc.Format)) {
            throw std::runtime_error("Unsupported backbuffer format " + std::to_string(desc.Format));
        }
        if (desc.SampleDesc.Count != 1) {
            throw std::runtime_error("The backbuffer is multisampled");
        }
        ComPtr<ID3D11Device> device;
        backbuffer->GetDevice(device.GetAddressOf());
        ComPtr<ID3D11DeviceContext> context;
        device->GetImmediateContext(context.GetAddressOf());

        desc.Usage = D3D11_USAGE_STAGING;
        desc.BindFlags = 0;
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        desc.MiscFlags = 0;
        desc.MipLevels = 1;
        desc.ArraySize = 1;
        ComPtr<ID3D11Texture2D> staging;
        Check(device->CreateTexture2D(&desc, nullptr, staging.GetAddressOf()), "CreateTexture2D");
        context->CopyResource(staging.Get(), backbuffer.Get());

        D3D11_MAPPED_SUBRESOURCE mapped{};
        Check(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Map");
        Frame frame;
        frame.width = desc.Width;
        frame.height = desc.Height;
        frame.format = desc.Format;
        const std::size_t row = std::size_t{ desc.Width } * 4;
        frame.pixels.resize(row * desc.Height);
        for (std::uint32_t y = 0; y < desc.Height; ++y) {
            std::memcpy(frame.pixels.data() + y * row, static_cast<const std::uint8_t*>(mapped.pData) + y * mapped.RowPitch, row);
        }
        context->Unmap(staging.Get(), 0);
        return frame;
    }

    Encoded Encode(const Frame& frame, std::uint32_t maxWidth, bool jpeg, int quality)
    {
        if (!frame.width || !frame.height || frame.pixels.size() < std::size_t{ frame.width } * frame.height * 4) {
            throw std::runtime_error("Empty frame");
        }
        const std::uint32_t outWidth = std::min(frame.width, std::max<std::uint32_t>(maxWidth, 1));
        const std::uint32_t outHeight = std::max<std::uint32_t>(
            1, static_cast<std::uint32_t>(std::uint64_t{ frame.height } * outWidth / frame.width));

        // Area average: every output pixel is the mean of the source block it covers.
        const std::size_t stride = (std::size_t{ outWidth } * 3 + 3) & ~std::size_t{ 3 };
        std::vector<std::uint8_t> rgb(stride * outHeight);
        const std::size_t srcRow = std::size_t{ frame.width } * 4;
        for (std::uint32_t y = 0; y < outHeight; ++y) {
            const std::uint32_t y0 = static_cast<std::uint32_t>(std::uint64_t{ y } * frame.height / outHeight);
            const std::uint32_t y1 = std::max(y0 + 1, static_cast<std::uint32_t>(std::uint64_t{ y + 1 } * frame.height / outHeight));
            for (std::uint32_t x = 0; x < outWidth; ++x) {
                const std::uint32_t x0 = static_cast<std::uint32_t>(std::uint64_t{ x } * frame.width / outWidth);
                const std::uint32_t x1 = std::max(x0 + 1, static_cast<std::uint32_t>(std::uint64_t{ x + 1 } * frame.width / outWidth));
                std::uint64_t sb = 0, sg = 0, sr = 0;
                for (std::uint32_t sy = y0; sy < y1; ++sy) {
                    const std::uint8_t* p = frame.pixels.data() + sy * srcRow + std::size_t{ x0 } * 4;
                    for (std::uint32_t sx = x0; sx < x1; ++sx, p += 4) {
                        std::uint32_t b, g, r;
                        ToBgr(frame.format, p, b, g, r);
                        sb += b; sg += g; sr += r;
                    }
                }
                const std::uint64_t count = std::uint64_t{ y1 - y0 } * (x1 - x0);
                std::uint8_t* out = rgb.data() + y * stride + std::size_t{ x } * 3;
                out[0] = static_cast<std::uint8_t>(sb / count);
                out[1] = static_cast<std::uint8_t>(sg / count);
                out[2] = static_cast<std::uint8_t>(sr / count);
            }
        }

        const HRESULT init = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        struct Uninit
        {
            bool active;
            ~Uninit() { if (active) CoUninitialize(); }
        } uninit{ SUCCEEDED(init) };

        ComPtr<IWICImagingFactory> factory;
        Check(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(factory.GetAddressOf())),
            "WIC factory");
        ComPtr<IStream> stream;
        Check(CreateStreamOnHGlobal(nullptr, TRUE, stream.GetAddressOf()), "CreateStreamOnHGlobal");
        ComPtr<IWICBitmapEncoder> encoder;
        Check(factory->CreateEncoder(jpeg ? GUID_ContainerFormatJpeg : GUID_ContainerFormatPng, nullptr, encoder.GetAddressOf()),
            "CreateEncoder");
        Check(encoder->Initialize(stream.Get(), WICBitmapEncoderNoCache), "Encoder Initialize");
        ComPtr<IWICBitmapFrameEncode> target;
        ComPtr<IPropertyBag2> options;
        Check(encoder->CreateNewFrame(target.GetAddressOf(), options.GetAddressOf()), "CreateNewFrame");
        if (jpeg && options) {
            PROPBAG2 name{};
            name.pstrName = const_cast<LPOLESTR>(L"ImageQuality");
            VARIANT value;
            VariantInit(&value);
            value.vt = VT_R4;
            value.fltVal = static_cast<float>(std::clamp(quality, 1, 100)) / 100.0f;
            options->Write(1, &name, &value);
        }
        Check(target->Initialize(options.Get()), "Frame Initialize");
        Check(target->SetSize(outWidth, outHeight), "SetSize");
        WICPixelFormatGUID format = GUID_WICPixelFormat24bppBGR;
        Check(target->SetPixelFormat(&format), "SetPixelFormat");
        Check(target->WritePixels(outHeight, static_cast<UINT>(stride), static_cast<UINT>(rgb.size()), rgb.data()),
            "WritePixels");
        Check(target->Commit(), "Frame Commit");
        Check(encoder->Commit(), "Encoder Commit");

        STATSTG stat{};
        Check(stream->Stat(&stat, STATFLAG_NONAME), "Stream Stat");
        Encoded result;
        result.width = outWidth;
        result.height = outHeight;
        result.bytes.resize(static_cast<std::size_t>(stat.cbSize.QuadPart));
        LARGE_INTEGER zero{};
        Check(stream->Seek(zero, STREAM_SEEK_SET, nullptr), "Stream Seek");
        ULONG read = 0;
        Check(stream->Read(result.bytes.data(), static_cast<ULONG>(result.bytes.size()), &read), "Stream Read");
        result.bytes.resize(read);
        return result;
    }

    std::string Base64(const std::vector<std::uint8_t>& bytes)
    {
        static constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        std::string out;
        out.reserve((bytes.size() + 2) / 3 * 4);
        std::size_t i = 0;
        for (; i + 2 < bytes.size(); i += 3) {
            const std::uint32_t v = bytes[i] << 16 | bytes[i + 1] << 8 | bytes[i + 2];
            out += alphabet[v >> 18];
            out += alphabet[(v >> 12) & 63];
            out += alphabet[(v >> 6) & 63];
            out += alphabet[v & 63];
        }
        if (i + 1 == bytes.size()) {
            const std::uint32_t v = bytes[i] << 16;
            out += alphabet[v >> 18];
            out += alphabet[(v >> 12) & 63];
            out += "==";
        } else if (i + 2 == bytes.size()) {
            const std::uint32_t v = bytes[i] << 16 | bytes[i + 1] << 8;
            out += alphabet[v >> 18];
            out += alphabet[(v >> 12) & 63];
            out += alphabet[(v >> 6) & 63];
            out += '=';
        }
        return out;
    }
}
