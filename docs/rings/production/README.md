# Personal production ring

Production is promoted, not composed: `personal-promote.yml` points an
immutable `production-YYYYMMDD[-rerunN]` tag at a soaked staging nightly's
own commit and copies that nightly's artifacts and image digests. The old
`personal-production.yml` compose is retired.

To verify a completed promotion, replace `PIN` with the production tag:

```sh
gh release download PIN -R btli/omnigent -p source.json -O -
git fetch origin --tags
git rev-parse PIN^{commit} "$(gh release download PIN -R btli/omnigent -p source.json -O - | jq -r .source_tag)^{commit}"
gh release download production-latest -R btli/omnigent -p source.json -O - | jq -r .production_tag
```

Expect both `rev-parse` lines to print the same sha, `source.json` to name the
nightly, its sha and trigger, and `production-latest` to name `PIN`. Each
`omnigent-production-*` asset's sha256 in the release's `SHA256SUMS` equals
the nightly's `omnigent-staging-*` entry in its `build-complete.json`.

Main sync and its conflict recovery now belong to the staging nightly; see the
[staging ring README](../staging/README.md). For promote rules, migration
approval, rollback and the hardware contract, see the
[composer README](../../../.github/scripts/personal-staging/README.md).
