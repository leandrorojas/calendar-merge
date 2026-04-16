# Security Policy

This project handles iCloud credentials and Telegram bot tokens. If you discover
a security issue, please report it privately instead of opening a public issue.

## Reporting

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
feature on this repository, or email the maintainer directly.

Include:
- Description of the issue and its impact
- Steps to reproduce
- Affected version(s) or commits

## Scope

The following are in scope:
- Credential leakage (iCloud, Telegram, Gemini)
- Insecure handling of ICS feed contents
- Dependency vulnerabilities with a practical impact here

Out of scope:
- Issues in upstream libraries (`pyicloud`, `pyfangs`, `icalendar`) — please
  report those to their maintainers.

## Response

Maintainer response is best-effort since this is a hobby project. Critical
issues (credential exposure, RCE) will be prioritized.
