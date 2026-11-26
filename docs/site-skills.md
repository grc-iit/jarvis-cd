# Site skills (`jarvis rg skill`)

A site skill is one Markdown document about a site: how its queues, MPI,
network or filesystems behave, and what to do about it. JARVIS keeps the
skills it is given in its site home, so every JARVIS root that shares the
home shares its skills. With a site graph (`JARVIS_RESOURCE_GRAPH`) the home
is `JARVIS_SITE_HOME`, else `~/.ppi-jarvis/site`; without one it is the
JARVIS root (see
[resource_graph.md](resource_graph.md#site-level-graph-jarvis_resource_graph)).
CLIO, or a person, reads them before planning work on the site.

```
<site home>/skills/<name>.md    the skill
<site home>/skills/index.json   jarvis.site-skills.v1
<site home>/skills/.lock        serializes writes
```

A site may also publish skills beside its graph, in
`<dir of JARVIS_RESOURCE_GRAPH>/skills/` (same layout). JARVIS lists and
shows them with `source: site` and never writes there. A skill of JARVIS's
own with the same name wins.

| Command | Purpose |
|---------|---------|
| `jarvis rg skill list [+json]` | List the skills |
| `jarvis rg skill show NAME [+json]` | Print one skill |
| `jarvis rg skill set NAME (--file F \| --file - \| --text T) [--description D] [--set_by WHO] [+json]` | Create or replace a skill |
| `jarvis rg skill rm NAME [+json]` | Remove a skill |

A name matches `[a-z0-9][a-z0-9_.-]*` and is at most 64 characters. A skill
is at most 256 KiB. `set` writes the document and the index atomically under
a flock on `.lock`, so concurrent writers never tear either one. Every
command is a file read or write, light enough for a login node. `+json`
prints one JSON line.

```bash
jarvis rg skill set ares-mpi --file ares-mpi.md \
    --description "Ares MPI: site Open MPI 5.0.5 has no Slurm support" +json
jarvis rg skill list +json
jarvis rg skill show ares-mpi
```

## Documents

`list` returns `jarvis.site-skills.v1`:

| Field | Meaning |
|-------|---------|
| `graph` | JARVIS's own graph file |
| `site_graph` | `true` when `JARVIS_RESOURCE_GRAPH` names a site graph |
| `site_graph_file` | the site's graph (read-only), or `null` |
| `directory` | JARVIS's skills directory (the one `set` and `rm` write) |
| `published_directory` | the skills the site publishes beside its graph, or `null` |
| `skills[]` | one entry per skill: `name`, `path`, `present` (the `.md` file exists), `source` (`site` or `jarvis`), `description`, `sha256`, `bytes`, `updated_at`, `updated_by` |

A `.md` file that someone copied into the directory by hand is listed too,
with `null` metadata.

`show` returns `jarvis.site-skill.v1`: the same entry plus `content` and
`sha256_actual`, the digest of the file as it is now. When `sha256_actual`
differs from `sha256`, the file was edited outside JARVIS. Without `+json`,
`show` prints only the Markdown.

`set` returns the entry plus `action: "set"` and `previous_sha256` (the
digest it replaced, or `null`). A `--description` is kept across later sets
that omit it. `--set_by` defaults to `$USER`.

`rm` returns `{name, action, directory}`, where `action` is `removed` or
`absent`. Removing a missing skill is not an error. When the site publishes
a skill with the same name, the report names it under
`published_still_applies`.

[`jarvis sched info +json`](sched.md) lists the skills (name, description,
path and sha256) next to the scheduler and network facts.

## Refusals

A refusal exits `5` with a `jarvis.issue.v1` (`subject: site skill`):

| `kind` | `field` | When |
|--------|---------|------|
| `skill_name_invalid` | `skill name` | the name does not match the pattern |
| `skill_not_found` | `skill name` | `show` of a skill that does not exist |
| `skill_too_large` | `skill content` | the document is over 256 KiB |
| `skill_published` | `skill name` | `rm` of a skill only the site publishes, which JARVIS only reads |
| `skill_content_missing` | `--file` | `set` got neither, or both, of `--file` and `--text` |
| `jarvis_not_initialized` | `JARVIS_ROOT` | the root is not initialized and `JARVIS_RESOURCE_GRAPH` is unset, so there is no graph directory. With `JARVIS_RESOURCE_GRAPH` set, `rg skill` works on a fresh root |
