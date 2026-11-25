# Options that decide CommonLibSSE-NG's ABI. The plugin build and the shared prebuilt library
# (tools/pc-build commonlib) both read this file so the two can never drift apart.
set(ENABLE_SKYRIM_SE OFF CACHE BOOL "")
set(ENABLE_SKYRIM_VR OFF CACHE BOOL "")
set(BUILD_TESTS OFF CACHE BOOL "")
set(COMMONLIB_ENABLE_IPO OFF CACHE BOOL "")
set(SKSE_SUPPORT_PATCH_SAFETY OFF CACHE BOOL "")
