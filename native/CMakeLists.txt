cmake_minimum_required(VERSION 3.23)
project(SkyrimDevTools LANGUAGES CXX)

if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "The plugin requires Windows x64")
endif()

set(COMMONLIB_SOURCE "" CACHE PATH "Pinned CommonLibSSE-NG checkout")
set(HTTPLIB_SOURCE "" CACHE PATH "Pinned cpp-httplib checkout")
set(ENABLE_SKYRIM_SE OFF CACHE BOOL "")
set(ENABLE_SKYRIM_VR OFF CACHE BOOL "")
set(BUILD_TESTS OFF CACHE BOOL "")
set(COMMONLIB_ENABLE_IPO OFF CACHE BOOL "")
set(SKSE_SUPPORT_PATCH_SAFETY OFF CACHE BOOL "")
add_subdirectory("${COMMONLIB_SOURCE}" commonlib)
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
target_include_directories(SkyrimDevTools PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
target_compile_definitions(SkyrimDevTools PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
target_link_libraries(SkyrimDevTools PRIVATE
    CommonLibSSE::CommonLibSSE SkyrimDevToolsServer minhook::minhook user32)
