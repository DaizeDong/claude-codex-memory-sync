# Catalog integration

`profile_catalog` is the startup adapter for the shared `skill_smith.catalog`
API. It selects the existing profile roots and external manifest default, then
supplies explicit paths, clients, scopes and a skill traversal depth of eight.
The producer does not search for checkout directories or choose a cache version.
`CLAUDE_CONFIG_REPO` continues to select the default external manifest directory.

`build_plan` discovers once. Skills, native agent planning and inventory consume
that snapshot directly. A context-local snapshot also serves the unchanged config
adapter's `plugin_catalog` compatibility call. The context is reset on exit and
does not act as a persistent cache. Standalone compatibility APIs may discover
their own snapshot, or accept the caller's snapshot explicitly.

The plan's `catalog` and `inventory.catalog` contain the complete schema-one
envelope. Unsupported schema versions stop adaptation. JSON reports include
source descriptions and operational paths and belong in private profile storage.
Compatibility rows retain `catalog_record` and `catalog_entrypoint`
alongside historical fields. The agent diagnostic API omits descriptions to
preserve its existing prose-free contract; its identity, entrypoint coordinates,
version, hashes, scope, client, marketplace and status observations remain present.
The complete description is still in the plan's catalog envelope. Ownership files
store stable source and entrypoint identity rather than observation timestamps,
so read-only discovery does not make an unchanged profile need rewriting.

Authentication observations belong to individual MCP bindings or App connectors.
Filesystem presence does not establish authentication, runtime discovery or
compatibility. Optional `private_bindings` and `runtime_discovery` paths can be
supplied to `discover_profile`; missing observations remain unknown.

External source resolution, installed aliases, frontmatter names and the
external-installer exception are producer responsibilities. Inventory may attach
Git and executable observations, but does not parse another source manifest or
recover names itself. Name matches are candidate suggestions. Link repair requires
an exact source path from existing ownership metadata, and generic link deployment
does not take over an external installer. An owned installation name does not
authorize switching that artifact to an unrelated source with the same name.

Explicit disablement can retire an intact owned plugin artifact. Missing versions,
unknown enablement and unreadable plugin metadata preserve artifacts. The existing
hash, user-edit, destination baseline, lock and rollback checks remain in place.
Source role frontmatter is still validated while rendering to enforce existing
execution restrictions; that validation does not discover source identity or
qualify plugin names. Role execution and model routing are unchanged by T07.

## Producer prerequisite

The dependency is pinned to the proposed `skill-smith==0.1.4` consumer interface.
The installed 0.1.3 catalog needs the staged additive T07 producer changes before
this integration can run. `discover_profile` requires
the producer capability names `workflow_roots`, `plugin_descriptors`, and
`plugin_metadata_paths`, and fails visibly when they are missing.

The required additions are typed user command/agent roots, catalog-backed legacy
explicit plugin selection, plugin metadata paths, declared frontmatter names on
entrypoints, and settings-only plugin observations. They preserve schema version
one and all existing status dimensions. The proposal and before hashes are kept
in the ignored T07 producer candidate directory for the producer owner to review.
SYNC does not load that directory in production and contains no fallback parser.

After the producer owner accepts the interface and its version, run:

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_profile_catalog.py tests/test_profile_inventory.py tests/test_profile_agents.py tests/test_profile_sync.py -q -p no:cacheprovider
..\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

The implementation report distinguishes worker scratch validation against the
staged producer candidate from normal tests against the installed producer.
Live inventory comparison is a separate read-only acceptance step. T08 snapshot
extraction, T09 memory migration and T10 role/overlay replacement remain separate.
