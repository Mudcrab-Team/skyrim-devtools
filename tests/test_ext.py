import asyncio
import json
import unittest

from skyrim_devtools.use import ext
from skyrim_devtools.use.options import split_options
import test_use
from test_use import FakeSocket, Harness, run

ECHO = {"name": "HelloExt.echo", "thread": "any", "timeoutMs": 5000, "owner": "HelloExt",
        "description": "Echo the text back.",
        "schema": {"type": "object", "properties": {"text": {"type": "string", "description": "what to echo"},
                                                      "times": {"type": "integer"}},
                   "required": ["text"], "additionalProperties": False}}
ODD = {"name": "Odd.set", "thread": "main", "timeoutMs": 1000, "owner": "Odd",
       "schema": {"type": "object", "properties": {"from": {"type": "string"}}}}
NOSCHEMA = {"name": "Free.run", "thread": "main", "timeoutMs": 1000, "owner": "Free", "description": "Runs."}


class WithExtMethods(unittest.TestCase):
    def setUp(self):
        FakeSocket.default_extra = [ECHO, ODD, NOSCHEMA]

    def tearDown(self):
        FakeSocket.default_extra = []


class CheckArgsTests(unittest.TestCase):
    schema = ECHO["schema"]

    def test_unknown_key_warns_and_is_dropped(self):
        clean, warnings = ext.check_args(self.schema, {"text": "a", "txt": "b"}, label="tool x")
        self.assertEqual(clean, {"text": "a"})
        self.assertEqual(warnings[0]["unknown"], ["txt"])
        self.assertEqual(warnings[0]["accepted"], ["text", "times"])
        self.assertIn("accepted keys: text, times", warnings[0]["message"])

    def test_strict_mode_is_an_error(self):
        with self.assertRaises(ext.ArgumentError) as caught:
            ext.check_args(self.schema, {"txt": "b"}, strict=True)
        self.assertIn("text, times", str(caught.exception))

    def test_schema_without_properties_accepts_anything(self):
        self.assertEqual(ext.check_args({"type": "object"}, {"a": 1})[1], [])
        self.assertEqual(ext.check_args(None, {"a": 1})[1], [])

    def test_open_schema_accepts_extra_keys(self):
        schema = {"properties": {"a": {}}, "additionalProperties": True}
        self.assertEqual(ext.check_args(schema, {"b": 1})[1], [])

    def test_attach_to_json_object_and_to_plain_text(self):
        warning = [{"code": "unknownArguments"}]
        self.assertEqual(json.loads(ext.attach_warnings('{"a":1}', warning)), {"a": 1, "warnings": warning})
        self.assertTrue(ext.attach_warnings("hello", warning).startswith("hello\nwarnings: "))
        self.assertEqual(ext.attach_warnings("x", []), "x")

    def test_tool_names(self):
        self.assertEqual(ext.snake("HelloExt.playerName"), "hello_ext_player_name")
        self.assertEqual(ext.tool_name("Player.state", {"player_state"}), "ext_player_state")

    def test_option_keys_are_checked(self):
        self.assertEqual(split_options({"alwaysActive": True, "strictArgs": True}),
                         ({"alwaysActive": True}, {"strictArgs": True}))
        with self.assertRaises(ValueError):
            split_options({"alwaysActiv": True})


