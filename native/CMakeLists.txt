cmake_minimum_required(VERSION 3.23)
project(SkyrimDevTools LANGUAGES CXX)

if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "The plugin requires Windows x64")
endif()

if(NOT CMAKE_CONFIGURATION_TYPES AND NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

set(COMMONLIB_SOURCE "" CACHE PATH "Pinned CommonLibSSE-NG checkout")
set(COMMONLIB_PREFIX "" CACHE PATH "Install prefix of a prebuilt CommonLibSSE-NG; when empty it is built from COMMONLIB_SOURCE")
set(HTTPLIB_SOURCE "" CACHE PATH "Pinned cpp-httplib checkout")
include("${CMAKE_CURRENT_SOURCE_DIR}/commonlib-options.cmake")
if(COMMONLIB_PREFIX)
    find_package(CommonLibSSE CONFIG REQUIRED PATHS "${COMMONLIB_PREFIX}" NO_DEFAULT_PATH)
else()
    add_subdirectory("${COMMONLIB_SOURCE}" commonlib)
endif()
find_package(nlohmann_json CONFIG REQUIRED)
find_package(minhook CONFIG REQUIRED)

# The transport is kept apart from CommonLib so httplib and the game headers never share a unit.
add_library(SkyrimDevToolsServer STATIC server/server.cpp)
target_compile_features(SkyrimDevToolsServer PRIVATE cxx_std_20)
target_include_directories(SkyrimDevToolsServer PRIVATE "${HTTPLIB_SOURCE}" "${CMAKE_CURRENT_SOURCE_DIR}")
target_compile_definitions(SkyrimDevToolsServer PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN
    _WIN32_WINNT=0x0A00 CPPHTTPLIB_WEBSOCKET_MAX_PAYLOAD_LENGTH=16777216)
target_link_libraries(SkyrimDevToolsServer PUBLIC nlohmann_json::nlohmann_json PRIVATE ws2_32)

# Every module .cpp is linked straight into the DLL, so its static registrar is never dropped.
file(GLOB DEVTOOLS_MODULES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")
add_library(SkyrimDevTools SHARED ${DEVTOOLS_MODULES})
target_compile_features(SkyrimDevTools PRIVATE cxx_std_23)
target_include_directories(SkyrimDevTools PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}" "${CMAKE_CURRENT_SOURCE_DIR}/third_party/gpa")
target_compile_definitions(SkyrimDevTools PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
target_link_libraries(SkyrimDevTools PRIVATE
    CommonLibSSE::CommonLibSSE SkyrimDevToolsServer minhook::minhook user32)
# Game headers dominate every module's compile time, so they are parsed once per build.
target_precompile_headers(SkyrimDevTools PRIVATE pch.h)

# Optional AMD board metrics. The ADLX SDK headers carry AMD's own licence, so they are never vendored; point
# ADLX_SDK at the SDK folder of a local ADLX checkout to build the helper DLL that the plugin loads if present.
set(ADLX_SDK "" CACHE PATH "SDK folder of a local ADLX checkout (optional)")
if(ADLX_SDK)
    add_library(SkyrimDevToolsAdlx SHARED adlx/devtools_adlx.cpp)
    target_compile_features(SkyrimDevToolsAdlx PRIVATE cxx_std_17)
    target_include_directories(SkyrimDevToolsAdlx PRIVATE "${ADLX_SDK}/Include")
    target_compile_definitions(SkyrimDevToolsAdlx PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
endif()
