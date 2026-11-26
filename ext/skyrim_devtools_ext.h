/* SPDX-License-Identifier: MIT
 *
 * Extension API of the SkyrimDevTools plugin: lets another SKSE plugin publish its own test methods and
 * events on the playtesting bus. A method registered here appears to the agent as an MCP tool with the JSON
 * Schema given at registration; an event emitted here reaches the agent's event stream.
 *
 * The header is plain C so any compiler can use it, and it is versioned: a plugin asks for the API version it
 * was built against and the host answers only if it can serve it. Every structure starts with its own size so
 * later versions can append fields without breaking older plugins.
 *
 * Getting the API (either way works; both return the same table):
 *   1. Export: GetProcAddress(GetModuleHandleA("SkyrimDevTools.dll"), "SkyrimDevTools_GetApi"), call it with
 *      SKYRIM_DEVTOOLS_API_VERSION. NULL means the host is absent or older than the requested version.
 *   2. SKSE messaging: the host broadcasts SKYRIM_DEVTOOLS_MESSAGE_API after every plugin has loaded
 *      (kPostPostLoad); register a listener for the sender "SkyrimDevTools" and read the message data as a
 *      const SkyrimDevtoolsApi*.
 *
 * Threads: a handler runs on the thread named in its registration. SKYRIM_DEVTOOLS_THREAD_ANY is the
 * connection thread and must not touch game state; use MAIN for anything that does. A handler may answer
 * later from any thread (see the reply functions), within its timeout.
 */
#ifndef SKYRIM_DEVTOOLS_EXT_H
#define SKYRIM_DEVTOOLS_EXT_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SKYRIM_DEVTOOLS_API_VERSION 1u
/* 'SDE1': message type of the API broadcast. */
#define SKYRIM_DEVTOOLS_MESSAGE_API 0x53444531u

enum
{
    SKYRIM_DEVTOOLS_THREAD_ANY = 0,
    SKYRIM_DEVTOOLS_THREAD_MAIN = 1,
    SKYRIM_DEVTOOLS_THREAD_UI = 2
};

/* Return values of the registration and emit functions. */
enum
{
    SKYRIM_DEVTOOLS_OK = 0,
    SKYRIM_DEVTOOLS_ERR_ARGUMENT = -1,   /* null, empty or malformed argument, or a name that is not allowed */
    SKYRIM_DEVTOOLS_ERR_SCHEMA = -2,     /* schema_json or data_json is not valid JSON */
    SKYRIM_DEVTOOLS_ERR_CONFLICT = -3,   /* the name is a built-in method or belongs to another plugin */
    SKYRIM_DEVTOOLS_ERR_UNKNOWN = -4     /* unregister or emit of something that was never registered or declared */
};

/* A pending call. Pass it back to exactly one of the reply functions; a second answer is ignored. */
typedef struct SkyrimDevtoolsReply_* SkyrimDevtoolsReply;

/* params_json is a JSON object, valid only until the handler returns: copy it before answering later. */
typedef void (*SkyrimDevtoolsHandler)(const char* params_json, SkyrimDevtoolsReply reply, void* user);

typedef struct SkyrimDevtoolsMethod
{
    uint32_t size;               /* sizeof(SkyrimDevtoolsMethod) */
    const char* name;            /* "Plugin.method": [A-Za-z0-9_.], at most 64 chars, one dot at least */
    const char* description;     /* shown to the agent; say what it does and what it returns */
    const char* schema_json;     /* JSON Schema of the params object, or NULL to accept anything */
    uint32_t thread;             /* SKYRIM_DEVTOOLS_THREAD_* */
    uint32_t timeout_ms;         /* 0 means the default of 5000 */
    SkyrimDevtoolsHandler handler;
    void* user;                  /* passed back to the handler */
} SkyrimDevtoolsMethod;

typedef struct SkyrimDevtoolsApi
{
    uint32_t size;               /* sizeof(SkyrimDevtoolsApi) of the host */
    uint32_t version;            /* SKYRIM_DEVTOOLS_API_VERSION the table implements */

    /* owner is your plugin's name; it tags the method in the method list and is needed to unregister it.
     * Registering the same name again as the same owner replaces the method. */
    int (*register_method)(const char* owner, const SkyrimDevtoolsMethod* method);
    int (*unregister_method)(const char* owner, const char* name);

    /* Answers a call with a JSON value, or with an error (code is yours; use values above -32000). */
    void (*reply_ok)(SkyrimDevtoolsReply reply, const char* result_json);
    void (*reply_fail)(SkyrimDevtoolsReply reply, int code, const char* message);

    /* An event type ("Plugin.event", same character rules as a method name) must be declared once before it is
     * emitted; it is delivered only while the agent is subscribed to it. Emitting from any thread is cheap and
     * never blocks on the game. */
    int (*declare_event)(const char* owner, const char* type, const char* description);
    int (*emit_event)(const char* type, const char* data_json);
} SkyrimDevtoolsApi;

typedef const SkyrimDevtoolsApi* (*SkyrimDevtoolsGetApiFn)(uint32_t version);

#ifdef __cplusplus
}
#endif

#endif
