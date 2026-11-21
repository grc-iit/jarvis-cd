# Site skills (`jarvis rg skill`)

A site skill is one Markdown document about a site: how its queues, MPI,
network or filesystems behave, and what to do about it. JARVIS keeps skills
next to the resource graph, so every JARVIS root that shares the site graph
(`JARVIS_RESOURCE_GRAPH`, see
[resource_graph.md](resource_graph.md#site-level-graph)) shares its skills.
CLIO, or a person, reads them before planning work on the site.

```
<dir of the resource graph>/skills/<name>.md    the skill
<dir of the resource graph>/skills/index.json   jarvis.site-skills.v1
<dir of the resource graph>/skills/.lock        serializes writes
```

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
| `graph` | the resource graph file |
| `site_graph` | `true` when the graph is the site graph (`JARVIS_RESOURCE_GRAPH`) rather than a per-root one |
| `directory` | the skills directory |
| `skills[]` | one entry per skill: `name`, `path`, `present` (the `.md` file exists), `description`, `sha256`, `bytes`, `updated_at`, `updated_by` |

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
`absent`. Removing a missing skill is not an error.

[`jarvis sched info +json`](sched.md) lists the skills (name, description,
path and sha256) next to the scheduler and network facts.

## Refusals

A refusal exits `5` with a `jarvis.issue.v1` (`subject: site skill`):

| `kind` | `field` | When |
|--------|---------|------|
| `skill_name_invalid` | `skill name` | the name does not match the pattern |
| `skill_not_found` | `skill name` | `show` of a skill that does not exist |
| `skill_too_large` | `skill content` | the document is over 256 KiB |
| `skill_content_missing` | `--file` | `set` got neither, or both, of `--file` and `--text` |
