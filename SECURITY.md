# Security Policy

## Supported versions

Shidashi is pre-1.0. Only the `main` branch is supported: fixes land there and
are not backported.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Report it privately through GitHub:
[Report a vulnerability](https://github.com/obentoo/shidashi/security/advisories/new)
(the **Security** tab, then **Report a vulnerability**). Only the maintainers
see the report.

Include what you can:

- the affected command or file, and the commit you tested (`git rev-parse HEAD`);
- the steps to reproduce, and what an attacker gains;
- whether it needs a specific host setup (Shidashi runs `emerge`, `systemd-nspawn`
  and image tools as root on a Gentoo host).

You will get an acknowledgment, and updates as the report is triaged and fixed.
Once a fix is released, the advisory is published with credit to the reporter,
unless you prefer to stay anonymous.

## Scope

In scope: the Shidashi code in this repository, its CI workflows, and the build
configuration under `variants/`.

Out of scope, please report upstream instead:

- a vulnerability in a Gentoo package itself: [Gentoo Security](https://security.gentoo.org/);
- a vulnerability in a package of the Bentoo overlay:
  [obentoo/bentoo](https://github.com/obentoo/bentoo).
