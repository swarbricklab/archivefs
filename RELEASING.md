# Releasing archivefs

How to cut a release and register it on **PyPI** and **conda-forge**. The steps that
need an account or a web console — the ones a maintainer must do by hand — are marked
**(manual)**.

## Versioning

The version comes from the **git tag** via `setuptools-scm` — there is no version string
to edit. Tag `vX.Y.Z` and the build is `X.Y.Z`. A build from an untagged commit gets a
dev version, which PyPI will not accept, so **always release from a tag**.

## 1. Pre-flight (every release)

```bash
pytest -m "not network and not credentials"
ruff check .
# Build and validate the distributions exactly as CI will:
python -m build
python -m twine check --strict dist/*
```

`twine check --strict` must pass — it catches a malformed README, a bad license
expression, or missing metadata before PyPI ever sees it.

## 2. First PyPI release — set up Trusted Publishing (manual, once)

Trusted Publishing (OIDC) means no API token is ever stored. Set it up once:

1. **(manual)** Create the project on PyPI. Either do a first manual upload
   (`twine upload dist/*` with a temporary token) *or* register a **pending publisher**
   at <https://pypi.org/manage/account/publishing/> so the very first upload is OIDC.
2. **(manual)** On PyPI → the `archivefs` project → *Publishing* → add a trusted publisher:
   - Owner: `swarbricklab` · Repository: `archivefs`
   - Workflow: `publish.yml` · Environment: `pypi`
3. **(manual)** In GitHub → *Settings → Environments*, create an environment named `pypi`.
4. In [`.github/workflows/publish.yml`](.github/workflows/publish.yml), uncomment the
   `release:` trigger. Thereafter a published GitHub Release publishes automatically;
   `workflow_dispatch` stays for manual re-runs.

## 3. Subsequent releases

1. Update `CHANGELOG.md` (move `[Unreleased]` to the new version).
2. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. **(manual)** Publish a GitHub Release for the tag → `publish.yml` builds, runs
   `twine check`, and uploads via Trusted Publishing.

## 4. conda-forge (after the PyPI release)

conda-forge builds from the **PyPI sdist**, so it comes *after* step 2/3.

1. Get the sdist's sha256: `pip download archivefs==X.Y.Z --no-deps --no-binary :all:`
   then `sha256sum archivefs-X.Y.Z.tar.gz` (or copy it from the PyPI *Download files* page,
   or let `grayskull pypi archivefs` generate the whole recipe).
2. Update [`conda-recipe/meta.yaml`](conda-recipe/meta.yaml): set `version` and the real
   `sha256`.
3. **(manual)** Fork <https://github.com/conda-forge/staged-recipes>, copy the recipe to
   `recipes/archivefs/meta.yaml`, and open a PR. The conda-forge bots build it on all
   platforms and a maintainer reviews it.
4. Once merged, conda-forge creates an `archivefs-feedstock`; future releases are picked
   up automatically by its version bot (you just review the auto-PR).

The recipe ships only the base runtime deps (`fsspec`, `requests`). The optional extras
(`dvc`, `chiro` → `chirograph`) are installed alongside by users who want them; note
`chirograph` must be on conda-forge first for a `chiro`-flavoured package to be viable.