class ExtToolTests(WithExtMethods):
    def test_registered_methods_become_tools_with_their_schema(self):
        async def scenario():
            h = Harness()
            before = {t.name for t in await h.server.list_tools()}
            self.assertNotIn("hello_ext_echo", before)
            await h.text("game_start")
            tools = {t.name: t for t in await h.server.list_tools()}
            echo = tools["hello_ext_echo"]
            self.assertEqual(echo.inputSchema["required"], ["text"])
            self.assertEqual(echo.inputSchema["properties"]["text"]["description"], "what to echo")
            self.assertIn("HelloExt.echo", echo.description)
            self.assertIn("odd_set", tools)  # a property named `from` falls back to one `arguments` object
            self.assertIn("free_run", tools)
            await h.text("game_stop")
            self.assertNotIn("hello_ext_echo", {t.name for t in await h.server.list_tools()})
        run(scenario())

    def test_tool_calls_the_method(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["HelloExt.echo"] = {"received": "hi"}
            reply = json.loads(await h.text("hello_ext_echo", text="hi"))
            self.assertEqual(reply, {"received": "hi"})
            self.assertEqual(h.sockets[0].calls[-1], ("HelloExt.echo", {"text": "hi"}))
            h.sockets[0].results["Odd.set"] = {"ok": True}
            await h.text("odd_set", arguments={"from": "x"})
            self.assertEqual(h.sockets[0].calls[-1], ("Odd.set", {"from": "x"}))
            await h.text("game_stop")
        run(scenario())

    def test_unknown_argument_warns_naming_accepted_keys(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["HelloExt.echo"] = {"received": "hi"}
            reply = json.loads(await h.text("hello_ext_echo", text="hi", txt="typo"))
            self.assertEqual(reply["received"], "hi")
            warning = reply["warnings"][0]
            self.assertEqual(warning["unknown"], ["txt"])
            self.assertEqual(warning["accepted"], ["text", "times"])
            self.assertEqual(h.sockets[0].calls[-1][1], {"text": "hi"})
            await h.text("game_stop")
        run(scenario())

    def test_curated_tools_are_checked_too(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            content = await h.tool("list_menus", open_only=True, open_onyl=True)
            text = "\n".join(part.text for part in content)
            line = next(line for line in text.splitlines() if line.startswith("warnings: "))
            warning = json.loads(line[len("warnings: "):])[0]
            self.assertEqual(warning["unknown"], ["open_onyl"])
            self.assertIn("open_only", warning["accepted"])
            await h.text("game_stop")
        run(scenario())

    def test_strict_mode_refuses(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            await h.text("game_options", changes={"strictArgs": True})
            with self.assertRaises(Exception) as caught:
                await h.text("hello_ext_echo", text="hi", txt="typo")
            self.assertIn("accepted keys: text, times", str(caught.exception))
            await h.text("game_options", changes={"strictArgs": False})
            await h.text("hello_ext_echo", text="hi", txt="typo")
            await h.text("game_stop")
        run(scenario())

    def test_rpc_call_checks_params_against_the_method_schema(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["HelloExt.echo"] = {"received": "hi"}
            reply = json.loads(await h.text("rpc_call", method="HelloExt.echo", params={"text": "hi", "tx": 1}))
            self.assertEqual(reply["warnings"][0]["unknown"], ["tx"])
            self.assertEqual(h.sockets[0].calls[-1][1], {"text": "hi"})
            await h.text("game_stop")
        run(scenario())

    def test_methods_changed_event_refreshes_the_tools(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            socket = h.sockets[0]
            socket.extra_methods.append({"name": "Late.ping", "thread": "any", "timeoutMs": 1000, "owner": "Late",
                                         "description": "Added after connect.", "schema": {"type": "object", "properties": {}}})
            socket.push({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "methodsChanged", "seq": 1, "epoch": 0, "data": {"added": ["Late.ping"]}}})
            for _ in range(50):
                if "late_ping" in {t.name for t in await h.server.list_tools()}:
                    break
                await asyncio.sleep(0.05)
            self.assertIn("late_ping", {t.name for t in await h.server.list_tools()})
            socket.extra_methods.pop()
            socket.push({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "methodsChanged", "seq": 2, "epoch": 0, "data": {"removed": ["Late.ping"]}}})
            for _ in range(50):
                if "late_ping" not in {t.name for t in await h.server.list_tools()}:
                    break
                await asyncio.sleep(0.05)
            self.assertNotIn("late_ping", {t.name for t in await h.server.list_tools()})
            await h.text("game_stop")
        run(scenario())

    def test_method_named_like_a_curated_tool_gets_a_prefix(self):
        FakeSocket.default_extra = [dict(ECHO, name="Snapshot.take", owner="Snap")]

        async def scenario():
            h = Harness()
            await h.text("game_start")
            names = {t.name for t in await h.server.list_tools()}
            self.assertIn("snapshot", names)
            self.assertIn("snapshot_take", names)
            await h.text("game_stop")
        run(scenario())


class OptionTests(unittest.TestCase):
    def test_config_options_reach_the_plugin_at_start(self):
        async def scenario():
            h = Harness()
            h.manager.config["options"] = {}
            h.manager.option_state.update({"alwaysActive": True, "autoDismissMenus": ["TutorialMenu"]})
            old = test_use.METHODS
            test_use.METHODS = old + ["Options.set", "Options.get"]
            try:
                await h.text("game_start")
            finally:
                test_use.METHODS = old
            calls = [c for c in h.sockets[0].calls if c[0] == "Options.set"]
            self.assertEqual(calls, [("Options.set", {"alwaysActive": True, "autoDismissMenus": ["TutorialMenu"]})])
            await h.text("game_stop")
        run(scenario())

    def test_live_change_applies_and_a_bad_key_is_refused(self):
        async def scenario():
            h = Harness()
            old = test_use.METHODS
            test_use.METHODS = old + ["Options.set", "Options.get"]
            try:
                await h.text("game_start")
            finally:
                test_use.METHODS = old
            h.sockets[0].results["Options.set"] = {"cleanTransition": True}
            reply = json.loads(await h.text("game_options", changes={"cleanTransition": True, "cleanTransitionCell": "QASmoke"}))
            self.assertEqual(h.sockets[0].calls[-1], ("Options.set", {"cleanTransition": True, "cleanTransitionCell": "QASmoke"}))
            self.assertTrue(reply["options"]["cleanTransition"])
            with self.assertRaises(Exception) as caught:
                await h.text("game_options", changes={"alwaysActiv": True})
            self.assertIn("unknown option", str(caught.exception))
            with self.assertRaises(Exception):
                await h.text("game_options", changes={"strictArgs": "yes"})
            await h.text("game_stop")
        run(scenario())

    def test_vanity_camera_option_can_be_turned_back_on(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].results["Setting.set"] = {"previous": 120.0, "value": 86400.0}
            await h.text("game_options", changes={"disableVanityCamera": False})
            self.assertEqual(h.sockets[0].calls[-1][0], "Setting.set")
            await h.text("game_stop")
        run(scenario())

    def test_options_without_a_game_are_kept_for_the_next_start(self):
        async def scenario():
            h = Harness()
            reply = json.loads(await h.text("game_options", changes={"alwaysActive": True}))
            self.assertIn("no game is running", reply["note"])
            self.assertTrue(h.manager.option_state["alwaysActive"])
        run(scenario())

    def test_stall_event_shows_in_status(self):
        async def scenario():
            h = Harness()
            await h.text("game_start")
            h.sockets[0].push({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "mainThreadStalled", "seq": 1, "epoch": 0, "data": {"stalledMs": 7000, "thresholdMs": 6000}}})
            await asyncio.sleep(0.1)
            status = json.loads(await h.text("game_status"))
            self.assertEqual(status["mainThreadStalled"]["stalledMs"], 7000)
            self.assertIn("main thread", status["warning"])
            h.sockets[0].push({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "mainThreadResumed", "seq": 2, "epoch": 0, "data": {"stalledMs": 9000}}})
            await asyncio.sleep(0.1)
            self.assertNotIn("mainThreadStalled", json.loads(await h.text("game_status")))
            await h.text("game_stop")
        run(scenario())


if __name__ == "__main__":
    unittest.main()
