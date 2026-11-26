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
# The extension header is public API; a synced build tree carries it as ext/ next to the sources.
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/ext")
    set(DEVTOOLS_EXT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/ext")
    set(DEVTOOLS_EXAMPLES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/examples")
else()
    set(DEVTOOLS_EXT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../ext")
    set(DEVTOOLS_EXAMPLES_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../examples")
endif()
target_include_directories(SkyrimDevTools PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}" "${DEVTOOLS_EXT_DIR}")
target_compile_definitions(SkyrimDevTools PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
target_link_libraries(SkyrimDevTools PRIVATE
    CommonLibSSE::CommonLibSSE SkyrimDevToolsServer minhook::minhook user32)
# Game headers dominate every module's compile time, so they are parsed once per build.
target_precompile_headers(SkyrimDevTools PRIVATE pch.h)

# The example extension plugin shows the extension API end to end; it loads next to the main plugin.
if(EXISTS "${DEVTOOLS_EXAMPLES_DIR}/hello-ext/hello_ext.cpp")
    add_library(SkyrimDevToolsExample SHARED "${DEVTOOLS_EXAMPLES_DIR}/hello-ext/hello_ext.cpp")
    target_compile_features(SkyrimDevToolsExample PRIVATE cxx_std_23)
    target_include_directories(SkyrimDevToolsExample PRIVATE "${DEVTOOLS_EXT_DIR}")
    target_compile_definitions(SkyrimDevToolsExample PRIVATE NOMINMAX WIN32_LEAN_AND_MEAN)
    target_link_libraries(SkyrimDevToolsExample PRIVATE CommonLibSSE::CommonLibSSE)
endif()
