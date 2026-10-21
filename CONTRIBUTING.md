# Contributing to PICO

Thank you for your interest in PICO. This guide explains how to set up a
development checkout, which checks to run, and what a pull request needs.

Before you start, read [`AGENTS.md`](AGENTS.md), the [README](README.md), and
the documents in [`docs/`](docs/), in particular
[`docs/technical-reference.md`](docs/technical-reference.md).

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report security
issues privately, as described in [`SECURITY.md`](SECURITY.md), and never in a
public issue.

## Find something to work on

- Issues labelled
  [`good first issue`](https://github.com/dhuzard/pico-orchestrator/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
  are small and well bounded.
- Ask questions in
  [Discussions](https://github.com/dhuzard/pico-orchestrator/discussions).
- For a larger change, open an issue first so that we can agree on the scope
  before you write code.

## Development setup

You need Git, Node.js 24 (the version CI uses) and Python 3 (CI uses 3.12).
Then run the same checks as `AGENTS.md`:

```bash
npm ci
npm run check
python -m pip install -r requirements-dev.txt pytest
python -m pytest -q tests
python tools/validate_routing.py
ruff check .
```

- `npm run check` runs the TypeScript type check and the Node tests.
- `python -m pytest -q tests` runs the Python test suite.
- `python tools/validate_routing.py` checks that the policies, schemas,
  prompts, templates, README and the routing cases in
  `tests/routing-cases.yaml` agree.
- `ruff check .` lints the Python code. Install ruff with
  `python -m pip install ruff` if you do not have it. Ruff is being added to
  CI, so run it before you open a pull request.

To try a change in a real repository, run this checkout directly with
`scripts/run-local.sh <target>`. It runs the checkout as it is, without the
cached launcher.

## Rules for routing and policy changes

Routing is a policy decision, and it must stay deterministic and reviewable.

- **One evaluator.** `tools/routing_engine.py` is the only implementation of
  the routing evaluator. Do not copy route logic into prompts, templates or
  extensions.
- **Policies live in `routing/`.** `routing/default-policy.yaml` and
  `routing/delegation-policy.yaml` own the policy. Change behavior there, not
  in code paths that read it.
- **Schemas are versioned.** `schemas/` own the task, mission, execution and
  provenance contracts. A breaking change needs a new schema version, and
  migration notes in the pull request.
- **Run the offline routing cases.** Add or update cases in
  `tests/routing-cases.yaml`, then run `python tools/validate_routing.py` and
  the tests. For delegation or classification changes, also run the offline
  benchmarks that
  [`docs/technical-reference.md`](docs/technical-reference.md) describes.
- **Do not claim more than the evidence.** Preserve benchmark provenance.
  Routing one task to Claude or Codex is a policy choice, not a claim that one
  model is always better.
- **Test the unsafe cases.** Changes in this area should cover ambiguous
  repository identity, concurrent waves, resume, worker isolation and failing
  safely.

## Pull requests

- Use the [pull request template](.github/pull_request_template.md) and fill
  in every section, especially **Validation** and **Risk and rollback**.
- Keep each pull request to one bounded change. Put unrelated fixes in
  separate pull requests.
- Write `Closes #N` for the issue the pull request resolves, so it closes on
  merge. Write `Refs #N` instead when the pull request only partly addresses
  the issue.
- Add or update tests for any change in behavior.
- Add an entry under `## [Unreleased]` in [`CHANGELOG.md`](CHANGELOG.md)
  when users or contributors will notice the change.
- Update the documentation when you change a command, a policy or a
  contract.
- Do not commit secrets, local state, generated provenance or unrelated
  files.

The maintainer reviews every pull request. Changes to `routing/`, `schemas/`,
the launcher, the worker extensions and the Garden controls get extra
scrutiny (see [`.github/CODEOWNERS`](.github/CODEOWNERS)).

## Commit messages

- Write the subject in the imperative mood and in sentence case, and say
  what the change does, for example `Forward workspace watch arguments
  without consuming option values`.
- Keep the subject under about 72 characters, with no trailing period.
- Use the body to explain why, and to mention follow-up work.
- Reference issues with `#<number>` in the body. Use a closing keyword only
  when the issue should close on merge.

## AI-assisted contributions

PICO is a tool for working with coding agents, and AI-assisted contributions
are welcome. You remain responsible for them:

- Review and understand every line you submit.
- Run the checks yourself, and report only results you actually observed. Do
  not fabricate test results, benchmark numbers, token or cost metrics, or
  provenance.
- Say in the pull request if a large part of the change was generated, so
  that reviewers know where to look.

## License

PICO is released under the [MIT License](LICENSE). By contributing, you agree
that your contribution is licensed under the same license
(inbound = outbound). There is no contributor license agreement (CLA) to
sign.
