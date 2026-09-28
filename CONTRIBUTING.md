# Contributing to Clicker4AI

Thanks for taking a look. This is a small project with one maintainer; issues
and pull requests are welcome.

## License of contributions

Clicker4AI is licensed under [AGPL-3.0-only](LICENSE). By submitting a
contribution you agree that it is licensed under the same terms (inbound =
outbound). There is no CLA.

As section 14 of the license allows, the maintainer (Kosio) is the proxy who
can accept a future version of the AGPL for the whole project, your
contribution included; see [NOTICE](NOTICE). Your code stays yours, and
AGPL-3.0 keeps applying to it either way.

Optionally, sign off your commits (`git commit -s`) to certify the
[Developer Certificate of Origin](https://developercertificate.org/).

## Before you open a pull request

- For anything bigger than a fix, open an issue first so we can agree on the
  approach.
- Keep the frontend dependency-free, with no build step.
- Run the checks: `python3 -m py_compile clicker4ai/*.py scripts/*.py`,
  `node --check clicker4ai/web/app.js`, `scripts/smoke_api.py` and
  `scripts/passkey_test.py`.

## Security

Please do not report security issues in public issues; use
**Security → Report a vulnerability** on GitHub instead.
