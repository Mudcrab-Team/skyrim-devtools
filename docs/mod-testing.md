# Testing a mod with skyrim-use

Two tools answer the questions a mod author asks after a play session: "what could this NPC say, and could the
player reach it?" and "what did my plugin actually exercise?". Both read the engine's loaded records and event
sources, so they work for mod-added NPCs, topics, quests and scenes exactly as for the base game.

## Dialogue

`dialogue_topics {npc}` lists every topic and info an NPC could say or offer. `npc` is a reference id or part of
the name of a loaded actor. For each topic: text, kind (player, scene, combat, favors, detection, service,
misc), owning quest (editor id, plugin, running) and its infos with the plugin that defines them, `passes`
(quest and info conditions evaluated by the engine against this NPC and the player, `failing` holding the
condition function indexes that were false), `said` and the first response line. Only lines addressed to the
NPC are listed (the info or its quest names the NPC, race, faction, class or voice type, and that matches);
`include_generic` adds lines with no speaker condition. Narrow it with `plugins`, `quest` and `kinds`.

`dialogue_explore {npc, strategy}` talks to the NPC and tries every branch. `breadth` takes all topics of a list
before going deeper, `unseen-first` takes never-said topics first and follows a conversation down. A plain save
(`explore_<ref>`) is written before the first line. After a branch the explorer compares the result with the
parent list: a quest stage or inventory event, or a list that mostly repeats the parent's but differs, means the
choice changed the world, and the save is loaded again before the next sibling so mutually exclusive choices
(accept or refuse) are all taken. The report holds the tree traversed with the lines heard, every info heard,
branches left by the budget (`max_branches`, `time_budget_s`) and `unreachable`: lines addressed to the NPC
that were never heard, split into those blocked by conditions at the start (with the failing function
indexes) and those whose conditions pass but were not reached, usually shadowed by an earlier info of the same
topic. A markdown summary is returned and both files are written to `~/.skyrim-use/coverage/`.

Limits: replay is by topic text; a topic whose text changes between visits is reported as unreachable by
replay. A conversation that opens a barter, training or other menu ends that branch. The player must stand near
a loaded NPC (`Dialogue.start` activates it). Reaching an info that needs another quest stage first takes a save
in that state.

## Coverage

`coverage_start {plugins}` begins a session filtered to mod file names (empty means every plugin).
`coverage_report {limit, stop}` returns JSON plus a markdown summary and writes both under
`~/.skyrim-use/coverage/`:

* quest stages set (quest, stage, seconds since start), against the stages the loaded quests of the plugin hold;
* dialogue infos said (the watcher notices each info an NPC starts, and the engine's said flag catches the rest),
  against every info the plugin defines;
* scenes started (a poll of `isPlaying`; the library has no start event) and quests started;
* Papyrus warnings and errors with their script and plugin, plus the scripts that wrote any log line, and the
  delta of the engine events script sinks see (`GameEvents.counts`).

A Papyrus line is attributed to a plugin through the form id the VM prints next to the script name; a line
without one is kept and has no plugin. A script that ran without logging is not observable, and a stage's
`defined` count is what the runtime keeps for the quest plus stages set this session, so treat covered/defined
for stages as a lower bound on what exists. Papyrus logging is already switched on in the isolated profile.

`papyrusError {severity, text, script, form}` is a native event pushed for every warning, error or fatal log
line (subscribe to it like any event). While a coverage session runs, the server copies those events into the
timeline as `{"t":"papyrusError", "ms", ...}` lines, so the alerts layer and the video panel can show them.

## A session

~~~text
game_start {save: "MyModFixture"}
coverage_start {plugins: ["MyMod.esp"]}
... play ...
dialogue_topics {npc: "Innkeeper", plugins: ["MyMod.esp"]}
dialogue_explore {npc: "Innkeeper"}
coverage_report {stop: true}
~~~
