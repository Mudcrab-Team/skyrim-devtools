// SPDX-License-Identifier: GPL-3.0-or-later
// COMMUNITY(alandtse/CommonLibSSE-NG@7c27a75da3eb0db080c1f1f63f6df1b7cd733c2a, GPL-3.0-or-later)
// Papyrus introspection: which script classes are loaded, what they declare, and calls into the
// running VM that answer with the function's return value. The call goes through the VM's own
// dispatch (the way a script or the console's CallFunction reaches a function), so latent
// functions, argument coercion and script states behave as they do in the game.
#include "state_common.h"

#include <algorithm>
#include <cctype>
#include <thread>

// windows.h renames Variable::GetObject.
#undef GetObject

namespace
{
    using namespace devtools;
    using namespace devtools::view;
    using namespace RE::BSScript;
    using RE::BSTSmartPointer;
    using RE::VMTypeID;
    using namespace std::chrono_literals;

    constexpr std::size_t maxArgs = 16;
    constexpr int maxResultDepth = 4;
    constexpr auto maxTimeout = 120000;

    std::string Lower(std::string text)
    {
        std::ranges::transform(text, text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        return text;
    }

    bool SameName(std::string_view a, std::string_view b)
    {
        return a.size() == b.size() && std::ranges::equal(a, b, [](unsigned char x, unsigned char y) { return std::tolower(x) == std::tolower(y); });
    }

    Internal::VirtualMachine& RequireVm()
    {
        const auto vm = Internal::VirtualMachine::GetSingleton();
        if (!vm || !vm->initialized) {
            throw RpcError(code::notReady, "The Papyrus VM is not running yet");
        }
        return *vm;
    }

    std::vector<std::string> ClassNames(Internal::VirtualMachine& vm)
    {
        std::vector<std::string> names;
        RE::BSSpinLockGuard lock(vm.typeInfoLock);
        for (auto& entry : vm.objectTypeMap) {
            if (entry.first.c_str() && *entry.first.c_str()) {
                names.emplace_back(entry.first.c_str());
            }
        }
        std::ranges::sort(names, [](const auto& a, const auto& b) { return Lower(a) < Lower(b); });
        return names;
    }

    BSTSmartPointer<ObjectTypeInfo> LoadedType(Internal::VirtualMachine& vm, const std::string& name)
    {
        BSTSmartPointer<ObjectTypeInfo> info;
        // Resolving may load the script from disk; that is what a call would do anyway.
        if (!vm.GetScriptObjectType(RE::BSFixedString(name), info) || !info) {
            throw RpcError(code::notFound, "No Papyrus script class named " + name);
        }
        return info;
    }

    // A class's functions followed by its parents'.
    template <class Visit>
    void ForEachFunction(ObjectTypeInfo* type, Visit&& visit)
    {
        for (; type; type = type->GetParent()) {
            auto* globals = type->GetGlobalFuncIter();
            for (std::uint32_t i = 0; globals && i < type->GetNumGlobalFuncs(); ++i) {
                if (globals[i].func) {
                    visit(globals[i].func.get(), type);
                }
            }
            auto* members = type->GetMemberFuncIter();
            for (std::uint32_t i = 0; members && i < type->GetNumMemberFuncs(); ++i) {
                if (members[i].func) {
                    visit(members[i].func.get(), type);
                }
            }
        }
    }

    IFunction* FindFunction(ObjectTypeInfo* type, const std::string& name, bool wantGlobal)
    {
        IFunction* found = nullptr;
        ForEachFunction(type, [&](IFunction* function, ObjectTypeInfo*) {
            if (!found && function->GetIsStatic() == wantGlobal && SameName(function->GetName().c_str(), name)) {
                found = function;
            }
        });
        return found;
    }

    Json DescribeFunction(IFunction* function, const ObjectTypeInfo* owner)
    {
        auto params = Json::array();
        for (std::uint32_t i = 0; i < function->GetParamCount(); ++i) {
            RE::BSFixedString name;
            TypeInfo type;
            function->GetParam(i, name, type);
            params.push_back(Json{ { "name", name.c_str() ? name.c_str() : "" }, { "type", type.TypeAsString() } });
        }
        return Json{ { "name", function->GetName().c_str() }, { "global", function->GetIsStatic() },
            { "native", function->GetIsNative() }, { "returns", function->GetReturnType().TypeAsString() },
            { "params", std::move(params) }, { "declaredIn", owner ? owner->GetName() : "" } };
    }

    RE::TESForm* ResolveForm(const Json& value, const char* key)
    {
        if (value.is_object()) {
            return RequireForm(value);
        }
        if (value.is_string()) {
            const auto text = value.get<std::string>();
            const auto digits = text.starts_with("0x") || text.starts_with("0X") ? text.substr(2) : text;
            const bool hex = !digits.empty() && digits.size() <= 8 && std::ranges::all_of(digits, [](unsigned char c) { return std::isxdigit(c) != 0; });
            const auto form = hex ? RE::TESForm::LookupByID(ParseFormId(value, key)) : FindByEditorId(text);
            if (!form) {
                throw RpcError(code::notFound, std::string("No form for '") + text + "'");
            }
            return form;
        }
        if (value.is_number_unsigned()) {
            if (const auto form = RE::TESForm::LookupByID(value.get<RE::FormID>())) {
                return form;
            }
        }
        throw RpcError(code::notFound, std::string("No form for ") + key);
    }

    // Writes one JSON value into a Papyrus variable of the declared type.
    void Fill(Internal::VirtualMachine& vm, Variable& out, const TypeInfo& type, const Json& value, const std::string& where)
    {
        const auto bad = [&](const char* expected) {
            throw RpcError(code::invalidParams, where + " expects " + expected + " (" + type.TypeAsString() + ")");
        };
        if (type.IsArray()) {
            if (value.is_null()) {
                out.SetNone();
                return;
            }
            if (!value.is_array() || value.size() > 128) {
                bad("an array of up to 128 values");
            }
            BSTSmartPointer<Array> array;
            if (!vm.CreateArray(type, static_cast<std::uint32_t>(value.size()), array) || !array) {
                throw RpcError(code::internal, "Could not create an array for " + where);
            }
            // An array type's raw value is its element type plus 10 for the primitives.
            const auto raw = static_cast<std::size_t>(type.GetRawType());
            TypeInfo element(static_cast<TypeInfo::RawType>(raw >= 10 && raw < 20 ? raw - 10 : raw));
            for (std::size_t i = 0; i < value.size(); ++i) {
                Fill(vm, (*array)[static_cast<std::uint32_t>(i)], element, value[i], where + "[" + std::to_string(i) + "]");
            }
            out.SetArray(array);
            return;
        }
        if (type.IsInt()) {
            if (value.is_boolean()) {
                out.SetSInt(value.get<bool>() ? 1 : 0);
            } else if (value.is_number()) {
                out.SetSInt(value.get<std::int32_t>());
            } else {
                bad("a number");
            }
        } else if (type.IsFloat()) {
            if (!value.is_number()) {
                bad("a number");
            }
            out.SetFloat(value.get<float>());
        } else if (type.IsBool()) {
            if (value.is_boolean()) {
                out.SetBool(value.get<bool>());
            } else if (value.is_number()) {
                out.SetBool(value.get<double>() != 0.0);
            } else {
                bad("true or false");
            }
        } else if (type.IsString()) {
            if (!value.is_string()) {
                bad("a string");
            }
            out.SetString(value.get<std::string>());
        } else if (type.IsObject()) {
            if (value.is_null()) {
                out.SetNone();
                return;
            }
            const auto form = ResolveForm(value, where.c_str());
            PackHandle(&out, form, static_cast<VMTypeID>(form->GetFormType()));
        } else {
            bad("a supported type");
        }
    }

    // Without a declared signature the JSON type decides.
    void FillLoose(Variable& out, const Json& value)
    {
        if (value.is_boolean()) {
            out.SetBool(value.get<bool>());
        } else if (value.is_number_integer()) {
            out.SetSInt(value.get<std::int32_t>());
        } else if (value.is_number()) {
            out.SetFloat(value.get<float>());
        } else if (value.is_string()) {
            out.SetString(value.get<std::string>());
        } else if (value.is_null()) {
            out.SetNone();
        } else {
            throw RpcError(code::invalidParams, "Arguments are numbers, booleans, strings, null or form references");
        }
    }

    class Args final : public IFunctionArguments
    {
    public:
        explicit Args(std::vector<std::function<void(Variable&)>> fillers) : fillers_(std::move(fillers)) {}

        bool operator()(RE::BSScrapArray<Variable>& dst) const override
        {
            dst.resize(fillers_.size());
            for (std::size_t i = 0; i < fillers_.size(); ++i) {
                fillers_[i](dst[i]);
            }
            return true;
        }

    private:
        std::vector<std::function<void(Variable&)>> fillers_;
    };

    Json Convert(const Variable& value, int depth)
    {
        if (value.IsNoneObject() || value.IsNoneArray()) {
            return nullptr;
        }
        if (value.IsArray()) {
            const auto array = value.GetArray();
            if (!array) {
                return nullptr;
            }
            auto out = Json::array();
            const auto size = array->size();
            for (std::uint32_t i = 0; i < size && i < 200; ++i) {
                out.push_back(depth > 0 ? Convert((*array)[i], depth - 1) : Json(nullptr));
            }
            return out;
        }
        if (value.IsObject()) {
            const auto object = value.GetObject();
            if (!object) {
                return nullptr;
            }
            Json out{ { "script", object->type ? object->type->GetName() : "" } };
            const auto handle = object->GetHandle();
            // A form handle carries the form ID in its low half.
            if (const auto form = RE::TESForm::LookupByID(static_cast<RE::FormID>(handle & 0xFFFFFFFFu)); form && handle) {
                out["formID"] = Hex(form->GetFormID());
                out["type"] = FormTypeName(form);
                out["name"] = Name(form->GetName());
                out["editorID"] = Name(form->GetFormEditorID());
            }
            return out;
        }
        if (value.IsInt()) {
            return value.GetSInt();
        }
        if (value.IsFloat()) {
            return view::Round(value.GetFloat(), 4);
        }
        if (value.IsBool()) {
            return value.GetBool();
        }
        if (value.IsString()) {
            return std::string(value.GetString());
        }
        return nullptr;
    }

    // Completes the reply with the function's return value, or continues into a second call.
    class Finish final : public IStackCallbackFunctor
    {
    public:
        explicit Finish(ReplyPtr reply, Json envelope) : reply_(std::move(reply)), envelope_(std::move(envelope)) {}

        void operator()(Variable result) override
        {
            try {
                envelope_["value"] = Convert(result, maxResultDepth);
                reply_->Ok(envelope_);
            } catch (const std::exception& error) {
                reply_->Fail(code::internal, error.what());
            }
        }

        void SetObject(const BSTSmartPointer<Object>&) override {}

    private:
        ReplyPtr reply_;
        Json envelope_;
    };

    std::string ClassFor(RE::TESForm* form)
    {
        if (form->As<RE::Actor>()) {
            return "Actor";
        }
        if (form->As<RE::TESObjectREFR>()) {
            return "ObjectReference";
        }
        if (form->As<RE::TESQuest>()) {
            return "Quest";
        }
        return "Form";
    }

    RE::BGSBaseAlias* FindAlias(const Json& spec)
    {
        const auto questForm = ResolveForm(spec.at("quest"), "alias.quest");
        const auto quest = questForm->As<RE::TESQuest>();
        if (!quest) {
            throw RpcError(code::invalidParams, "alias.quest is not a quest");
        }
        for (const auto alias : quest->aliases) {
            if (!alias) {
                continue;
            }
            if (spec.contains("id") && alias->aliasID == spec["id"].get<std::uint32_t>()) {
                return alias;
            }
            if (spec.contains("name") && SameName(alias->aliasName.c_str(), spec["name"].get<std::string>())) {
                return alias;
            }
        }
        throw RpcError(code::notFound, "The quest has no such alias (give id or name)");
    }
}

DEVTOOLS_MODULE(PapyrusCall)
{
    Register("Papyrus.classes", [](const Json& params) {
        AllowOnly(params, { "filter", "prefix", "offset", "limit", "detail" });
        auto& vm = RequireVm();
        const auto filter = Lower(Optional<std::string>(params, "filter", ""));
        const auto prefix = Lower(Optional<std::string>(params, "prefix", ""));
        const auto offset = Optional<std::size_t>(params, "offset", 0);
        const auto limit = std::clamp(Optional<std::size_t>(params, "limit", 100), std::size_t{ 1 }, std::size_t{ 500 });
        const auto detail = Optional<bool>(params, "detail", false);

        std::vector<std::string> matched;
        for (auto& name : ClassNames(vm)) {
            const auto lower = Lower(name);
            if ((filter.empty() || lower.find(filter) != std::string::npos) && (prefix.empty() || lower.starts_with(prefix))) {
                matched.push_back(std::move(name));
            }
        }
        auto classes = Json::array();
        for (std::size_t i = offset; i < matched.size() && classes.size() < limit; ++i) {
            if (!detail) {
                classes.push_back(matched[i]);
                continue;
            }
            Json entry{ { "name", matched[i] } };
            BSTSmartPointer<ObjectTypeInfo> info;
            if (vm.GetScriptObjectTypeNoLoad(RE::BSFixedString(matched[i]), info) && info) {
                const auto parent = info->GetParent();
                entry["parent"] = parent ? Json(parent->GetName()) : Json(nullptr);
                entry["globalFunctions"] = info->GetNumGlobalFuncs();
                entry["memberFunctions"] = info->GetNumMemberFuncs();
                entry["linked"] = info->IsLinked();
            }
            classes.push_back(std::move(entry));
        }
        return Json{ { "total", matched.size() }, { "offset", offset }, { "classes", std::move(classes) } };
    }, { .thread = Thread::Main, .timeout = 15s });

    Register("Papyrus.functions", [](const Json& params) {
        AllowOnly(params, { "class", "filter", "kind", "offset", "limit" });
        auto& vm = RequireVm();
        const auto className = Require<std::string>(params, "class");
        const auto filter = Lower(Optional<std::string>(params, "filter", ""));
        const auto kind = Optional<std::string>(params, "kind", "all");
        if (kind != "all" && kind != "global" && kind != "member") {
            throw RpcError(code::invalidParams, "kind is all, global or member");
        }
        const auto offset = Optional<std::size_t>(params, "offset", 0);
        const auto limit = std::clamp(Optional<std::size_t>(params, "limit", 100), std::size_t{ 1 }, std::size_t{ 500 });
        const auto info = LoadedType(vm, className);

        std::vector<std::pair<IFunction*, ObjectTypeInfo*>> matched;
        ForEachFunction(info.get(), [&](IFunction* function, ObjectTypeInfo* owner) {
            const bool global = function->GetIsStatic();
            if ((kind == "all" || (kind == "global") == global) &&
                (filter.empty() || Lower(function->GetName().c_str()).find(filter) != std::string::npos)) {
                matched.emplace_back(function, owner);
            }
        });
        auto functions = Json::array();
        for (std::size_t i = offset; i < matched.size() && functions.size() < limit; ++i) {
            functions.push_back(DescribeFunction(matched[i].first, matched[i].second));
        }
        return Json{ { "class", info->GetName() }, { "total", matched.size() }, { "offset", offset }, { "functions", std::move(functions) } };
    }, { .thread = Thread::Main, .timeout = 15s });

    // A global function (`class` + `function`), or a member function of a form or alias. `args` follow the
    // function's declared parameter types; a form argument is {formID} / {editorID} or a hex string.
    RegisterDeferred("Papyrus.call", [](const Json& params, ReplyPtr reply) {
        AllowOnly(params, { "class", "function", "args", "form", "alias", "timeoutMs", "loadEpoch" });
        RequirePlayable(params);
        auto& vm = RequireVm();
        const auto functionName = Require<std::string>(params, "function");
        const auto args = Optional<Json>(params, "args", Json::array());
        if (!args.is_array() || args.size() > maxArgs) {
            throw RpcError(code::invalidParams, "args is an array of up to 16 values");
        }
        const auto timeoutMs = Optional<int>(params, "timeoutMs", 10000);
        if (timeoutMs < 100 || timeoutMs > maxTimeout) {
            throw RpcError(code::invalidParams, "timeoutMs is 100 to 120000");
        }
        const bool hasForm = params.contains("form") && !params["form"].is_null();
        const bool hasAlias = params.contains("alias") && !params["alias"].is_null();
        if (hasForm && hasAlias) {
            throw RpcError(code::invalidParams, "Give form or alias, not both");
        }

        RE::TESForm* form = hasForm ? ResolveForm(params["form"], "form") : nullptr;
        RE::BGSBaseAlias* alias = hasAlias ? FindAlias(params["alias"]) : nullptr;
        const bool global = !form && !alias;
        std::string className = Optional<std::string>(params, "class", "");
        if (className.empty()) {
            if (global) {
                throw RpcError(code::invalidParams, "A global call needs class (the script name, for example Game)");
            }
            className = form ? ClassFor(form) : std::string(alias->GetTypeString().c_str());
        }

        const auto info = LoadedType(vm, className);
        const auto function = FindFunction(info.get(), functionName, global);
        std::vector<std::function<void(Variable&)>> fillers;
        Json signature = nullptr;
        if (function) {
            signature = DescribeFunction(function, nullptr);
            if (function->GetParamCount() != args.size()) {
                throw RpcError(code::invalidParams, functionName + " takes " + std::to_string(function->GetParamCount()) + " argument(s)", signature);
            }
            for (std::uint32_t i = 0; i < args.size(); ++i) {
                RE::BSFixedString paramName;
                TypeInfo type;
                function->GetParam(i, paramName, type);
                // Resolved here so a bad argument fails before anything is dispatched.
                Variable probe;
                Fill(vm, probe, type, args[i], std::string("argument ") + std::to_string(i) + " (" + (paramName.c_str() ? paramName.c_str() : "") + ")");
                fillers.push_back([&vm, type, value = args[i], i](Variable& out) {
                    Fill(vm, out, type, value, "argument " + std::to_string(i));
                });
            }
        } else if (global) {
            throw RpcError(code::notFound, className + " has no global function " + functionName);
        } else {
            // Attached scripts can expose functions the type walk does not list (states, natives bound late).
            for (const auto& arg : args) {
                fillers.push_back([value = arg](Variable& out) { FillLoose(out, value); });
            }
        }

        const auto policy = vm.GetObjectHandlePolicy();
        Json envelope{ { "class", className }, { "function", functionName }, { "global", global } };
        if (!signature.is_null()) {
            envelope["returns"] = signature["returns"];
        }
        BSTSmartPointer<IStackCallbackFunctor> callback(new Finish(reply, envelope));
        auto* arguments = new Args(std::move(fillers));
        bool dispatched = false;
        if (global) {
            dispatched = vm.DispatchStaticCall(RE::BSFixedString(className), RE::BSFixedString(functionName), arguments, callback);
        } else {
            const auto handle = form ? policy->GetHandleForObject(static_cast<VMTypeID>(form->GetFormType()), form)
                                     : policy->GetHandleForObject(alias->GetVMTypeID(), alias);
            if (handle == policy->EmptyHandle()) {
                delete arguments;
                throw RpcError(code::invalidParams, "The VM has no handle for that target (is it a loaded form?)");
            }
            dispatched = vm.DispatchMethodCall(handle, RE::BSFixedString(className), RE::BSFixedString(functionName), arguments, callback);
        }
        if (!dispatched) {
            throw RpcError(code::invalidParams, "The VM rejected the call to " + className + "." + functionName, signature);
        }

        // A latent function that never finishes would otherwise hold the reply until the bus deadline.
        std::thread([reply, timeoutMs] {
            std::this_thread::sleep_for(std::chrono::milliseconds(timeoutMs));
            reply->Fail(code::timeout, "The call was dispatched but has not returned (a latent function may still be running; its effects are not undone)");
        }).detach();
    }, { .thread = Thread::Main, .timeout = 125s });
}
